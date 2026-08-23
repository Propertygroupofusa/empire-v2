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
- **Floor-breach cooldown** (`FLOOR_BREACH_COOLDOWN_SECONDS`, 5 min -
  lowered from the original 30 min per the account owner's explicit
  choice, after being told what it protects against): after a
  floor-breach forced exit, the branch stays in cash for a real cooldown
  instead of instantly rebuying into a new coin (the original bug this
  fixed: AAVE → STOP HIT → instant rebuy XRP → breach again → instant
  rebuy BONK, three real losses in a row). Still real protection at 5
  min, just thinner than the original 30 - back to trading faster, at
  the cost of somewhat less cushion before it can risk hitting the same
  wall again.
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

- **"Trade this" button on the backtest page**
  (`crypto_selection_backtest.html`, `POST
  /api/trading-dashboard/family-tree-status/spawn-branch/{product_id}`):
  per the account owner's explicit request, lets them act directly on a
  coin that ranks well in the backtest (e.g. DOGE-USD/XRP-USD) instead of
  only linking back to the dashboard. Starts a real $50 branch on exactly
  that coin, funded from the same real-unallocated-cash pool as the
  existing auto-pick "Start new $50 branch" endpoint - refuses with a
  clear reason if the coin isn't one the bot trades, is currently
  excluded (manual or auto layer), is already claimed by another branch,
  or there isn't enough real free cash. This is a real trade, not a
  simulation - it's the same $50-seed/root-child mechanism every organic
  or auto-spawned branch uses.

### Manual unlock of locked profit

Per the account owner's explicit request: locked profit (the 10% skim +
the stranded-dust sweep, both above) is deliberately one-way everywhere
else in this system - "permanently out of the compounding loop." This
adds the one and only manual override, via the dashboard's 🔓 Unlock
button on the Locked Profit card and `POST
/api/trading-dashboard/family-tree-status/unlock-profit`
(`{amount, bot_name?}`):

- `bot_name` omitted → **cash out**: `locked_usd` drops by the real
  amount, immediately available again as free spendable cash to
  whichever branch's own cycle next wants to buy (or withdrawable
  directly from Coinbase by the account owner - it was always real
  money in the account, just virtually earmarked).
- `bot_name` given → **add to branch**: same `locked_usd` decrease, plus
  that exact amount added directly into the named branch's
  `allocated_usd` - a pure bookkeeping transfer (no Coinbase order,
  real dollars never left the account), same mechanism a spawn's
  parent-deduct/child-add uses. No restriction on which branch - winning
  or losing, any existing branch - per the account owner's explicit
  choice ("all can be an option").

Refuses cleanly (400) if the amount isn't positive or exceeds what's
actually locked, and (404) if the named branch doesn't exist -
`locked_usd` is left untouched on any refusal. `_subtract_locked_usd()`
in `crypto_family_tree_bot.py` is the reverse of the existing
`_add_locked_usd()`, clamped so it can never release more than genuinely
exists.

The dashboard UI is a real tappable modal (amount field, a cash-out/
add-to-branch toggle, and - when adding to a branch - a tappable list of
real branches that highlights on selection), not a chain of browser
`prompt()` dialogs - per the account owner's explicit follow-up that
typing an exact `bot_name` into a text prompt was too fiddly on mobile.

### Alpaca-side unlock: cash-out only, no "add to a bucket"

`alpaca_dashboard.html` has the same 🔓 Unlock button and
`POST /api/trading-dashboard/alpaca-overview/unlock-profit`
(`{amount}`) - but per the account owner's own explicit reasoning,
**cash-out only, no "add to a bucket" mode**. The two locked-profit
systems aren't the same shape under the hood: crypto branches are
independent principal pools, so adding unlocked money to one is a real,
meaningful bookkeeping transfer. The 8 `bot_N` buckets here are instead
proportional *shares* of one real Alpaca equity - `_rebalance_bots()`
re-derives every bucket's share from the real account balance on every
single load, so manually bumping one bucket's `base_capital` would just
get silently smeared back across all 8 on the very next refresh. There
was nothing meaningful an "add to a bucket" action could actually do
here, so it was never built - this also means Alpaca's `locked_usd` is
purely a tracked number today, not real protection the way crypto's is
(the bot buckets already rebalance against the FULL real equity,
`locked_usd` included - a real difference from the crypto side worth
knowing about, not yet changed).

