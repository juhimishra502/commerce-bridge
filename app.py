"""
Commerce Bridge — a multi-vendor portal demonstrating one core idea: a
merchant's product catalog can be exposed through two competing agentic
checkout standards from one adapter layer:

  - ACP  (Agentic Commerce Protocol, Stripe + OpenAI / ChatGPT)
  - UCP  (Universal Commerce Protocol, Google + Shopify / Gemini)

A vendor account can register multiple stores. Each store has its own
products, per-product channel settings (sold via ChatGPT / Gemini / both /
neither), and its own orders. Incoming checkouts land as pending orders
that the vendor must approve (triggering payment) or reject before any
charge happens.

Payment mode: if STRIPE_SECRET_KEY is set (a real Stripe TEST key),
approving an order makes a genuine Stripe test-mode PaymentIntent. If it's
not set — e.g. Stripe signups are invite-only in some countries — it
falls back to a local simulated charge with the same response shape.

Schema notes:
  - The ACP request/response shapes follow OpenAI's published spec
    (developers.openai.com/commerce/specs/checkout) closely.
  - The UCP shapes follow the publicly documented primitives (checkout
    session, line items, totals, status) and the documented status enum
    (incomplete / requires_escalation / ready_for_complete). Shopify/Google
    have not yet published full field-level JSON examples, so this side is
    a good-faith mapping onto what IS documented, not a byte-for-byte spec
    match.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from functools import wraps

import stripe
from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

def _load_or_create_secret_key():
    """A random key generated fresh on every process start would silently
    invalidate every logged-in session on each restart/redeploy (signed
    cookies fail to verify against a new key). Persist one to disk instead
    so restarts don't log everyone out, unless FLASK_SECRET_KEY is set
    (e.g. in production)."""
    env_key = os.environ.get("FLASK_SECRET_KEY")
    if env_key:
        return env_key
    key_path = os.path.join(os.path.dirname(__file__), ".flask_secret_key")
    if os.path.exists(key_path):
        with open(key_path, "r") as f:
            return f.read().strip()
    key = os.urandom(24).hex()
    with open(key_path, "w") as f:
        f.write(key)
    return key


app = Flask(__name__)
app.secret_key = _load_or_create_secret_key()

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

DB_PATH = os.path.join(os.path.dirname(__file__), "commerce_bridge.db")
TAX_RATE = 0.08  # flat demo tax rate, not real tax logic

# In-memory holding area for checkout sessions between "create" and
# "complete" — deliberately transient (an uncompleted session expiring on
# restart is fine; the durable record is the `orders` row written on
# completion).
SESSIONS = {}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS vendors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            accepted_terms_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor_id INTEGER NOT NULL REFERENCES vendors(id),
            store_name TEXT NOT NULL,
            default_acp_enabled INTEGER NOT NULL DEFAULT 1,
            default_ucp_enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER NOT NULL REFERENCES stores(id),
            title TEXT NOT NULL,
            description TEXT,
            price_cents INTEGER NOT NULL,
            currency TEXT NOT NULL DEFAULT 'usd',
            acp_enabled INTEGER NOT NULL DEFAULT 1,
            ucp_enabled INTEGER NOT NULL DEFAULT 1,
            available INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT UNIQUE NOT NULL,
            store_id INTEGER NOT NULL REFERENCES stores(id),
            protocol TEXT NOT NULL,
            status TEXT NOT NULL,
            buyer_json TEXT,
            line_items_json TEXT NOT NULL,
            totals_json TEXT NOT NULL,
            total_cents INTEGER NOT NULL,
            payment_intent_id TEXT,
            payment_mode TEXT,
            shipment_estimate TEXT,
            created_at TEXT NOT NULL,
            decided_at TEXT
        );
        CREATE TABLE IF NOT EXISTS demo_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            customer_email TEXT,
            store_id INTEGER NOT NULL REFERENCES stores(id),
            product_id INTEGER NOT NULL REFERENCES products(id),
            stage TEXT NOT NULL,
            payment_method TEXT,
            billing_name TEXT,
            billing_address TEXT,
            payment_intent_id TEXT,
            payment_mode TEXT,
            shipping_estimate TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


init_db()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def current_vendor():
    vendor_id = session.get("vendor_id")
    if not vendor_id:
        return None
    return get_db().execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()


def current_store():
    """The vendor's currently active store, defaulting to their first one."""
    vendor = current_vendor()
    if vendor is None:
        return None
    db = get_db()
    store_id = session.get("active_store_id")
    if store_id:
        row = db.execute(
            "SELECT * FROM stores WHERE id = ? AND vendor_id = ?", (store_id, vendor["id"])
        ).fetchone()
        if row:
            return row
    row = db.execute(
        "SELECT * FROM stores WHERE vendor_id = ? ORDER BY id LIMIT 1", (vendor["id"],)
    ).fetchone()
    if row:
        session["active_store_id"] = row["id"]
    return row


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        vendor = current_vendor()
        if vendor is None:
            return redirect(url_for("login"))
        if not vendor["accepted_terms_at"]:
            return redirect(url_for("terms"))
        return f(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Checkout math (shared by ACP + UCP)
# ---------------------------------------------------------------------------

def _line_items_for(store_id, items, protocol):
    db = get_db()
    channel_field = "acp_enabled" if protocol == "acp" else "ucp_enabled"
    line_items = []
    for entry in items:
        product = db.execute(
            "SELECT * FROM products WHERE id = ? AND store_id = ?", (entry["id"], store_id)
        ).fetchone()
        if product is None:
            raise ValueError(f"Unknown product id {entry['id']}")
        if not product["available"]:
            raise ValueError(f"'{product['title']}' is not available for sale")
        if not product[channel_field]:
            raise ValueError(f"'{product['title']}' is not enabled for this channel")

        quantity = entry["quantity"]
        base_amount = product["price_cents"] * quantity
        tax = round(base_amount * TAX_RATE)
        line_items.append(
            {
                "id": f"li_{uuid.uuid4().hex[:8]}",
                "item": {"id": product["id"], "quantity": quantity},
                "base_amount": base_amount,
                "discount": 0,
                "subtotal": base_amount,
                "tax": tax,
                "total": base_amount + tax,
            }
        )
    return line_items


def _totals_for(line_items):
    subtotal = sum(li["subtotal"] for li in line_items)
    tax = sum(li["tax"] for li in line_items)
    total = subtotal + tax
    return [
        {"type": "items_base_amount", "display_text": "Items", "amount": subtotal},
        {"type": "tax", "display_text": "Tax", "amount": tax},
        {"type": "total", "display_text": "Total", "amount": total},
    ], total


class _SimulatedIntent:
    """Stands in for a stripe.PaymentIntent when no real Stripe key is
    configured, so callers can treat both cases identically (.id, .status)."""

    def __init__(self, amount_cents):
        self.id = f"pi_simulated_{uuid.uuid4().hex[:16]}"
        self.status = "succeeded"
        self.amount = amount_cents


def _charge_with_test_card(amount_cents, description):
    """Represents an agent's delegated payment token being charged, run at
    the moment a vendor approves an order (not at checkout-complete time).

    With a real Stripe TEST key configured, this confirms a genuine
    Stripe test-mode PaymentIntent using Stripe's built-in test payment
    method. Without a key, it returns a simulated intent with the same
    shape, so the rest of the flow is unaffected.
    """
    if not stripe.api_key:
        return _SimulatedIntent(amount_cents)

    return stripe.PaymentIntent.create(
        amount=amount_cents,
        currency="usd",
        payment_method="pm_card_visa",
        confirm=True,
        off_session=True,
        description=description,
    )


# ---------------------------------------------------------------------------
# Auth & onboarding routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    if current_vendor():
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET":
        return render_template("signup.html", error=None)

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    if not email or not password:
        return render_template("signup.html", error="Email and password are required.")
    if len(password) < 8:
        return render_template("signup.html", error="Password must be at least 8 characters.")

    db = get_db()
    if db.execute("SELECT id FROM vendors WHERE email = ?", (email,)).fetchone():
        return render_template("signup.html", error="An account with that email already exists.")

    cur = db.execute(
        "INSERT INTO vendors (email, password_hash, created_at) VALUES (?, ?, ?)",
        (email, generate_password_hash(password, method="pbkdf2:sha256"), now_iso()),
    )
    db.commit()
    session.clear()
    session["vendor_id"] = cur.lastrowid
    return redirect(url_for("terms"))


@app.route("/terms", methods=["GET", "POST"])
def terms():
    vendor = current_vendor()
    if vendor is None:
        return redirect(url_for("login"))

    if request.method == "GET":
        return render_template("terms.html", error=None)

    if request.form.get("agree") != "on":
        return render_template("terms.html", error="You must accept the terms to continue.")

    db = get_db()
    db.execute("UPDATE vendors SET accepted_terms_at = ? WHERE id = ?", (now_iso(), vendor["id"]))
    db.commit()

    has_store = db.execute("SELECT id FROM stores WHERE vendor_id = ?", (vendor["id"],)).fetchone()
    return redirect(url_for("onboarding") if not has_store else url_for("dashboard"))


@app.route("/onboarding", methods=["GET", "POST"])
@login_required
def onboarding():
    if request.method == "GET":
        return render_template("onboarding.html", error=None)

    store_name = request.form.get("store_name", "").strip()
    if not store_name:
        return render_template("onboarding.html", error="Store name is required.")

    acp = 1 if request.form.get("acp") == "on" else 0
    ucp = 1 if request.form.get("ucp") == "on" else 0

    db = get_db()
    cur = db.execute(
        "INSERT INTO stores (vendor_id, store_name, default_acp_enabled, default_ucp_enabled, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (current_vendor()["id"], store_name, acp, ucp, now_iso()),
    )
    db.commit()
    session["active_store_id"] = cur.lastrowid
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", error=None)

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    row = get_db().execute("SELECT * FROM vendors WHERE email = ?", (email,)).fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        return render_template("login.html", error="Invalid email or password.")

    session.clear()
    session["vendor_id"] = row["id"]
    if not row["accepted_terms_at"]:
        return redirect(url_for("terms"))
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    vendor = current_vendor()
    db = get_db()
    stores = db.execute(
        "SELECT * FROM stores WHERE vendor_id = ? ORDER BY id", (vendor["id"],)
    ).fetchall()

    if not stores:
        return redirect(url_for("onboarding"))

    store = current_store()
    products = db.execute(
        "SELECT * FROM products WHERE store_id = ? ORDER BY id", (store["id"],)
    ).fetchall()
    orders = db.execute(
        "SELECT * FROM orders WHERE store_id = ? ORDER BY id DESC", (store["id"],)
    ).fetchall()

    products_json = json.dumps(
        [
            {
                "id": p["id"],
                "available": bool(p["available"]),
                "acp_enabled": bool(p["acp_enabled"]),
                "ucp_enabled": bool(p["ucp_enabled"]),
            }
            for p in products
        ]
    )

    return render_template(
        "dashboard.html",
        vendor=vendor,
        stores=stores,
        store=store,
        products=products,
        orders=orders,
        products_json=products_json,
    )


@app.route("/dashboard/switch-store", methods=["POST"])
@login_required
def switch_store():
    vendor = current_vendor()
    store_id = request.form.get("store_id", type=int)
    row = get_db().execute(
        "SELECT id FROM stores WHERE id = ? AND vendor_id = ?", (store_id, vendor["id"])
    ).fetchone()
    if row:
        session["active_store_id"] = row["id"]
    return redirect(url_for("dashboard"))


@app.route("/dashboard/stores", methods=["POST"])
@login_required
def add_store():
    vendor = current_vendor()
    store_name = request.form.get("store_name", "").strip()
    acp = 1 if request.form.get("acp") == "on" else 0
    ucp = 1 if request.form.get("ucp") == "on" else 0
    if store_name:
        db = get_db()
        cur = db.execute(
            "INSERT INTO stores (vendor_id, store_name, default_acp_enabled, default_ucp_enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (vendor["id"], store_name, acp, ucp, now_iso()),
        )
        db.commit()
        session["active_store_id"] = cur.lastrowid
    return redirect(url_for("dashboard"))


@app.route("/dashboard/products", methods=["POST"])
@login_required
def add_product():
    store = current_store()
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    try:
        price_cents = int(round(float(request.form.get("price", "0")) * 100))
    except ValueError:
        price_cents = 0

    if title and price_cents > 0:
        db = get_db()
        db.execute(
            "INSERT INTO products "
            "(store_id, title, description, price_cents, acp_enabled, ucp_enabled, available, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (
                store["id"],
                title,
                description,
                price_cents,
                store["default_acp_enabled"],
                store["default_ucp_enabled"],
                now_iso(),
            ),
        )
        db.commit()
    return redirect(url_for("dashboard"))


