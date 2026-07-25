# Run Empire v2

---

name: run-empire-v2
description: Build, launch, and smoke-test the Empire v2 FastAPI platform (video orders, subscriptions, trading dashboard) locally - run the app, verify a change, screenshot the quote form
type: project-skill

---

## Overview

Empire v2 is a FastAPI backend serving several products off one app: a video
production SaaS (quote form → Stripe payment → HeyGen video generation →
customer delivery), a worker/client services marketplace, subscriptions, and
a real-money trading dashboard backed by background bot threads.

The driver is a Node.js script (`.claude/skills/run-empire-v2/driver.mjs`)
that launches the Python server on port 8000, verifies it's ready, and runs
smoke tests on key endpoints (health check, quote form, subscription tiers,
order creation, order status).

**This skill covers the HTTP surface** (routes, orders, subscriptions). For
the background trading bots (`prop_bot.py`, `tradovate_bot.py`) - which are
daemon threads calling a real broker API on a timer, not HTTP endpoints -
see the separate `.claude/skills/verify/SKILL.md`, which drives them with a
stubbed network boundary instead.

## Prerequisites

**Runtime:**
- Python 3.11+ with `pip`
- Node.js 18+ (for the driver script)

**OS packages** (if needed):
```bash
apt-get install -y python3 python3-pip python3-venv nodejs npm
```

**Python dependencies:**
```bash
pip install -r requirements.txt
```

The app includes FastAPI, SQLAlchemy, Stripe SDK, httpx (for HeyGen), and other dependencies already specified in `requirements.txt`.

## Build

No build step needed. The app is a pure Python FastAPI server that runs directly.

```bash
# Verify imports work
python3 -c "from main import app; print('FastAPI app loaded OK')"
```

## Run (Agent Path)

**Start the server + run tests:**
```bash
node .claude/skills/run-empire-v2/driver.mjs test
```

This will:
1. Start the FastAPI server on `http://localhost:8000`
2. Wait for it to be ready (30-second timeout)
3. Run smoke tests:
   - Health check (`GET /health`)
   - Quote form loads (`GET /quote` returns HTML)
   - List subscription tiers (`GET /subscriptions/tiers`)
   - Get one tier's detail (`GET /subscriptions/tiers/pro`)
   - Create an order via API (`POST /orders/request-quote`, JSON body)
   - Retrieve order status (`GET /orders/customer/{id}?email=...`)
4. Print results (pass/fail count)
5. Stop the server and exit

**Output** (this is a real run, verified this session):
```
Starting FastAPI server on port 8000...
INFO:pgusa:Migration OK: monitor_errors table
INFO:health_monitor:📊 Loaded: 14 errors, 0 fixes, 2 metrics
✓ Server ready

📊 Running smoke tests...

✓ Health Check
✓ Quote Form HTML: HTML form loaded
✓ List Subscription Tiers: 4 tiers
✓ Get Pro Tier Details: tier detail loaded
✓ Create Order (Request Quote): Order #1 created (750 cents)
✓ Get Order Status: Order #1 retrieved

📈 Results: 6 passed, 0 failed
```

### Driver Commands

| Command | What it does | Needs a server already running? |
|---------|------------|------|
| `node driver.mjs start` | Launch server, stay running (Ctrl+C to stop) | starts its own |
| `node driver.mjs test` | Launch, run smoke tests, stop | starts its own |
| `node driver.mjs health` | Show health endpoint response | **yes** |
| `node driver.mjs quote-form` | Check if quote form HTML loads | **yes** |
| `node driver.mjs create-order` | Create a test order and show response | **yes** |
| `node driver.mjs stop` | Kill any running server process | n/a |

`health`, `quote-form`, and `create-order` hit an already-listening server -
they don't start one themselves. Run `start` in one shell (or background it)
first, or just use `test`, which is self-contained (start → test → stop) and
is the right default for a quick check.

## Run (Human Path)

To develop interactively:

