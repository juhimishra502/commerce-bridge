"""
Commerce Bridge — a tiny demo showing one product catalog sold through two
competing agentic-checkout standards:

  - ACP  (Agentic Commerce Protocol, Stripe + OpenAI / ChatGPT)
  - UCP  (Universal Commerce Protocol, Google + Shopify / Gemini)

Merchants currently have to integrate each one separately to be buyable by
different AI shopping agents. This demo shows a single catalog + a thin
adapter layer producing both shapes, with both paths settling through the
same Stripe payment underneath.

Payment mode: if STRIPE_SECRET_KEY is set (a real Stripe TEST key), this
makes a genuine Stripe test-mode PaymentIntent. If it's not set — e.g.
Stripe signups are invite-only in some countries — it falls back to a
local simulated charge with the same response shape, so the adapter logic
still runs and demos end-to-end without a Stripe account.

Schema notes:
  - The ACP request/response shapes below follow OpenAI's published spec
    (developers.openai.com/commerce/specs/checkout) closely.
  - The UCP shapes follow the publicly documented primitives (checkout
    session, line items, totals, status) and the documented status enum
    (incomplete / requires_escalation / ready_for_complete). Shopify/Google
    have not yet published full field-level JSON examples, so this side is
    a good-faith mapping onto what IS documented, not a byte-for-byte spec
    match — flagged here and in the README rather than silently assumed.
"""

import json
import os
import uuid

import stripe
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder="static")

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

with open(os.path.join(os.path.dirname(__file__), "catalog.json")) as f:
    CATALOG = {p["id"]: p for p in json.load(f)}

# In-memory session store. Fine for a demo; a real service would use a database.
SESSIONS = {}
# Total amount owed per session, kept server-side only (not part of either
# public schema) so responses stay byte-accurate to the real specs.
SESSION_TOTALS = {}

TAX_RATE = 0.08  # flat demo tax rate, not real tax logic


def _line_items_for(items):
    """Turn [{id, quantity}] into ACP-style line items using the catalog."""
    line_items = []
    for entry in items:
        product = CATALOG[entry["id"]]
        quantity = entry["quantity"]
        base_amount = product["price"] * quantity
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
    """Represents an agent's delegated payment token being charged.

    With a real Stripe TEST key configured, this confirms a genuine
    Stripe test-mode PaymentIntent using Stripe's built-in test payment
    method — it will show up in the Stripe test Dashboard. Without a key
    (e.g. no Stripe account available yet), it returns a simulated intent
    with the same shape, so the rest of the flow is unaffected.
    """
    if not stripe.api_key:
        return _SimulatedIntent(amount_cents)

    intent = stripe.PaymentIntent.create(
        amount=amount_cents,
        currency="usd",
        payment_method="pm_card_visa",
        confirm=True,
        off_session=True,
        description=description,
    )
    return intent


# ---------------------------------------------------------------------------
# Demo page
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "demo.html")


@app.route("/catalog")
def get_catalog():
    return jsonify(list(CATALOG.values()))


# ---------------------------------------------------------------------------
# ACP (Agentic Commerce Protocol) — Stripe / OpenAI shape
# ---------------------------------------------------------------------------

@app.route("/acp/checkout_sessions", methods=["POST"])
def acp_create_session():
    body = request.get_json()
    line_items = _line_items_for(body["items"])
    totals, total = _totals_for(line_items)
    session_id = f"acp_{uuid.uuid4().hex[:10]}"

    session = {
        "id": session_id,
        "protocol": "acp",
        "buyer": body.get("buyer", {}),
        "status": "ready_for_payment",
        "currency": "usd",
        "line_items": line_items,
        "fulfillment_options": [],
        "totals": totals,
        "messages": [],
        "order": None,
    }
    SESSIONS[session_id] = session
    SESSION_TOTALS[session_id] = total
    return jsonify(session), 201


@app.route("/acp/checkout_sessions/<session_id>/complete", methods=["POST"])
def acp_complete_session(session_id):
    session = SESSIONS[session_id]
    intent = _charge_with_test_card(
        SESSION_TOTALS[session_id], f"ACP order {session_id}"
    )
    session["status"] = "completed"
    session["order"] = {
        "id": f"order_{uuid.uuid4().hex[:10]}",
        "checkout_session_id": session_id,
        "permalink_url": f"https://example-shop.test/orders/{session_id}",
        "stripe_payment_intent": intent.id,
        "stripe_status": intent.status,
        "payment_mode": "real_stripe_test" if stripe.api_key else "simulated",
    }
    return jsonify(session)


# ---------------------------------------------------------------------------
# UCP (Universal Commerce Protocol) — Google / Shopify shape
# ---------------------------------------------------------------------------

@app.route("/.well-known/ucp")
def ucp_manifest():
    return jsonify(
        {
            "ucp": {
                "version": "2026-04-08",
                "services": {
                    "dev.ucp.shopping": [
                        {
                            "version": "2026-04-08",
                            "transport": "rest",
                            "endpoint": request.host_url.rstrip("/") + "/ucp/v1",
                        }
                    ]
                },
                "capabilities": {
                    "dev.ucp.shopping.checkout": [{"version": "2026-04-08"}]
                },
                "payment_handlers": {"stripe_test": {"version": "2026-04-08"}},
            }
        }
    )


@app.route("/ucp/checkout-sessions", methods=["POST"])
def ucp_create_session():
    body = request.get_json()
    line_items = _line_items_for(body["items"])
    totals, total = _totals_for(line_items)
    session_id = f"ucp_{uuid.uuid4().hex[:10]}"

    session = {
        "id": session_id,
        "protocol": "ucp",
        "status": "ready_for_complete",  # digital goods, nothing to escalate
        "line_items": line_items,
        "totals": totals,
        "messages": [],
        "order": None,
    }
    SESSIONS[session_id] = session
    SESSION_TOTALS[session_id] = total
    return jsonify(session), 201


@app.route("/ucp/checkout-sessions/<session_id>/complete", methods=["POST"])
def ucp_complete_session(session_id):
    session = SESSIONS[session_id]
    intent = _charge_with_test_card(
        SESSION_TOTALS[session_id], f"UCP order {session_id}"
    )
    session["status"] = "completed"
    session["order"] = {
        "id": f"order_{uuid.uuid4().hex[:10]}",
        "checkout_session_id": session_id,
        "stripe_payment_intent": intent.id,
        "stripe_status": intent.status,
        "payment_mode": "real_stripe_test" if stripe.api_key else "simulated",
    }
    return jsonify(session)


if __name__ == "__main__":
    if stripe.api_key:
        print("Payment mode: REAL Stripe test-mode charges (STRIPE_SECRET_KEY set)")
    else:
        print(
            "Payment mode: SIMULATED (no STRIPE_SECRET_KEY set) — the adapter "
            "logic runs end-to-end, but no real Stripe API call is made. Set "
            "STRIPE_SECRET_KEY to a Stripe TEST key to switch to real test charges."
        )
    app.run(debug=True, port=4242)
