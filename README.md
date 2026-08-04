# Commerce Bridge

A small vendor portal: any vendor can sign up, run one or more stores, and expose each store's catalog through **two competing agentic-checkout standards**, with an approval step before any payment settles.

## The problem

Two rival open standards now exist for letting AI agents (ChatGPT, Gemini, etc.) buy things on a merchant's behalf:

- **ACP** — Agentic Commerce Protocol, co-developed by Stripe and OpenAI, used by ChatGPT's Instant Checkout.
- **UCP** — Universal Commerce Protocol, co-developed by Google and Shopify (with Etsy, Wayfair, Target, Walmart), used by Gemini / AI Mode in Search.

A merchant who wants to be buyable by *both* ChatGPT and Gemini currently has to integrate both specs separately. This demo shows what a thin adapter layer looks like: **the merchant's catalog and Stripe integration stay written once; a small adapter exposes it in both shapes** — plus a realistic vendor workflow around it: sign up, accept terms, create a store, list products, choose which channel(s) each product sells through, and approve or reject incoming purchase requests before anything is charged.

## What this actually is (and isn't)

- The ACP request/response shapes follow OpenAI's [published checkout spec](https://developers.openai.com/commerce/specs/checkout) closely (`line_items`, `totals`, `fulfillment_options`, `messages`, status enum, etc.).
- The UCP shapes follow the *publicly documented* primitives (checkout session, line items, totals, and the documented status lifecycle `incomplete` → `requires_escalation` → `ready_for_complete`). Shopify/Google haven't yet published full field-level JSON examples for the checkout session body, so this side is a good-faith mapping onto what **is** documented — not a byte-for-byte spec match. Worth double-checking against `ucp.dev` before treating it as authoritative.
- **Payment mode is configurable.** Stripe signups are [invite-only in India](https://support.stripe.com/questions/stripe-accounts-are-invite-only-in-india) as of this writing, so this defaults to a **simulated** charge (same response shape, no external call) if no `STRIPE_SECRET_KEY` is set — the full adapter flow runs with zero setup. Set a real Stripe **test** key (e.g. borrowed from a collaborator's account, since test keys can never move real money and are safe to share for exactly this kind of demo) and approving an order switches to a genuine Stripe test-mode `PaymentIntent`, visible in the Stripe test Dashboard.
- **Real auth, real (local) persistence, real multi-tenancy** — accounts, stores, products, and orders live in a SQLite database (`commerce_bridge.db`), not in memory. Passwords are hashed (`pbkdf2:sha256` via werkzeug). Each vendor's stores/products/orders are fully isolated from every other vendor's.
- **Payment happens on approval, not on checkout.** Completing a checkout only creates a `pending_approval` order; the vendor must explicitly Approve (which triggers the charge) or Reject (no charge) from the dashboard.
- Still a demo, not a production app: no email verification, password reset, rate-limiting, or CSRF protection, and shipment tracking is a free-text field, not a real carrier integration. Flagged here rather than silently glossed over.

## Architecture

```
Vendor account ──▶ one or more Stores ──▶ Products (per-product ACP/UCP + availability toggles)
                                                │
                          ┌─────────────────────┴─────────────────────┐
                          ▼                                           ▼
              /acp/checkout_sessions (ACP shape)          /ucp/checkout-sessions (UCP shape)
                          │                                           │
                          └──────────────────┬────────────────────────┘
                                              ▼
                                   orders (pending_approval)
                                              │
                              vendor clicks Approve / Reject
                                              ▼
                                  Stripe test charge (or simulated)
```

## Running it

No Stripe account required to run this — it works out of the box in simulated-payment mode:

```bash
cd commerce-bridge
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open **http://localhost:4242**, sign up, accept the terms, create your first store, add a product or two, then try "Test checkout as ChatGPT agent" / "...as Gemini agent" — the resulting order lands in the Orders section as `pending_approval` until you Approve or Reject it.

**To use real Stripe test-mode charges instead** (optional): get a Stripe **test** secret key (starts with `sk_test_`) — either from your own account, or a test key someone else shares with you (safe to share; test keys can never move real money) — then:
```bash
export STRIPE_SECRET_KEY=sk_test_your_key_here
python app.py
```
Now approving an order creates a real `PaymentIntent` visible in the [Stripe test Dashboard](https://dashboard.stripe.com/test/payments).

## Key routes

| Route | Purpose |
|---|---|
| `/signup`, `/login`, `/logout` | Vendor account auth |
| `/terms` | One-time terms acceptance (account-level) |
| `/onboarding` | Create a vendor's first store |
| `/dashboard` | Manage the active store: products, channels, availability, test checkout, orders |
| `/dashboard/stores` (POST) | Add another store under the same vendor account |
| `POST /acp/checkout_sessions[/<id>/complete]` | ACP-shaped checkout for the active store |
| `POST /ucp/checkout-sessions[/<id>/complete]` | UCP-shaped checkout for the active store |
| `/dashboard/orders/<id>/approve\|reject\|shipment` | Order decisions (approval triggers the charge) |

## Why I built this

Researching Stripe's product direction, the clearest open, unresolved problem I found wasn't a feature gap — it's that ACP and UCP are two live, competing standards for the same job (agent-mediated checkout), and merchants are stuck choosing or double-integrating. This is a small, honest attempt at the adapter pattern that problem implies, built to learn the actual specs rather than describe them abstractly.

## Sources

- [Agentic Commerce Protocol — GitHub](https://github.com/agentic-commerce-protocol/agentic-commerce-protocol)
- [ACP Checkout Spec — OpenAI Developers](https://developers.openai.com/commerce/specs/checkout)
- [Universal Commerce Protocol — Shopify Engineering](https://shopify.engineering/ucp)
- [UCP Docs](https://ucpdocs.com/)