```bash
# Terminal 1: Start the server
python main.py

# Terminal 2: Test in another shell
curl http://localhost:8000/health
curl http://localhost:8000/quote

# request-quote takes a JSON body (Pydantic model), not query params -
# sending it as query params 422s.
curl -X POST http://localhost:8000/orders/request-quote \
  -H "Content-Type: application/json" \
  -d '{"customer_name":"John","customer_email":"test@example.com","customer_company":"TestCo","video_type":"explainer","script_or_topic":"test","target_audience":"business","avatar":"anna","language":"english_us"}'

# Order status is customer-facing and needs the matching email as a second
# factor (see Gotchas) - not the order ID alone.
curl "http://localhost:8000/orders/customer/1?email=test@example.com"

# Browser: Open http://localhost:8000/quote to see the form
# (Ctrl+C in Terminal 1 to stop)
```

## Environment Variables

Optional (app works without these):
- `STRIPE_SECRET_KEY` — Stripe API key (for payment processing)
- `STRIPE_PUBLISHABLE_KEY` — Stripe publishable key
- `STRIPE_WEBHOOK_SECRET` — Webhook signature validation
- `HEYGAN_API_KEY` — HeyGen API key (for video generation)
- `GMAIL_EMAIL` / `GMAIL_PASSWORD` — Gmail SMTP (for email notifications)

For local testing, these are optional; video generation and email are skipped if not configured.

## Test

```bash
# Run the driver smoke tests (recommended) - real HTTP requests against a
# real running server and a real (SQLite) DB write
node .claude/skills/run-empire-v2/driver.mjs test
```

There's no `tests/` directory or pytest suite. There are two standalone
scripts at the repo root (`test_outreach_persistence.py`,
`test_video_generation.py`) runnable directly with `python3 <file>`, but
they're one-off integration checks for specific features, not a general
suite - the driver above is the actual smoke test for "does the app work."

## Gotchas

### Quote form returns 404 on Railway

**Issue:** The quote form endpoint works locally but returns 404 when deployed to Railway.

**Cause:** Railway's working directory varies between deployments, affecting where `quote_request.html` is resolved.

**Fix:** The `/quote` endpoint tries three file paths:
1. `os.path.dirname(os.path.abspath(__file__))/quote_request.html`
2. `/app/quote_request.html`
3. `quote_request.html`

This handles most Railway variations. If the form still fails after deployment, check the Railway container's working directory in logs.

### `request-quote` takes a JSON body, not query params

**Issue:** `POST /orders/request-quote` 422s if you send the fields as URL
query params.

**Why:** `routers/orders.py` declares it as `async def request_quote(quote:
QuoteRequest, ...)` - a Pydantic model, so FastAPI expects a JSON request
body. This used to take query params and used to be double-prefixed
(`/orders/orders/request-quote`) before the DB-backed storage migration;
both of those are gone now. If you hit either old shape, you'll get a 404
(wrong path) or 422 (wrong param style) - this skill was itself out of date
about this until this session's verification pass caught it.

**Fix:** POST a JSON body to `/orders/request-quote` (single prefix). See
the curl example above or `driver.mjs`'s `jsonBody` test case.

### Order status lookup needs the customer's email, not just the ID

**Issue:** `GET /orders/{order_id}` 401s (admin-key gated).

**Why:** Order IDs are small sequential ints with no other protection, so a
plain `GET /orders/{id}` is trivially enumerable - that route now requires
an admin key (`admin_auth.require_admin_key`). The real customer-facing
route is `GET /orders/customer/{order_id}?email=...`, which requires the
email the order was placed under as a second factor instead.

**Fix:** For a customer-facing check, hit `/orders/customer/{id}` with the
matching `email` query param. For an admin check, hit `/orders/{id}` with
an `X-Admin-Key` header.

### HeyGen integration is optional

**Issue:** If `HEYGAN_API_KEY` is not set, video generation fails silently.

**Why:** The imports are wrapped in try-except; missing `httpx` or missing API key doesn't crash the server.

**Fix:** Set `HEYGAN_API_KEY` in environment to enable video generation. Without it, orders stay in `video_generation_status: "pending"` forever.

### Orders persist across restarts now (SQLite by default)