_TOGGLE_FIELDS = {"available", "acp_enabled", "ucp_enabled"}


@app.route("/dashboard/products/<int:product_id>/toggle", methods=["POST"])
@login_required
def toggle_product(product_id):
    store = current_store()
    field = request.form.get("field")
    if field not in _TOGGLE_FIELDS:
        return redirect(url_for("dashboard"))

    db = get_db()
    row = db.execute(
        "SELECT * FROM products WHERE id = ? AND store_id = ?", (product_id, store["id"])
    ).fetchone()
    if row:
        new_value = 0 if row[field] else 1
        db.execute(f"UPDATE products SET {field} = ? WHERE id = ?", (new_value, product_id))
        db.commit()
    return redirect(url_for("dashboard"))


@app.route("/dashboard/orders/<int:order_id>/approve", methods=["POST"])
@login_required
def approve_order(order_id):
    store = current_store()
    db = get_db()
    order = db.execute(
        "SELECT * FROM orders WHERE id = ? AND store_id = ?", (order_id, store["id"])
    ).fetchone()
    if order and order["status"] == "pending_approval":
        intent = _charge_with_test_card(order["total_cents"], f"Order {order['public_id']}")
        db.execute(
            "UPDATE orders SET status = 'approved', payment_intent_id = ?, payment_mode = ?, decided_at = ? "
            "WHERE id = ?",
            (
                intent.id,
                "real_stripe_test" if stripe.api_key else "simulated",
                now_iso(),
                order_id,
            ),
        )
        db.commit()
    return redirect(url_for("dashboard"))