### Stranded-dust sweep speed, and a real USDC blind spot found

`_check_and_sweep_stranded_dust()` locks genuinely-too-small-to-trade
real cash (below `MIN_TRADE_USD`, unchanged) into `locked_usd` instead
of leaving it dead forever - see `DUST_STUCK_HOURS`. Per the account
owner's explicit follow-up requests, lowered twice: 24h → 15 min →
**6 min (0.1h)**. The second drop required `DUST_CHECK_INTERVAL_SECONDS`
to come down too (15 min → 5 min) - a stuck-threshold change alone would
have been meaningless, since the check itself still only runs on its own
interval regardless of how low the threshold goes. With a 5-min check
interval, dust stuck for 6 min gets caught on the next check after
crossing that mark - still never a literal zero-wait sweep, which would
risk catching cash that's only momentarily below the minimum mid-cycle
(e.g. the few seconds between a sell settling and its own rebuy), not
genuinely stranded.

**Important distinction for reading the dashboard**: this only ever
sweeps real, un-deployed cash sitting idle below `MIN_TRADE_USD` - it
has nothing to do with the cents in numbers like "Total Allocated
$458.67". That `.67` is just the real, live sum of every branch's
actively-compounding balance (real fills produce non-round numbers) -
not stranded dust, and sweeping it would mean draining real capital out
of live positions, the opposite of the point.

**A real gap found while investigating this (not yet fixed - needs the
account owner's input)**: `get_usd_balance()`/`get_asset_balance()` in
`crypto_btc_compound_bot.py` only ever reads the literal "USD" Coinbase
account - there is zero USDC awareness anywhere in this codebase. If a
meaningful chunk of the account's cash sits in USDC (Coinbase's
"Earn X% APY by converting USD to USDC" prompt, or its own auto-rewards
enrollment, can do this), the bot's `real_balance` figure - and every
downstream real-cash calculation (`spendable_for_spawn`, buy sizing, the
dust sweep above) - is blind to it. Not yet fixed: unclear whether
Coinbase's Advanced Trade API can fund a `BTC-USD`-style market order
directly from a USDC balance, or whether it needs converting back to USD
first - needs confirming before deciding whether the fix is "the bot
also reads/uses USDC" or "convert back to USD, the bot's view is
correct as-is." Account owner's choice, for now: convert back to USD
manually when this happens.

### Spawn threshold lowered again, $150 → $100