Orders, subscriptions, trading bot state, and withdrawal requests are all
DB-backed (`models.py` / `database.py`), not in-memory lists - an earlier
version of this doc said otherwise. Locally this defaults to a SQLite file
at `./empire.db` in the repo root (no `DATABASE_URL` needed); set
`DATABASE_URL` to a real Postgres URL to match production. Delete
`empire.db` to reset local state between runs (e.g. if `create-order` keeps
incrementing order IDs and you want a clean slate).

### `python-binance` version pin can silently break the whole build

**Issue:** `pip install -r requirements.txt` fails entirely (not just the
optional crypto bot) with `No matching distribution found for
python-binance==X.Y.Z` if that pin doesn't exist on PyPI.

**Why:** `crypto_scalp_grid_bot.py`'s import is guarded in `main.py` (won't
crash the app if missing), but `requirements.txt` itself isn't - a bad pin
there fails the *entire* `pip install`, which fails the Railway build for
every feature, not just the crypto bot. This actually happened - a pin of
`python-binance==1.20.1` (a version that was never published; latest real
release is `1.0.37`) broke production builds until fixed.

**Fix:** `pip index versions python-binance` to check what's real before
pinning. If `pip install -r requirements.txt` fails on an unrelated-looking
package, check whether it's guarded in `main.py` but still hard-pinned in
`requirements.txt` - the guard doesn't help if the file itself won't install.

## Troubleshooting

| Error | Fix |
|-------|-----|
| `ModuleNotFoundError: No module named 'fastapi'` | Run `pip install -r requirements.txt` |
| `No matching distribution found for python-binance==...` | Bad version pin in `requirements.txt` - check `pip index versions python-binance` and fix the pin (see Gotchas). Blocks *all* deps from installing, not just the crypto bot. |
| `Address already in use: ('0.0.0.0', 8000)` | Another process is using port 8000. Run `pkill -f "python main.py"` or change PORT in driver. |
| `ConnectionRefused` on smoke tests, or on `health`/`quote-form`/`create-order` | Server isn't running. `health`/`quote-form`/`create-order` don't start one themselves - run `start` first (or use `test`, which is self-contained). |
| Stripe endpoint returns null publishable key | `STRIPE_PUBLISHABLE_KEY` env var not set. Set it or skip payment tests locally. |
| Quote form shows `Not Found` | The `quote_request.html` file may have been moved. Check `ls -la quote_request.html`. |
| `POST /orders/request-quote` → 404 | Single `/orders` prefix, not `/orders/orders` - that double prefix was removed when the route body changed to a Pydantic model. |
| `POST /orders/request-quote` → 422 | Send a JSON body (`Content-Type: application/json`), not query params. |
| `GET /orders/{id}` → 401 | That route is admin-key gated now. Use `GET /orders/customer/{id}?email=...` for the customer-facing lookup. |

## Code Navigation

- **main.py** — FastAPI app, guarded router imports (`routers_to_load` dict, each wrapped in try/except so one missing optional module doesn't crash the app), `routers_list` registration loop, background bot threads started at startup, core endpoints (`/health`, `/quote`, `/order-success`, `/trading-dashboard`)
- **routers/orders.py** — Order lifecycle: quote (JSON body, DB-backed) → Stripe checkout → payment webhook → video generation → customer portal (email-gated) / admin lookup (key-gated)
- **routers/trading_dashboard.py** — Real Alpaca account data + withdrawal-request log backing `trading_dashboard.html`; see the `verify` skill to drive this without hitting the real broker API
- **prop_bot.py** / **tradovate_bot.py** — Background trading bots (daemon threads, not HTTP routes) - see `.claude/skills/verify/SKILL.md`
- **models.py** / **database.py** — SQLAlchemy models and async engine; SQLite locally by default, Postgres via `DATABASE_URL`
- **heygan_integration.py** — HeyGen API wrapper (avatar/language mapping, video polling)
- **quote_request.html** — Frontend form with two-stage flow and Stripe.js integration
- **.claude/CLAUDE.md** — Full architecture and deployment guide

## References

- **Stripe API:** https://stripe.com/docs/api/checkout/sessions
- **HeyGen API:** https://docs.heygen.com/
- **FastAPI:** http://localhost:8000/docs (when running)
- **Railway deployment:** https://railway.app