@app.route("/dashboard/orders/<int:order_id>/reject", methods=["POST"])
@login_required
def reject_order(order_id):
    store = current_store()
    db = get_db()
    order = db.execute(
        "SELECT * FROM orders WHERE id = ? AND store_id = ?", (order_id, store["id"])
    ).fetchone()
    if order and order["status"] == "pending_approval":
        db.execute(
            "UPDATE orders SET status = 'rejected', decided_at = ? WHERE id = ?",
            (now_iso(), order_id),
        )
        db.commit()
    return redirect(url_for("dashboard"))


@app.route("/dashboard/orders/<int:order_id>/shipment", methods=["POST"])
@login_required
def set_shipment(order_id):
    store = current_store()
    estimate = request.form.get("shipment_estimate", "").strip()
    db = get_db()
    order = db.execute(
        "SELECT * FROM orders WHERE id = ? AND store_id = ?", (order_id, store["id"])
    ).fetchone()
    if order and order["status"] == "approved":
        db.execute("UPDATE orders SET shipment_estimate = ? WHERE id = ?", (estimate, order_id))
        db.commit()
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# ACP (Agentic Commerce Protocol) — Stripe / OpenAI shape
# ---------------------------------------------------------------------------

@app.route("/acp/checkout_sessions", methods=["POST"])
@login_required
def acp_create_session():
    store = current_store()
    body = request.get_json()
    try:
        line_items = _line_items_for(store["id"], body["items"], "acp")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    totals, total = _totals_for(line_items)
    session_id = f"acp_{uuid.uuid4().hex[:10]}"
    sess = {
        "id": session_id,
        "protocol": "acp",
        "_store_id": store["id"],
        "buyer": body.get("buyer", {}),
        "status": "ready_for_payment",
        "currency": "usd",
        "line_items": line_items,
        "fulfillment_options": [],
        "totals": totals,
        "messages": [],
        "order": None,
        "_total": total,
    }
    SESSIONS[session_id] = sess
    return jsonify({k: v for k, v in sess.items() if not k.startswith("_")}), 201