Per the account owner's real observation ("I need more than just 5 up
here... add to the tree"): only 5 branches existed and none had crossed
the $150 spawn tier yet, so nothing new had spawned in a while.
`UNLOCK_TIER_USD` dropped to $100 (2x the $50 seed) - `PRIOR_UNLOCK_TIER_USD`
updated to $150 so the existing `_lower_existing_unlock_tiers()`
migration (built for the earlier $300→$150 drop) retroactively applies
the new tier to any branch still waiting on the old $150 bar for its
first spawn, without touching one that already progressed past it. This
leaves only a $50 buffer in the parent after each spawn (thinner than
the $150 tier's $100 buffer) but is still real - a branch can't go
net-negative from spawning, just closer to its own floor.

### Coin universe expanded, 26 → 37, after the tree hit its real ceiling

A direct consequence of the $100 spawn threshold above actually working:
the tree grew enough that every coin in the original `COIN_FAMILY_TREE`
(26 coins, 4 of them permanently in `MANUAL_EXCLUDED_COINS`) ended up
either claimed by an existing branch or excluded. A real "Start new $50
branch" click then failed with "Could not start a new branch: No
eligible coin left unclaimed to start a new branch on" -
`get_next_eligible_product_id()` working exactly as designed, just
genuinely out of room. Fixed by adding 10 more real, liquid pairs
already tradeable on Coinbase Advanced Trade - `BCH-USD`, `ETC-USD`,
`XLM-USD`, `ALGO-USD`, `FIL-USD`, `INJ-USD`, `SEI-USD`, `TIA-USD`,
`PEPE-USD`, `WIF-USD` - same onboarding as every coin already in the
list, nothing about the spawn/exclusion logic itself changed. Confirmed
against a reproduction of the exact real state that produced the error
(every original coin claimed or excluded) - a new branch can now spawn
on one of the 10 new coins.

### Real one-cycle sale cooldown per coin, so nothing can instantly rebuy what it (or another branch) just sold

Per the account owner's explicit follow-up: a coin becoming "claimed or
excluded temporarily" (the state above) should release again "one cycle
after it was bought and sold" if it's still bullish - previously nothing
enforced that gap. A coin became fully "unclaimed" the instant its
selling branch's row committed the new `product_id`, with nothing
stopping an immediate re-buy on the very next check - by a different
branch, or even the same branch on a near-simultaneous cycle.

Fixed with a real, short-lived, in-memory cooldown (`_coin_last_sold_at`
in `crypto_family_tree_bot.py`, all branches run as threads in the same
process so this doesn't need to survive a restart): `_branch_sell_and_
settle()` now records the moment a coin is sold, before it even looks
for a new coin to switch to. `_coin_sale_cooldown_active(product_id)`
checks whether less than one real `CYCLE_SECONDS` (30s default) has
passed since that coin was last sold - both `get_next_eligible_product_
id()` (manual/auto branch spawning) and `find_most_volatile_unclaimed_
coin()` (the coin-switch-after-exit path) now skip a coin still in that
window, on top of the existing claimed/excluded checks. After exactly
one cycle, if the coin is still bullish, it's a completely normal
candidate again - the existing bullish-first/volatility-tiebreak filter
in `find_most_volatile_unclaimed_coin()` is unchanged, this only adds a
timing gate in front of it.

### BTC root is never manually sellable, and the ROOT badge fix

Per the account owner's explicit request, spotted from the dashboard
showing three "ROOT"-badged, gold-styled cards (BTC, plus two adopted
legacy positions) and asking to lock down the real root while leaving
the other two alone. Two real fixes:

1. **Dashboard ROOT badge/gold styling** (`family_tree_dashboard.html`)
   used to key off `!b.parent_bot_name`, which is true both for BTC AND
   for any position adopted from the old pre-family-tree bot (see orphan
   adoption above) - those adopted branches aren't actually
   root-protected, they coin-switch and sell exactly like any other
   branch, so showing them identically to BTC was misleading. Now keyed
   off `b.bot_name === 'crypto_btc_compound'` specifically - only the
   real root gets the badge and gold card.
2. **BTC can never be manually sold** - `POST
   /family-tree-status/close/{bot_name}` now refuses outright
   (`400`) when `bot_name == ROOT_BOT_NAME`, matching BTC's existing
   automatic-exit behavior ("root stays on BTC-USD by design"). Enforced
   server-side, not just hidden in the UI, so it can't be bypassed by
   calling the endpoint directly - the dashboard shows "🔒 Permanent
   root — never sold manually" in place of the Sell button. Every other
   branch (including adopted-orphan ones) is completely unaffected and
   still sells normally when genuinely in profit.

### Catch-up spawn check, every cycle (not just right after a sell)

Per the account owner's real observation: the dashboard's "Next spawn"
progress bar could sit at 100% (a branch's `allocated_usd` already at or
above its `next_unlock_tier`) indefinitely without ever actually
spawning a child. Root cause: `_maybe_spawn_child()` used to be called
from exactly one place - inside `_branch_sell_and_settle()`, at the
moment a sale settles. A branch that crossed its tier but couldn't spawn
right then (every eligible coin already claimed at that exact instant),
or one adopted from the old pre-family-tree bot already above its tier
at adoption time (see orphan adoption above - this is also why some
branches show a "ROOT" badge on the dashboard despite not being the BTC
root: `isRoot` means "no parent," which is true both for BTC and for any
adopted legacy position, not a bug), had no other chance until its
*next* sell - which could be a long wait while it's holding a healthy,
not-yet-exited position.

Fixed: `run_branch_cycle()` now also calls `_maybe_spawn_child(branch)`
once at the top of every cycle, for every branch, not just after a sell.
Cheap when not eligible - the first line inside is a synchronous
comparison against the already-loaded branch, no DB query unless it's
actually crossed.

**A real bug caught by this fix's own test before it ever shipped**:
`_maybe_spawn_child()` deducts the $50 seed from a *fresh* row it loads
internally, not from the `branch` object `run_branch_cycle()` already
had in memory - so without reloading, every later use of
`branch.allocated_usd` in that same cycle (most importantly the buy-sizing
`spend = min(branch.allocated_usd, ...)` further down) would still see
the stale, pre-spawn balance, double-counting the $50 seed (transferred
for real to the child, then spent again off the old number on the
parent's own next buy). Fixed by reloading `branch` from the DB
immediately after the catch-up spawn check, before anything else uses
it. Confirmed via a test that asserts the exact dollar amount of the
following real buy.

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

### Real order-rejection reason now visible on the dashboard, not just a truncated Railway log line

The account owner shared Railway log screenshots showing real order
failures - `crypto_btc_compound_bot:[BTC-COMPOUND] Order not accepted:
{'error': 'INVALID_ARG...` and a separate `{'error': 'PERMISSION_...`,
both affecting real buy attempts (`crypto_tree_ltc_usd`,
`crypto_tree_ldo_usd`, `crypto_tree_dot_usd` all logging "buy did not
fill - will retry"). The actual error code/message was never visible in
either screenshot - Railway's mobile log view truncates long single
lines, and the useful part (nested under `error_response` in Coinbase's
response) was exactly what got cut off both times.

Root cause of the *invisibility*, not the rejection itself (Coinbase's
real reason for rejecting these specific orders is still unknown - could
be anything from a bad size/precision to a real permission issue on the
API key): `_place_and_confirm()` in `crypto_btc_compound_bot.py` (shared
by every branch's buy/sell) logged the raw `resp` dict directly, and
`place_market_buy()`'s only return on failure was bare `None` - so the
only place the real reason ever existed was that one truncated log line.

Fixed both ends of this:
1. `_describe_order_rejection()` pulls the real reason out of
   `resp["error_response"]` (`error` code + `message`) into one short,
   flat string that's far less likely to hit Railway's truncation limit
   than the full nested dict repr.
2. That reason is now persisted per `product_id` in the module-level
   `_last_order_error` dict (cleared the moment an order on that same
   coin succeeds), and `/api/trading-dashboard/family-tree-status`
   returns it per branch as `last_order_error`. `family_tree_dashboard.html`
   shows it directly on the branch's card (`⚠️ Last order rejected: ...`)
   when present - so the *next* real rejection is readable straight from
   the dashboard already open on the account owner's phone, no Railway
   log navigation or screenshot-and-hope-it's-not-cut-off required.

### MATIC-USD → POL-USD: a real dead trading pair, found via the order-rejection visibility fix above

The very next real rejection surfaced by the fix above was
`INVALID_ARGUMENT: Invalid product_id` on the branch holding MATIC-USD -
not a code bug, a real, permanent Coinbase change: Polygon migrated its
token from MATIC to POL, and Coinbase disabled MATIC-USD trading outright
on Oct 14, 2025, converting all balances to POL 1:1 by Oct 17 (confirmed
via Coinbase's own migration help page, not a guess). `MATIC-USD` in
`COIN_FAMILY_TREE` is now `POL-USD` - but renaming the list alone doesn't
unstick a branch that already exists with `product_id = "MATIC-USD"` in
the database, since a branch buys against its own stored `product_id`
directly and never re-reads `COIN_FAMILY_TREE` at buy time. Added
`_migrate_matic_to_pol()`, a startup migration (same pattern as
`_lower_existing_unlock_tiers()`) that moves any branch still on the dead
pair straight to `POL-USD` - the same coin, its real current identifier.

### BTC "Take profit now" - a real, root-safe carve-out from the manual-sell lockdown

Per the account owner's explicit follow-up request: a way to cash in
BTC's profit on demand, while BTC keeps its permanent root/parent spot -
"let it still keep its spot being a big dog being a parent and start all
over again... every time I hit that profit I wanted to take it." Since
BTC is otherwise completely locked out of manual selling (see "BTC root
is never manually sellable" above, at the account owner's own earlier
explicit request), this needed to be a deliberate, narrow carve-out, not
a bypass of that rule.

`POST /api/trading-dashboard/family-tree-status/root-take-profit`
(dashboard button: "🔒 Take profit now (stays BTC)", shown only when
BTC's position is genuinely in profit right now, same live-price check
`close_family_tree_branch` already uses) reuses the exact same
`_branch_sell_and_settle()` every automatic TARGET/STOP exit already
calls - root's own existing "stays on BTC-USD by design" logic inside
that function means this can never actually make BTC leave BTC-USD or
stop being root: it sells 100% at market, skims the same 10%-of-profit
into `locked_usd` every other exit already uses, and immediately rebuys
BTC-USD with the rest at the new price, same as any other branch's
normal win - then re-runs the cycle immediately (same reasoning
`close_family_tree_branch` already uses) so the rebuy happens in the same
call, not the next scheduled one. `_maybe_spawn_child()` still runs
afterward exactly as before, so BTC keeps spawning children normally.
This is purely an on-demand trigger for a cycle that already runs
automatically at BTC's computed target - `close_family_tree_branch`'s
root refusal (BTC can never be fully CLOSED/switched away) is completely
unaffected by this and still applies.

### Per-coin trade history, tracked across branches and repeat trades

Per the account owner's explicit request: "if I sell a coin and wind up
buying that same coin back, it'll start gaining its history... the third
time he bought Sol he sold it for this price and so far the profit has
been whatever it equals up to." New table `CryptoCoinTradeHistory`
(`models.py`) - one row per real completed sell, written inside
`_branch_sell_and_settle()` right where P&L is already computed.
Deliberately scoped by `product_id`, not by branch: since branches
switch coins over time and different branches can independently trade
the same coin at different points, a coin's history keeps accumulating
across all of that rather than resetting every time some branch happens
to hold it - buying SOL back after having sold it before picks up right
where its history left off. Append-only, never deleted, same reasoning
as the existing `ClosedTrade` model (a different table, for the
Alpaca/crypto-coinbase bots' ML training data - this one is family-tree-
specific and coin-scoped, not branch-scoped, so it wasn't a fit to reuse).

`GET /api/trading-dashboard/family-tree-status/coin-history` aggregates
real `trade_count`/`total_pnl`/`avg_pnl`/`win_rate` per coin (via a real
SQL `GROUP BY product_id`, not computed in Python) and nests each coin's
individual trades (up to the 500 most recent overall) underneath.
`family_tree_dashboard.html`'s new "📜 Coin Trade History" section shows
the aggregate table, tap a coin to expand its individual trades
(timestamp, branch, entry, exit, P&L, exit reason).

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

### Stock/ETF selection backtest (alpaca_selection_backtest.py)

The Alpaca-side counterpart to the crypto backtest above, per the
account owner's explicit request. Same "replay the bot's own real rules
on real history" approach - pulls real historical 15-min bars from
Alpaca's market-data API for every symbol `prop_bot.py`/
`alpaca_swing_bot.py` actually trade (`SPY`, `QQQ`, `DIA`, `IWM`, `GLD`,
`USO`, `SLV`, plus the new 1x inverse ETFs `SH`/`PSQ`/`DOG`/`RWM`) and
replays `alpaca_mean_reversion.py`'s real `should_exit_position()` -
importing the live function, not reimplementing it.

**Long-only, deliberately**: `validate_dual_direction()` can also flag a
SHORT entry, but shorting is disabled on the real Alpaca account
(`get_account_shorting_enabled()` in `prop_bot.py` - every real short
attempt has failed live with "account is not allowed to short"). A
short-side backtest would be purely hypothetical and couldn't inform any
real decision, so this only replays what's genuinely executable today -
entries trigger on RSI < 40, matching `validate_dual_direction`'s long
branch.

- Manual run: `POST /api/trading-dashboard/alpaca-selection-backtest`
  (admin-key gated, ~30-60s) and its viewer,
  `/alpaca-selection-backtest-view` (reuses the same saved admin key as
  the Alpaca dashboard). Cross-linked with the crypto backtest page and
  both dashboards, same pattern as the crypto side.
- The backtest run itself is shadow mode only - never places a real
  order, and nothing currently reads its results automatically (no
  auto-exclusion layer on this side yet, unlike the crypto side's
  two-layer system above).

**"Trade this" button on the backtest page** (`POST
/api/trading-dashboard/alpaca-overview/trade-this/{ticker}`): per the
account owner's explicit request, matching the crypto side's equivalent -
found missing and called out directly ("no stocks options to sell
anything on this dashboard... maybe you just didn't do it at all").
Places a REAL long market order on `prop_bot.py`'s real funded-account
evaluation, right now, on demand - but NOT a shortcut around the
account's real risk rules. It reuses the exact same real functions the
automatic entry path calls (`get_price_rsi`, `validate_entry`/
`APEX_MANDATE`'s universe check, `check_kill_conditions`,
`check_margin_safety`, `size_position`, `execute_futures_trade`) rather
than reimplementing any of them, so a manual entry gets the same real
protection an automatic one does - refused if you're already holding
that contract, `STOP_TRADING` is set, a real kill condition is active
(daily loss limit, critical buying power, equity below survival), or the
mandate/margin-safety checks fail (e.g. RSI not oversold, insufficient
buying power, real risk-limit exceeded). Long-only, matching everything
else `prop_bot.py` can actually execute today. On success, updates the
live `open_prop_positions` dict directly (not just the DB) so the
bot's own automatic exit cycle picks up and manages the new position on
its very next scan - the same reasoning `load_open_positions()` exists
for at startup.

**A real, previously-undiscovered bug found and fixed while building
this**: `_db_save_open()`'s measurement-system logging branch has always
called `datetime.now(timezone.utc)`, but `timezone` was never imported
in `prop_bot.py` (only bare `datetime`) - every real position opened by
this bot (automatic or manual) has been silently failing to log to the
trade-measurement system this whole time (`NameError`, caught and
logged as "Failed to persist opened position", never crashing the bot).
The actual position-tracking DB write (the real `BotPosition` row) was
never affected - that commits in its own block before this broken code
runs - so no real trading data was lost, only analytics logging. Fixed
by adding the missing import.

**Real bug found and fixed: "Trade this" failed on USO with "Could not
fetch a live price/RSI"**. Root cause was in `get_price_rsi()` in
`prop_bot.py`, the function both the automatic scan cycle and the
manual "Trade this" endpoint call: its request to Alpaca's 5-min bars
endpoint never passed an explicit `feed` parameter, unlike its sibling
`get_higher_tf_trend()` right below it (which passes `feed=iex` and was
never seen to fail) - so its behavior depended on whatever feed Alpaca's
default resolves to for the account's data-subscription tier, instead of
being deterministic. Thinner-volume symbols like USO/GLD/SLV are far
more likely to come up short of the required 50 five-minute bars on
whichever feed that turned out to be than a heavily-traded symbol like
SPY/QQQ. Fixed by adding `feed=iex` explicitly, matching the working
sibling function.

Separately, and regardless of the root cause above: `get_price_rsi()`'s
failure path only ever returned bare `None` - the automatic cycle never
needed more than that, but it meant the manual endpoint's error could
only ever say "could not fetch," with the real reason (HTTP status, bar
count, parse error) sitting in a Railway log this account owner has no
way to hand over easily. Fixed by having `get_price_rsi()` record the
specific reason for its last failure per symbol in the module-level
`_price_rsi_last_failure` dict (cleared on the next success), which
`POST /alpaca-overview/trade-this/{ticker}` now includes in its 503
detail - so a future fetch failure is diagnosable straight from the
dashboard's error alert, e.g. "Only 12 of the required 50 5-min bars are
available right now" instead of a generic message.

**Two-layer coin exclusion** (`get_effective_excluded_coins()` in
`crypto_family_tree_bot.py`, checked by both `find_most_volatile_unclaimed_coin()`
and `get_next_eligible_product_id()` before either ever runs):

1. **`MANUAL_EXCLUDED_COINS`** - `{STX-USD, BLUR-USD, UNI-USD, DOT-USD}`,
   the account owner's explicit real-money starting decision after the
   first backtest run (STX-USD was dead last: -44.1% ROI, 21.6% win
   rate). Per a LATER explicit choice, this is not a one-way permanent
   blacklist either - see `_manually_excluded_still_excluded()`: a coin
   in this starting set stays excluded only until it clears the SAME
   bar the automatic layer below needs to self-heal (its last
   `AUTO_EXCLUDE_RUN_WINDOW` real backtest runs all positive-ROI), then
   becomes tradable again. The default is the opposite of the automatic
   rule's default though: a coin here with fewer than the window's
   worth of real runs on record STAYS excluded (the original decision
   needs real positive evidence to be lifted, not just an absence of
   bad evidence) - whereas a coin the automatic rule has never flagged
   is never excluded in the first place just for lacking history. The
   starting SET itself (which 4 coins begin excluded) still only ever
   changes via another explicit decision like the original one.
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
   is too noisy to act on alone. This same rule can also re-exclude a
   coin that healed out of the manual list above if it turns bad again
   later - nothing in either layer is a one-way verdict.

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

### Downtrend profit via 1x inverse ETFs (no shorting/margin)

Per the account owner's explicit request to profit when the market is
falling, without taking on margin or shorting risk: `prop_bot.py`'s
`FUTURES` dict and `alpaca_swing_bot.py`'s `SWING_SYMBOLS` dict both now
also carry `SH`, `PSQ`, `DOG`, `RWM` - real, liquid **1x** (non-leveraged)
inverse ETFs, one per index already traded (inverse of SPY, QQQ, DIA,
IWM respectively). These are bought **LONG**, through the exact same
entry/exit code every other symbol already uses (`validate_dual_direction`,
`should_exit_position`, position sizing, order placement) - zero new code
path. An inverse ETF just moves opposite its index, so a normal long
entry on SH profits when SPY falls; the bot never actually shorts
anything or touches margin. Deliberately the 1x versions, not the 2x/3x
leveraged ones (SDS, SQQQ, SDOW, SRTY) - those add leveraged-decay risk
that wasn't asked for. `routers/trading_dashboard.py`'s
`CHART_STOCK_SYMBOLS` SSRF allowlist was extended to match, so the
dashboard can chart these too.

(Real shorting already exists as dormant, gated code in
`alpaca_mean_reversion.py`/`prop_bot.py` - `validate_dual_direction` can
return `"short"`, but `get_account_shorting_enabled()` checks the real
Alpaca account first and every short entry has been failing in
production with "account is not allowed to short" - a real account-level
restriction, not a bug. The inverse-ETF approach above sidesteps that
restriction entirely rather than requiring a margin-enabled account.)

**A real, previously-undiscovered production bug found and fixed while
adding this**: `APEX_MANDATE["universe"]["commodities"]` (in
`bot_mandates.py`) listed the underlying tickers (`GLD`, `USO`, `SLV`)
instead of the **contract codes** (`MGC`, `MCL`, `SIL`) - the same
identifier space `"futures"` (`MES`/`MNQ`/`MYM`/`M2K`) correctly uses,
and the same one `prop_bot.py`'s `MANDATE CHECK 1` and `validate_entry`
(`MANDATE CHECK 2`) both actually compare against (`contract`, the
`FUTURES` dict key - never the underlying symbol). A ticker can never
equal a contract code, so every gold/oil/silver signal has been silently
rejected at `MANDATE CHECK 1` with "NOT in approved universe - SKIPPING"
since this mandate existed - those three symbols have never once been
able to place a real order. Separately, `validate_entry`'s own internal
universe check never included `"commodities"` at all (only
`futures`/`crypto`/`approved`/`approved_pairs`), which would have
independently re-blocked them at `MANDATE CHECK 2` even after the first
fix. Both fixed: `commodities` now holds `["MGC", "MCL", "SIL"]`, and
`validate_entry` now includes both `commodities` and the new
`inverse_etfs` category in its approved-symbols check.

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
