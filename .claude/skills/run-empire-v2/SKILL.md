# Run Empire v2

---

name: run-empire-v2
description: Launch and test the FastAPI video production platform locally
type: project-skill

---

## Overview

Empire v2 is a FastAPI backend serving a video production SaaS platform: quote form → Stripe payment → HeyGen video generation → customer delivery.

The driver is a Node.js script (`.claude/skills/run-empire-v2/driver.mjs`) that launches the Python server on port 8000, verifies it's ready, and runs smoke tests on key endpoints (health check, quote form, order creation, order status).

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
   - Create an order via API (`POST /orders/orders/request-quote`)
   - Retrieve order status (`GET /orders/orders/{id}`)
4. Print results (pass/fail count)
5. Stop the server and exit

**Output:**
```
Starting FastAPI server on port 8000...
✓ Router loaded: /auth
✓ Router loaded: /orders
... more routers ...
✓ Server ready

📊 Running smoke tests...

✓ Health Check
✓ Quote Form HTML: HTML form loaded
✓ Create Order (Request Quote): Order #1 created (750 cents)
✓ Get Order Status: Order #1 retrieved

📈 Results: 4 passed, 0 failed
```

### Driver Commands

The driver supports several commands:

| Command | What it does |
|---------|------------|
| `node driver.mjs start` | Launch server, stay running (Ctrl+C to stop) |
| `node driver.mjs test` | Launch, run smoke tests, stop |
| `node driver.mjs health` | Show health endpoint response |
| `node driver.mjs quote-form` | Check if quote form HTML loads |
| `node driver.mjs create-order` | Create a test order and show response |
| `node driver.mjs stop` | Kill any running server process |

## Run (Human Path)

To develop interactively:

```bash
# Terminal 1: Start the server
python main.py

# Terminal 2: Test in another shell
curl http://localhost:8000/health
curl http://localhost:8000/quote
curl -X POST "http://localhost:8000/orders/orders/request-quote?customer_name=John&customer_email=test@example.com&customer_company=TestCo&video_type=explainer&script_or_topic=test&target_audience=business&avatar=anna&language=english_us"

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
# Run the driver smoke tests (recommended)
node .claude/skills/run-empire-v2/driver.mjs test

# Or run the Python test suite if it exists
pytest tests/ -v  # (if tests/ directory exists)
```

## Gotchas

### Quote form returns 404 on Railway

**Issue:** The quote form endpoint works locally but returns 404 when deployed to Railway.

**Cause:** Railway's working directory varies between deployments, affecting where `quote_request.html` is resolved.

**Fix:** The `/quote` endpoint tries three file paths:
1. `os.path.dirname(os.path.abspath(__file__))/quote_request.html`
2. `/app/quote_request.html`
3. `quote_request.html`

This handles most Railway variations. If the form still fails after deployment, check the Railway container's working directory in logs.

### Order endpoints use double `/orders` prefix locally

**Issue:** The routes in `routers/orders.py` define full paths like `/orders/request-quote`, and the router is registered at `/orders` prefix in `main.py`, resulting in `/orders/orders/request-quote`.

**Why it's like this:** This is intentional — the router registration can be changed if routes are moved to relative paths (e.g., `/request-quote`), but current code works as-is.

**Workaround:** Use `/orders/orders/*` for testing locally. This quirk is harmless.

### HeyGen integration is optional

**Issue:** If `HEYGAN_API_KEY` is not set, video generation fails silently.

**Why:** The imports are wrapped in try-except; missing `httpx` or missing API key doesn't crash the server.

**Fix:** Set `HEYGAN_API_KEY` in environment to enable video generation. Without it, orders stay in `video_generation_status: "pending"` forever.

### In-memory order storage (development only)

**Issue:** When the server restarts, all pending orders are lost.

**Why:** Orders are stored in Python lists (`pending_orders`, `completed_orders`) that live only as long as the process.

**Fix for production:** Migrate to persistent database (PostgreSQL/SQLite on disk). For local development, this is fine.

## Troubleshooting

| Error | Fix |
|-------|-----|
| `ModuleNotFoundError: No module named 'fastapi'` | Run `pip install -r requirements.txt` |
| `Address already in use: ('0.0.0.0', 8000)` | Another process is using port 8000. Run `pkill -f "python main.py"` or change PORT in driver. |
| `ConnectionRefused` on smoke tests | Server didn't start. Check `python main.py` output for errors (missing imports, etc). |
| Stripe endpoint returns null publishable key | `STRIPE_PUBLISHABLE_KEY` env var not set. Set it or skip payment tests locally. |
| Quote form shows `Not Found` | The `quote_request.html` file may have been moved. Check `ls -la quote_request.html`. |
| `/orders/*` endpoints return 404 | Use `/orders/orders/*` (the double prefix is correct due to router registration). |

## Code Navigation

- **main.py** — FastAPI app, lifespan, router registration, core endpoints (`/health`, `/quote`, `/order-success`)
- **routers/orders.py** — Order lifecycle: quote → Stripe checkout → payment webhook → video generation
- **heygan_integration.py** — HeyGen API wrapper (avatar/language mapping, video polling)
- **quote_request.html** — Frontend form with two-stage flow and Stripe.js integration
- **.claude/CLAUDE.md** — Full architecture and deployment guide

## References

- **Stripe API:** https://stripe.com/docs/api/checkout/sessions
- **HeyGen API:** https://docs.heygen.com/
- **FastAPI:** http://localhost:8000/docs (when running)
- **Railway deployment:** https://railway.app