@app.route("/acp/checkout_sessions/<session_id>/complete", methods=["POST"])
@login_required
def acp_complete_session(session_id):
    store = current_store()
    sess = SESSIONS.get(session_id)
    if not sess or sess["_store_id"] != store["id"]:
        return jsonify({"error": "Session not found"}), 404

    db = get_db()
    public_id = f"order_{uuid.uuid4().hex[:10]}"
    db.execute(
        "INSERT INTO orders "
        "(public_id, store_id, protocol, status, buyer_json, line_items_json, totals_json, total_cents, created_at) "
        "VALUES (?, ?, 'acp', 'pending_approval', ?, ?, ?, ?, ?)",
        (
            public_id,
            store["id"],
            json.dumps(sess["buyer"]),
            json.dumps(sess["line_items"]),
            json.dumps(sess["totals"]),
            sess["_total"],
            now_iso(),
        ),
    )
    db.commit()

    sess["status"] = "pending_approval"
    sess["order"] = {"id": public_id, "checkout_session_id": session_id, "status": "pending_approval"}
    del SESSIONS[session_id]
    return jsonify({k: v for k, v in sess.items() if not k.startswith("_")})


# ---------------------------------------------------------------------------
# UCP (Universal Commerce Protocol) — Google / Shopify shape
# ---------------------------------------------------------------------------

