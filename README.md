# Commerce Bridge

A small demo: **one product catalog, sold through two competing agentic-checkout standards**, with both paths settling through the same real (test-mode) Stripe payment underneath.

## The problem

Two rival open standards now exist for letting AI agents (ChatGPT, Gemini, etc.) buy things on a merchant's behalf:

- **ACP** — Agentic Commerce Protocol, co-developed by Stripe and OpenAI, used by ChatGPT's Instant Checkout.
- **UCP** — Universal Commerce Protocol, co-developed by Google and Shopify (with Etsy, Wayfair, Target, Walmart), used by Gemini / AI Mode in Search.

A merchant who wants to be buyable by *both* ChatGPT and Gemini currently has to integrate both specs separately. This demo shows what a thin adapter layer looks like: **the merchant's catalog and Stripe integration stay written once; a small adapter exposes it in both shapes.**

## What this actually is (and isn't)

- The ACP request/response shapes follow OpenAI's [published checkout spec](https://developers.openai.com/commerce/specs/checkout) closely (`line_items`, `totals`, `fulfillment_options`, `messages`, status enum, etc.).
- The UCP shapes follow the *publicly documented* primitives (checkout session, line items, totals, and the documented status lifecycle `incomplete` → `requires_escalation` → `ready_for_complete`). Shopify/Google haven't yet published full field-level JSON examples for the checkout session body, so this side is a good-faith mapping onto what **is** documented — not a byte-for-byte spec match. Worth double-checking against `ucp.dev` before treating it as authoritative.
- **Payment mode is configurable.** Stripe signups are [invite-only in India](https://support.stripe.com/questions/stripe-accounts-are-invite-only-in-india) as of this writing, so this defaults to a **simulated** charge (same response shape, no external call) if no `STRIPE_SECRET_KEY` is set — the full adapter flow runs with zero setup. Set a real Stripe **test** key (e.g. borrowed from a collaborator's account, since test keys can never move real money and are safe to share for exactly this kind of demo) and it switches to genuine Stripe test-mode `PaymentIntent`s, visible in the Stripe test Dashboard.
- This is a demo of the *pattern*, not a production-ready adapter — no auth, no persistence (sessions live in memory and reset on restart), no error handling beyond the happy path.

## Architecture

```
catalog.json  ──┬──▶  /acp/checkout_sessions   (ACP shape)  ──▶  Stripe test charge
                └──▶  /ucp/checkout-sessions    (UCP shape)  ──▶  Stripe test charge
```

One source of truth (`catalog.json`), two protocol-shaped adapters (`app.py`), one settlement path (Stripe test mode).

## Running it

No Stripe account required to run this — it works out of the box in simulated-payment mode:

```bash
cd commerce-bridge
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open **http://localhost:4242** in your browser. Click either button to simulate an agent buying through that protocol — the terminal running `app.py` tells you which payment mode is active.

**To use real Stripe test-mode charges instead** (optional): get a Stripe **test** secret key (starts with `sk_test_`) — either from your own account, or a test key someone else shares with you (safe to share; test keys can never move real money) — then:
```bash
export STRIPE_SECRET_KEY=sk_test_your_key_here
python app.py
```
Now completed checkouts create real `PaymentIntent`s visible in the [Stripe test Dashboard](https://dashboard.stripe.com/test/payments).

## Key endpoints

| Endpoint | Protocol | Purpose |
|---|---|---|
| `GET /catalog` | — | Shared product catalog |
| `GET /.well-known/ucp` | UCP | Capability manifest an agent fetches first |
| `POST /acp/checkout_sessions` | ACP | Create a checkout session |
| `POST /acp/checkout_sessions/<id>/complete` | ACP | Complete payment |
| `POST /ucp/checkout-sessions` | UCP | Create a checkout session |
| `POST /ucp/checkout-sessions/<id>/complete` | UCP | Complete payment |

## Why I built this

Researching Stripe's product direction, the clearest open, unresolved problem I found wasn't a feature gap — it's that ACP and UCP are two live, competing standards for the same job (agent-mediated checkout), and merchants are stuck choosing or double-integrating. This is a small, honest attempt at the adapter pattern that problem implies, built to learn the actual specs rather than describe them abstractly.

## Sources

- [Agentic Commerce Protocol — GitHub](https://github.com/agentic-commerce-protocol/agentic-commerce-protocol)
- [ACP Checkout Spec — OpenAI Developers](https://developers.openai.com/commerce/specs/checkout)
- [Universal Commerce Protocol — Shopify Engineering](https://shopify.engineering/ucp)
- [UCP Docs](https://ucpdocs.com/)
