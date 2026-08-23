# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Empire v2** is a multi-system SaaS platform combining:
1. **Video Production Service** — Stripe-powered quote form → HeyGen video generation → customer delivery
2. **Trading Automation** (secondary) — Futures/crypto trading with AI signal confirmation
3. **Content/Revenue Systems** — Email campaigns, YouTube publishing, data retention

**Current Focus:** Video production platform (quote form → Stripe payment → HeyGen generation).

**Deployment:** Railway (https://empire-v2-production.up.railway.app)

---

## Architecture

### Core Stack
- **Framework:** FastAPI (async, graceful error handling)
- **Database:** SQLite/PostgreSQL via async SQLAlchemy (persistent order storage)
- **Payments:** Stripe (checkout sessions, webhooks)
- **Video Generation:** HeyGen API (avatar + voice synthesis)
- **Email:** Gmail SMTP (configured via env vars)
- **Monitoring:** Health monitor + data retention manager

### Main Entry Point
**main.py** (17,648 bytes)
- FastAPI app initialization with CORS
- Lifespan context manager for startup/shutdown
- Router registration (auth, workers, clients, jobs, bookings, payments, admin, orders, revenue, social)
- Critical endpoints:
  - `GET /quote` — serves quote_request.html form (reads file, returns HTMLResponse)
  - `GET /health` — deployment health check
  - `GET /order-success` — Stripe success redirect
  - `GET /monitor/*` — monitoring endpoints
  - `GET /retention/*` — data retention status

**Key Pattern:** Routers imported with try-except to prevent crashes if modules missing. Missing routers log warnings but don't stop startup.

### Order/Video Generation Flow
**routers/orders.py** (23,525 bytes) — main business logic

1. **POST /orders/request-quote** — Customer submits video request
   - Accepts: customer info, video type, script, avatar, language, delivery timeline
   - Avatar: 8 options (Anna, Carlos, Emma, James, Lisa, Marcus, Olivia, Ryan)
   - Language: 22 options (English US/UK/AU, Spanish, French, German, Italian, Portuguese, Dutch, Swedish, Norwegian, Danish, Polish, Russian, Japanese, Korean, Chinese Simplified/Traditional, Arabic, Hindi)
   - Returns: order_id + quote_price
   - Persists order to database via SQLAlchemy ORM

2. **POST /orders/{order_id}/create-checkout** — Stripe session creation
   - Uses stored order data to create Stripe checkout session
   - Returns: session_id for redirectToCheckout()

3. **POST /orders/webhook/stripe** — Payment confirmation webhook
   - Validates webhook signature
   - Marks order as paid
   - Triggers `generate_video_for_order()` background task (if HeyGen available)

4. **Async generate_video_for_order()** — Background video generation
   - Calls HeyGen API with: script, avatar (mapped to HeyGen ID), language (mapped to voice settings)
   - Polls HeyGen API every 10 seconds for up to 10 minutes
   - On completion: stores video_url, sends email to customer
   - On timeout/error: sets status accordingly

5. **GET /orders/customer/{order_id}** — Customer portal
   - Displays order status, video download link (if ready)

6. **GET /orders/admin-dashboard** — Admin video tracking
   - Shows all orders with generation status

### Frontend Form
**quote_request.html** (21,646 bytes)
- Beautiful purple gradient UI
- Two-stage flow: Get Quote → Accept & Pay Now
- Form fields: name, email, company, phone, video type, script, target audience, avatar, language, delivery timeline
- Dynamic pricing calculator
- Stripe.js integration (calls POST /orders/request-quote, then creates checkout session)
- All form data serialized to JSON and sent to backend

### Supporting Systems

**heygan_integration.py** (5,083 bytes)
- `async generate_video()` — calls HeyGen API v1/videos/generate
- `async get_video_url()` — polls HeyGen API for completion
- Avatar/language mapping tables (convert user-friendly names to HeyGen format)

**health_monitor.py** (17,248 bytes)
- Monitors all systems continuously
- Tracks errors, fixed issues, performance metrics
- Stores in permanent archive tables

**data_retention.py** (12,265 bytes)
- Archives old data to permanent storage
- Keeps all data forever (non-deletion retention policy)

**database.py**
- Async SQLAlchemy engine with connection pooling
- Supports SQLite (local) and PostgreSQL (production) with asyncpg driver
- `AsyncSessionLocal()` factory for background tasks
- `Depends(get_db)` dependency for request handlers

---

## Development & Deployment

### Local Testing

**Setup:**
```bash
pip install -r requirements.txt
```

**Environment Variables** (create .env or set in Railway):
```
STRIPE_SECRET_KEY=sk_...
STRIPE_PUBLISHABLE_KEY=pk_...
STRIPE_WEBHOOK_SECRET=whsec_...
HEYGAN_API_KEY=...
GMAIL_EMAIL=... (optional)
GMAIL_PASSWORD=... (optional)
```

**Run locally:**
```bash
python main.py
# Runs on http://localhost:8000
# /docs for interactive API docs
# /quote for quote form
```

**Test quote form:**
1. Visit http://localhost:8000/quote
2. Fill form (all fields required except phone, reference URL)
3. Click "Get My Quote"
4. Click "Accept & Pay Now"
5. Stripe checkout redirect (uses test/live keys based on env)

### Deployment to Railway

**Branch:** `claude/video-editing-platform-ib585z`

**Deploy steps:**
1. Push code: `git push -u origin claude/video-editing-platform-ib585z`
2. Go to Railway dashboard → empire-v2 deployment → click Redeploy
3. Wait 2-3 minutes for "Active" status
4. Test: `https://empire-v2-production.up.railway.app/quote`

**Key deployment files:**
- **Dockerfile** — Python 3.11-slim, copies all files, runs `python main.py`
- **railway.json** — defines main-app service + other video services
- **.env vars on Railway** — STRIPE_SECRET_KEY, STRIPE_PUBLISHABLE_KEY, STRIPE_WEBHOOK_SECRET, HEYGAN_API_KEY (no Gmail configured yet)

**Critical Fix Applied:** `/quote` endpoint tries multiple file paths to find quote_request.html (handles Railway's working directory variations):
```python
possible_paths = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "quote_request.html"),
    "/app/quote_request.html",
    "quote_request.html",
]
```

### Git Workflow

- **Development branch:** `claude/usa-empire-v2-setup-01hmw8` (all changes pushed here)
- **Production branch:** `main` (do NOT push without explicit permission)
- **Commits:** Create new commits (don't amend) with clear messages
- **Deployment:** Push to main branch, Railway auto-deploys to https://empire-v2-production.up.railway.app

---

## Critical Implementation Details

### Order Storage
- **Database:** All orders persisted in `video_quote_orders` table via async SQLAlchemy ORM
- **Connection:** Uses `Depends(get_db)` for request-scoped sessions, `AsyncSessionLocal()` for background tasks
- **Databases supported:** SQLite (local development, default: empire.db), PostgreSQL with asyncpg (Railway production)
- Order object schema (VideoQuoteOrder model):
  ```python
  {
    "id": int,
    "status": "quote_requested|payment_received|video_ready|...",
    "customer_name/email/company/phone": str,
    "video_type": str,
    "script_or_topic": str,
    "target_audience": str,
    "avatar": str,  # e.g., "anna"
    "language": str,  # e.g., "english_us"
    "delivery_days": int,
    "quote_price": int (cents),
    "paid": bool,
    "stripe_session_id": str,
    "video_generation_status": "pending|generating|completed|failed|timeout",
    "video_url": str,
    ...
  }
  ```

### HeyGen Integration
- Wraps imports in try-except with `HEYGAN_AVAILABLE` flag (app won't crash if httpx missing)
- Requires `HEYGAN_API_KEY` env var
- Maps user avatars to HeyGen IDs: anna → anna_public_ca_en, etc.
- Maps language codes to HeyGen voice format (language + accent)
- Timeout: 10 minutes max polling (60 attempts × 10 sec)

### Stripe Integration
- Webhook endpoint: `POST /orders/webhook/stripe`
- **Critical:** Validates webhook signature with STRIPE_WEBHOOK_SECRET
- Webhook URL configured in Stripe dashboard: https://empire-v2-production.up.railway.app/orders/webhook/stripe
- Metadata: order_id, customer_email, customer_name (passed to HeyGen)

### Email Notifications
- **Status:** Optional (app works without Gmail credentials)
- **Trigger:** When video_generation_status == "completed"
- **Recipient:** customer_email
- **Env vars:** GMAIL_EMAIL, GMAIL_PASSWORD (App password, not regular password)
- Requires Gmail "App passwords" feature (may not be available on Google Workspace accounts)

---

## Verification Before Deployment

Before pushing changes to Railway, verify:

1. **Python syntax:** `python3 -m py_compile main.py routers/orders.py heygan_integration.py`
2. **Imports:** `python3 -c "from heygan_integration import generate_video; print('OK')"`
3. **File presence:** `test -f quote_request.html && echo "Form exists"`
4. **Endpoint logic:** Check main.py /quote endpoint reads HTML correctly
5. **Git status:** `git status` — ensure no uncommitted changes before deployment

---

## Railway Network Configuration (Broker API Access)

### Problem: Broker APIs Blocked (403 Forbidden)

When trading bots run on Railway, external API calls may fail with:
```
HTTP 403: Host not in allowlist: api.coinbase.com
Add this host to your network egress settings to allow access
```

This happens because Railway blocks outbound connections by default for security. Brokers (Coinbase, Alpaca) need explicit whitelist entries.

### Solution A: Configure Egress Allowlist (Permanent)

1. **Go to Railway Dashboard:**
   - https://railway.app/dashboard
   - Select `empire-v2` project
   - Click `main-app` service

2. **Add Network Egress Rules:**
   - Find **Settings** → **Network** (or **Policies** section)
   - Add these hosts to the **Egress Allowlist:**
     ```
     api.coinbase.com
     *.coinbase.com
     api.alpaca.markets
     alpaca.com
     api.polygon.io
     data.alpaca.markets
     stripe.com
     ```

3. **Save and Redeploy:**
   - Apply changes
   - Push code or click "Redeploy" in Railway dashboard
   - Wait 2-3 minutes for container restart

4. **Verify in Logs:**
   ```
   tail -f /tmp/empire-server.log | grep -E "COINBASE|ALPACA|balance|price fetch"
   ```
   - ✓ If successful, you'll see:
     ```
     INFO:crypto_coinbase_bot:Coinbase USD balance: $700.00
     INFO:crypto_coinbase_bot:[CRYPTO] Scanning BTC/USD, ETH/USD...
     ```
   - ✗ If still blocked, you'll see:
     ```
     WARNING:crypto_coinbase_bot:HTTP 403: Host not in allowlist
     ```

### Solution B: Deploy Network Workaround (Temporary)

**While waiting for Railway egress whitelist,** use the built-in retry/cache layer.

See **NETWORK_WORKAROUND.md** for:
- Step-by-step setup (5 minutes)
- Environment variable configuration
- How retry logic + caching work
- Fallback behavior when API unavailable
- Troubleshooting guide

Quick start:
```bash
# Set these env vars on Railway:
NETWORK_RETRY_ATTEMPTS=5
NETWORK_CACHE_TTL=600
CRYPTO_BOT_SKIP_ENTRIES_ON_API_FAILURE=true
FALLBACK_PRICE_MODE=skip

# Then redeploy
git push -u origin claude/usa-empire-v2-setup-01hmw8
```

Bot will then:
- ✓ Use cached API responses (5-10 min TTL)
- ✓ Retry connection errors with backoff
- ✓ Skip entries if price data unavailable
- ✓ Continue closing open positions normally

### Affected Bots

| Bot | Broker | Endpoints | Status |
|-----|--------|-----------|--------|
| `crypto_coinbase_bot.py` | Coinbase Advanced Trade | `api.coinbase.com` | Needs egress whitelist |
| `prop_bot.py` | Apex Futures (Prop Trading) | `api.alpaca.markets`, `data.alpaca.markets` | Needs egress whitelist |

### Environment Variables (Optional)

If Railway uses environment-based policy, set:
```
ALLOWED_EGRESS_HOSTS=api.coinbase.com,api.alpaca.markets,stripe.com,polygon.io
```

But the dashboard Settings approach (step 1-2 above) is the primary mechanism.

---

## Crypto Family Tree Bot (crypto_family_tree_bot.py)

Real-money Coinbase crypto trading system, separate from the older
`crypto_coinbase_bot.py`. Each "branch" is an independently-running
thread that trades one coin at a time, tracked as a `CryptoTreeBranch`
row (bot_name, product_id, allocated_usd, equity_floor, next_unlock_tier)
plus a `BotPosition` row while holding. The root branch (`crypto_btc_compound`,
always BTC-USD) is the permanent foundation the tree grows from; every
other branch can switch coins on exit and can spawn a $50-seed child
once it crosses its `next_unlock_tier`. Shared buy/sell/fee/target math
lives in `crypto_btc_compound_bot.py` (imported as `engine` by the
family-tree module, not duplicated).

Dashboard: `family_tree_dashboard.html`, served at `/family-tree-dashboard`,
gated by `X-Admin-Key` on the `/api/trading-dashboard/family-tree-status`
endpoint it calls (see `routers/trading_dashboard.py`).

### Real-money protections (per-branch)

- **-2% stop-loss** (`STOP_LOSS_PCT`) on every position - the hard floor
  on any single trade.
- **Breakeven ratchet** (`BREAKEVEN_TRIGGER_PCT`, 1%): once a position
  has ever been up 1%, its stop is raised to entry price - it can no
  longer close for a real loss beyond the round-trip fee, from that
  point on.
- **Peak-profit giveback cap** (`MAX_PROFIT_GIVEBACK_USD`, $3.75
  default): once a position has shown real profit, if it gives back
  more than that many dollars from its best point, it force-exits to
  lock in what's left - independent of the fixed target/stop.
- **Volatility-tiered minimum-profit floor** (`MIN_PROFIT_USD_LOW/MED/HIGH`,
  $2.50/$4.00/$6.00 by ATR band): a TARGET exit has to clear real fees
  plus this floor, not just barely break even - `min_profit_target_pct()`
  in `crypto_btc_compound_bot.py`.
- **Equity floor ratchet** (`BRANCH_FLOOR_TIER`, $50 steps): each
  branch's floor only ever ratchets UP as it earns real gains. Two real
  bugs in this mechanism were found and fixed in production (see below).
- **Floor-breach cooldown** (`FLOOR_BREACH_COOLDOWN_SECONDS`, 30 min):
  after a floor-breach forced exit, the branch stays in cash for a real
  cooldown instead of instantly rebuying into a new coin (the original
  bug this fixed: AAVE → STOP HIT → instant rebuy XRP → breach again →
  instant rebuy BONK, three real losses in a row).
- **Contestable strongest-sibling coin lock** (the "throne" model,
  `COIN_LOCK_KEY_PREFIX`): among 2+ siblings under the same parent, the
  one with the current highest `allocated_usd` holds its coin instead of
  coin-switching on exit - but loses that lock the instant a different
  sibling in the group grows bigger (checked every coordinator scan).
  Root (BTC) is always exempt - it never switches coins, contestable or
  not.
- **Coin selection**: `find_most_volatile_unclaimed_coin()` picks the
  highest-ATR coin among unclaimed candidates that's also "bullish"
  (price now higher than ~25 hours ago). This is a coarse, medium-term
  check with **no short-term overbought/extended signal** (unlike
  `prop_bot.py`'s RSI < 30 entry filter on the Alpaca side) - a real,
  known gap, not yet fixed. See the coin-selection backtest section
  below - candidates are now also filtered through a real exclusion
  list before this function even runs.
- **Manual controls on the dashboard**: a "Sell now" button per branch,
  only enabled when the position is genuinely in profit right now
  (re-checked server-side too, so it can't be bypassed by calling the
  API directly) - selling behind entry is refused. A "Start new $50
  branch" button, greyed out automatically unless there's real
  unallocated cash to fund it (`spendable_for_spawn`/`can_spawn` in the
  `/family-tree-status` response - see the spawn-branch bug below for
  why this calculation is non-trivial). Each open position also shows a
  live "Profit" ticker and a "Fee to enter" estimate (Coinbase's real
  trading fee, otherwise invisible - baked into the position's qty, not
  shown as its own line item anywhere else).

### Two real production bugs found and fixed (2026-08-22/23)

1. **Manual spawn-branch affordability bug**: the endpoint computed
   "real unallocated cash" by subtracting *every* branch's
   `allocated_usd` from the real Coinbase cash balance - but
   `get_usd_balance()` is cash-only and doesn't include money currently
   deployed in an open position. With most branches holding positions,
   this produced nonsense negative "free cash" figures (e.g. real
   balance $254.21, computed as -$267.06 free) and blocked spawns that
   were actually affordable. Fixed: only subtract **flat** branches'
   `allocated_usd` (the only ones actually competing for the shared cash
   pool).

2. **Stuck-flat-branch floor freeze**: a branch could end up flat with
   its real balance below its own ratcheted floor (root cause:
   `_maybe_spawn_child()` pulls the $50 seed for a new child right after
   the floor was ratcheted up to match the *pre-spawn* balance, leaving
   the parent flat and below its own now-stale floor) - and since a flat
   branch can only raise its balance BY trading, and trading is exactly
   what "below floor" blocks, this was a **permanent stall**, not a
   pause. Confirmed live: `crypto_tree_ldo_usd` sat at $155.05 vs. a
   $200.00 floor, logging "entries paused until it recovers" every cycle
   for 11+ minutes straight. Fixed: a flat branch found below its own
   floor now self-heals the floor down to match its real balance's tier
   immediately, then resumes trading the next cycle - self-heals ANY
   branch in this state automatically, not just a one-off patch.

   A related fix in the same area: **EQUITY FLOOR BREACH no longer
   overrides a healthy held position.** The floor-breach check used to
   run *before* the target/stop/breakeven/giveback logic and force-sold
   a held position unconditionally whenever branch equity dropped below
   its floor - even if that specific position was still above its own
   stop (possibly already breakeven-protected). Now it only force-sells
   via the floor-breach path when the position's own stop has *also*
   already failed; otherwise the position is left to run under its own
   protection, and the branch-level breach only continues to block new
   entries elsewhere.

All of the above were verified against reproductions of the exact real
numbers from production logs/screenshots before shipping, plus a full
offline regression suite (not committed to the repo - built and run
from the session scratchpad each time).

### Coin-selection backtest and exclusion (crypto_selection_backtest.py)

Built to test whether the 25-hour "bullish" coin-selection check (above)
is actually buying at bad moments: pulls each family-tree coin's real
historical Coinbase hourly candles and replays the bot's own real
target/stop/breakeven/giveback rules (importing the live functions
directly, not reimplementing them) to rank coins by real backtested ROI.
The backtest run itself never places an order - but its *results* now
feed a real coin-exclusion system that DOES change what the live bot
buys (see below), so this is no longer purely a shadow-mode diagnostic.

- Manual run: `POST /api/trading-dashboard/crypto-selection-backtest`
  (admin-key gated, ~30-90s - pulls ~27 coins' history concurrently from
  Coinbase's public candles endpoint) and its viewer,
  `/crypto-selection-backtest-view` (reuses the same saved admin key as
  the family tree dashboard).
- Requires outbound access to `api.exchange.coinbase.com` - works from
  Railway (the live bot already depends on this exact host every cycle)
  but may be blocked from a locked-down dev sandbox.
- Every run (manual button-press or the scheduled automatic one below)
  persists each coin's result to the `crypto_backtest_runs` table
  (`CryptoBacktestRun` model) - this history is what the automatic
  exclusion rule reads.
- Next phase (not started): the same idea for the Alpaca/stock side,
  which needs different historical data access and a different exit-rule
  set (`alpaca_mean_reversion.py`'s `should_exit_position`).

**Two-layer coin exclusion** (`get_effective_excluded_coins()` in
`crypto_family_tree_bot.py`, checked by both `find_most_volatile_unclaimed_coin()`
and `get_next_eligible_product_id()` before either ever runs):

1. **`MANUAL_EXCLUDED_COINS`** - `{STX-USD, BLUR-USD, UNI-USD, DOT-USD}`,
   set once by the account owner's explicit real-money decision after
   the first backtest run (STX-USD was dead last: -44.1% ROI, 21.6% win
   rate). Permanent - the automatic rule below never touches this set;
   only another explicit decision like this one can.
2. **Automatic layer** - per the account owner's explicit choice
   ("fully automatic... hands-off, no check before it takes effect"),
   the coordinator (`run()`'s `_scan()`) re-runs the real backtest on
   its own every `AUTO_BACKTEST_INTERVAL_SECONDS` (24h default) via
   `_run_scheduled_backtest_and_update_exclusions()`. A coin is
   auto-excluded once its last `AUTO_EXCLUDE_RUN_WINDOW` (3) real runs
   were ALL negative-ROI, and un-excluded the instant its most recent
   run turns positive - contestable/self-healing, same philosophy as
   the strongest-sibling throne and the floor self-heal, never a
   one-way verdict. Requiring several consecutive bad runs (not one) is
   deliberate: DOT-USD and UNI-USD swung from -38.8%/-40.0% to
   -14.2%/-12.4% in the same afternoon in real testing - a single run
   is too noisy to act on alone.

A branch already holding a coin at the moment it becomes excluded
(manually or automatically) is never force-sold - it keeps running
under its own normal rules and simply won't be offered that coin again
once it exits.

**Third real production bug found and fixed**: the strongest-sibling
throne lock (`_is_coin_locked`) short-circuits `_branch_sell_and_settle`'s
coin-switch decision *before* it ever reaches the exclusion check -
confirmed live: `crypto_tree_ldo_usd` held the throne while sitting on
newly-excluded BLUR-USD, and a manual sell sold BLUR and instantly
rebought the identical excluded coin, because the throne lock never
even looked at the exclusion list. Fixed: the throne lock no longer
applies when the branch's current coin is on the (combined manual +
automatic) exclusion set - it still holds for every other exit exactly
as before.

### Alpaca (prop_bot.py) parity

`alpaca_mean_reversion.py`'s `should_exit_position()` carries the same
two protections as the crypto side: a breakeven ratchet (`+1%` trigger,
percentage-based to match Alpaca's existing convention) and a
peak-profit giveback cap (`0.5%`). Return signature is a 4-tuple
`(should_exit, reason, exit_type, new_peak_pnl_pct)` - the 4th value
must be persisted by the caller (`prop_bot.py` does this via
`_db_update_peak_pct`, storing into `BotPosition.peak_pct`).

---

## Common Tasks

**Add new video type:**
- Update `quote_request.html` — add `<option>` in videoType select
- Update `baseVideoPrices` object in script (JavaScript pricing calculator)
- Update backend pricing function if logic changes

**Add new avatar:**
- Update `quote_request.html` — add `<option>` in avatar select
- Update `heygan_integration.py` — add to AVATAR_MAP dictionary
- Map to actual HeyGen avatar ID

**Add new language:**
- Update `quote_request.html` — add `<option>` in language select  
- Update `heygan_integration.py` — add to VOICE_MAP dictionary
- Include language name and accent for HeyGen API

**Test payment flow end-to-end:**
1. POST to /orders/request-quote with all required fields
2. Capture order_id from response
3. POST to /orders/{order_id}/create-checkout to get Stripe session_id
4. Simulate Stripe webhook: POST to /orders/webhook/stripe with valid signature
5. Check /orders/admin-dashboard for video generation status

**Monitor deployment:**
- https://empire-v2-production.up.railway.app/health — returns {"status": "ok"}
- https://empire-v2-production.up.railway.app/monitor/status — health monitor status
- https://empire-v2-production.up.railway.app/monitor/errors — error history
- Railway deploy logs show startup sequence

---

## Known Limitations & TODOs

- **Email:** Gmail not configured (skip for now, test with HeyGen generation only)
- **Video editing:** HeyGen only generates new videos; can't modify existing videos per user request
- **Admin auth:** No authentication on admin endpoints yet (add before production)
- **Database:** PostgreSQL recommended for production (Railway plugin auto-configures with asyncpg driver)

---

## Troubleshooting

### Trading Bot Issues

**Symptom:** Crypto/Alpaca bot shows "Equity: unknown" or "Cash available: unknown"

**Causes:**
1. Broker API credentials not configured (check env vars)
2. Network egress blocked (see Railway Network Configuration above)
3. API key has wrong permissions/scope
4. Broker account not in live/paper mode

**Solution:**
```bash
# Check env vars are set
echo "COINBASE_API_KEY_NAME: ${COINBASE_API_KEY_NAME:-NOT SET}"
echo "COINBASE_API_PRIVATE_KEY: ${COINBASE_API_PRIVATE_KEY:-NOT SET}"
echo "ALPACA_API_KEY: ${ALPACA_API_KEY:-NOT SET}"
echo "ALPACA_BASE_URL: ${ALPACA_BASE_URL:-NOT SET}"

# Check logs for connectivity errors
curl http://localhost:8000/health  # Should return {"status": "ok"}
tail -50 /tmp/empire-server.log | grep -i "error\|failed\|403"
```

---

**Symptom:** "coinbase price fetch failed (need 50+ candles)" for all symbols

**Cause:** Price feed not accumulating history (bot just started, or data stale)

**Solution:**
- Bot needs ~50 15-minute candles per symbol (≈12 hours of data)
- Wait for candles to accumulate, then entries resume
- Or verify `/crypto/analytics/health` endpoint shows instrumentation active

---

**Symptom:** "Cash pool $0.00 below minimum trade size" for crypto bot

**Cause:** Coinbase USD balance read failed, so cash = $0 → no trades allowed

**Solution:**
1. Verify Coinbase account has USD balance > $5
2. Check API credentials have "read" permission on balances
3. Confirm network egress allows `api.coinbase.com`
4. Restart bot: `pkill -f "python main.py"` then restart

---

### Payment/Revenue System Issues

**Symptom:** Bot earnings showing `total_earned: 0.0` but jobs completed

**Cause:** Payment records not created, or Payment table out of sync with Job table

**Solution:**
```python
# Check if bot worker exists
python3 << 'EOF'
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from models import Worker, Payment, Job
import asyncio

async def check():
    engine = create_async_engine("sqlite+aiosqlite:///empire.db")
    async_session = sessionmaker(engine, class_=AsyncSession)
    async with async_session() as session:
        result = await session.execute(select(Worker).where(Worker.email == "bot@pgusa.local"))
        bot = result.scalar_one_or_none()
        if not bot:
            print("❌ Bot worker not found - create it first")
            return
        print(f"✓ Bot worker ID: {bot.id}")
        
        # Check payments
        p_result = await session.execute(select(Payment).where(Payment.worker_id == str(bot.id)))
        payments = p_result.scalars().all()
        print(f"✓ Payments for bot: {len(payments)}")
    await engine.dispose()

asyncio.run(check())
EOF
```

---

**Symptom:** Payments stuck in "processing" status for >24 hours

**Cause:** Payout system encountered an error; manual intervention needed

**Solution:**
1. Check `/payments/bot/earnings` → look for `payout_status: "processing"`
2. Verify Stripe payout was actually initiated: `stripe_payout_id` should be non-null
3. If stuck, manually mark as paid in database (admin endpoint):
   ```python
   # In database console:
   UPDATE payments SET payout_status = 'paid', paid_at = datetime('now') 
   WHERE id = '<payment_id>' AND payout_status = 'processing';
   ```
4. Check Stripe dashboard for failed payouts (may need manual retry)

---

### Database & Deployment Issues

**Symptom:** SQLite database locked: "database is locked"

**Cause:** Multiple processes writing simultaneously (Railway restart while bot still running)

**Solution:**
- This is rare with async SQLAlchemy, but if it happens:
  1. Kill all Python processes: `pkill -f python`
  2. Delete lock files: `rm -f empire.db-*`
  3. Restart: `python main.py`

---

**Symptom:** On Railway: Quote form returns 404 for `/quote`

**Cause:** `quote_request.html` file path not found (working directory mismatch)

**Solution:**
- Already fixed in `/quote` endpoint with fallback paths:
  1. `/app/quote_request.html` (Railway container root)
  2. `quote_request.html` (repo root)
  3. `os.path.dirname(__file__)/quote_request.html` (module-relative)
- If still fails: Check Railway logs for which path was attempted

---

### Monitoring & Analytics

**Symptom:** `/crypto/analytics/metrics/summary` returns all zeros for metrics

**Cause:** No trade logs created yet, or wrong `strategy_version` filter

**Solution:**
```bash
# Check trade log records exist
curl http://localhost:8000/crypto/analytics/health
# Should show instrumentation list

# Check raw logs
curl http://localhost:8000/crypto/analytics/trades/recent
# Should show trade entries if any exist
```

---

## References

- **API Endpoints:** See API_ENDPOINTS.md
- **Stripe docs:** https://stripe.com/docs/api/checkout/sessions
- **HeyGen docs:** https://docs.heygen.com/
- **FastAPI docs:** http://localhost:8000/docs (when running locally)