@app.route("/ucp/checkout-sessions", methods=["POST"])
@login_required
def ucp_create_session():
    store = current_store()
    body = request.get_json()
    try:
        line_items = _line_items_for(store["id"], body["items"], "ucp")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    totals, total = _totals_for(line_items)
    session_id = f"ucp_{uuid.uuid4().hex[:10]}"
    sess = {
        "id": session_id,
        "protocol": "ucp",
        "_store_id": store["id"],
        "status": "ready_for_complete",  # digital goods, nothing to escalate
        "line_items": line_items,
        "totals": totals,
        "messages": [],
        "order": None,
        "_total": total,
    }
    SESSIONS[session_id] = sess
    return jsonify({k: v for k, v in sess.items() if not k.startswith("_")}), 201


@app.route("/ucp/checkout-sessions/<session_id>/complete", methods=["POST"])
@login_required
def ucp_complete_session(session_id):
    store = current_store()
    sess = SESSIONS.get(session_id)
    if not sess or sess["_store_id"] != store["id"]:
        return jsonify({"error": "Session not found"}), 404

    db = get_db()
    public_id = f"order_{uuid.uuid4().hex[:10]}"
    db.execute(
        "INSERT INTO orders "
        "(public_id, store_id, protocol, status, buyer_json, line_items_json, totals_json, total_cents, created_at) "
        "VALUES (?, ?, 'ucp', 'pending_approval', NULL, ?, ?, ?, ?)",
        (
            public_id,
            store["id"],
            json.dumps(sess["line_items"]),
            json.dumps(sess["totals"]),
            sess["_total"],
            now_iso(),
        ),
    )
    db.commit()

    sess["status"] = "pending_approval"
    sess["order"] = {"id": public_id, "checkout_session_id": session_id}
    del SESSIONS[session_id]
    return jsonify({k: v for k, v in sess.items() if not k.startswith("_")})


# ---------------------------------------------------------------------------
# Customer purchase flow demo — a human-facing walkthrough (request → payment
# method → checkout → invoice), separate from the vendor dashboard above.
# No login: this is a scripted demo of the buyer's side of a purchase, using
# real product/store data already in the system. Not a real customer account
# system — see README for why.
# ---------------------------------------------------------------------------

SHIPPING_ESTIMATES = ["3-5 business days", "5-7 business days", "7-10 business days"]


def _demo_order_or_404(demo_order_id):
    return get_db().execute(
        "SELECT * FROM demo_orders WHERE id = ?", (demo_order_id,)
    ).fetchone()


@app.route("/customer-demo")
def customer_demo_list():
    db = get_db()
    products = db.execute(
        "SELECT products.*, stores.store_name FROM products "
        "JOIN stores ON stores.id = products.store_id "
        "ORDER BY products.id"
    ).fetchall()
    demo_orders = db.execute(
        "SELECT demo_orders.*, products.title AS product_title, stores.store_name FROM demo_orders "
        "JOIN products ON products.id = demo_orders.product_id "
        "JOIN stores ON stores.id = demo_orders.store_id "
        "ORDER BY demo_orders.id DESC"
    ).fetchall()
    return render_template("customer_demo_list.html", products=products, demo_orders=demo_orders)


@app.route("/customer-demo/start", methods=["POST"])
def customer_demo_start():
    customer_name = request.form.get("customer_name", "").strip()
    customer_email = request.form.get("customer_email", "").strip()
    product_id = request.form.get("product_id", type=int)

    db = get_db()
    product = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not customer_name or not product:
        return redirect(url_for("customer_demo_list"))

    cur = db.execute(
        "INSERT INTO demo_orders (customer_name, customer_email, store_id, product_id, stage, created_at) "
        "VALUES (?, ?, ?, ?, 'requested', ?)",
        (customer_name, customer_email, product["store_id"], product["id"], now_iso()),
    )
    db.commit()
    return redirect(url_for("customer_demo_request", demo_order_id=cur.lastrowid))


@app.route("/customer-demo/<int:demo_order_id>/request")
def customer_demo_request(demo_order_id):
    db = get_db()
    demo_order = _demo_order_or_404(demo_order_id)
    product = db.execute(
        "SELECT products.*, stores.store_name FROM products JOIN stores ON stores.id = products.store_id "
        "WHERE products.id = ?",
        (demo_order["product_id"],),
    ).fetchone()
    return render_template("customer_demo_request.html", demo_order=demo_order, product=product)


@app.route("/customer-demo/<int:demo_order_id>/payment", methods=["GET", "POST"])
def customer_demo_payment(demo_order_id):
    db = get_db()
    demo_order = _demo_order_or_404(demo_order_id)
    product = db.execute(
        "SELECT products.*, stores.store_name FROM products JOIN stores ON stores.id = products.store_id "
        "WHERE products.id = ?",
        (demo_order["product_id"],),
    ).fetchone()

    if request.method == "GET":
        return render_template("customer_demo_payment.html", demo_order=demo_order, product=product)

    db.execute(
        "UPDATE demo_orders SET payment_method = 'card', stage = 'checkout' WHERE id = ?",
        (demo_order_id,),
    )
    db.commit()
    return redirect(url_for("customer_demo_checkout", demo_order_id=demo_order_id))


@app.route("/customer-demo/<int:demo_order_id>/checkout", methods=["GET", "POST"])
def customer_demo_checkout(demo_order_id):
    db = get_db()
    demo_order = _demo_order_or_404(demo_order_id)
    product = db.execute(
        "SELECT products.*, stores.store_name FROM products JOIN stores ON stores.id = products.store_id "
        "WHERE products.id = ?",
        (demo_order["product_id"],),
    ).fetchone()

    if request.method == "GET":
        return render_template("customer_demo_checkout.html", demo_order=demo_order, product=product)

    billing_name = request.form.get("billing_name", "").strip()
    billing_address = request.form.get("billing_address", "").strip()

    tax = round(product["price_cents"] * TAX_RATE)
    total = product["price_cents"] + tax
    intent = _charge_with_test_card(total, f"Customer demo order {demo_order_id}")
    shipping_estimate = SHIPPING_ESTIMATES[demo_order_id % len(SHIPPING_ESTIMATES)]

    db.execute(
        "UPDATE demo_orders SET billing_name = ?, billing_address = ?, payment_intent_id = ?, "
        "payment_mode = ?, shipping_estimate = ?, stage = 'invoiced' WHERE id = ?",
        (
            billing_name,
            billing_address,
            intent.id,
            "real_stripe_test" if stripe.api_key else "simulated",
            shipping_estimate,
            demo_order_id,
        ),
    )
    db.commit()
    return redirect(url_for("customer_demo_invoice", demo_order_id=demo_order_id))


@app.route("/customer-demo/<int:demo_order_id>/invoice")
def customer_demo_invoice(demo_order_id):
    db = get_db()
    demo_order = _demo_order_or_404(demo_order_id)
    product = db.execute(
        "SELECT products.*, stores.store_name FROM products JOIN stores ON stores.id = products.store_id "
        "WHERE products.id = ?",
        (demo_order["product_id"],),
    ).fetchone()

    tax = round(product["price_cents"] * TAX_RATE)
    total = product["price_cents"] + tax
    return render_template(
        "customer_demo_invoice.html", demo_order=demo_order, product=product, tax=tax, total=total
    )


if __name__ == "__main__":
    if stripe.api_key:
        print("Payment mode: REAL Stripe test-mode charges (STRIPE_SECRET_KEY set)")
    else:
        print(
            "Payment mode: SIMULATED (no STRIPE_SECRET_KEY set) — approvals still "
            "run the full flow, just without a real Stripe API call."
        )
    app.run(debug=True, port=4242)
