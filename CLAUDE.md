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
  (price now higher than ~25 hours ago), now ALSO filtered by
  `engine.ENTRY_MAX_RSI` (65) - see "Overbought-entry RSI filter" below,
  a previously-known gap that's now fixed. See the coin-selection
  backtest section below - candidates are now also filtered through a
  real exclusion list before this function even runs.
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

### Real production crash found and fixed: duplicate locked-profit rows (MultipleResultsFound)

A real Railway traceback surfaced this: `sqlalchemy.exc.MultipleResultsFound:
Multiple rows were found when one or none was required`, raised from
`scalar_one_or_none()`. Root cause: `_add_locked_usd()` did a plain
"select, then insert-or-update" against the shared `trading_bot_state`
table with no real DB-level uniqueness backing it - the model has
always declared `bot_name = Column(String, unique=True, ...)`, but
`Base.metadata.create_all()` only applies that constraint when CREATING
a brand-new table; it's a no-op against `trading_bot_state`, which
already existed from long before this locked-profit feature was added.
Every branch runs as its own thread, and two branches skimming profit
close enough together both saw "no row yet" and both inserted a real
row for the same `LOCKED_PROFIT_STATE_KEY` - after that, every read of
`locked_usd` (`get_locked_usd`, `_add_locked_usd`, `_subtract_locked_usd`
- all `scalar_one_or_none()`) started crashing, which meant every
branch's buy path (they all check `real_balance - locked_usd` before
sizing a trade) could be failing on this same line.

Fixed with three parts, each independently tested against the real
crash scenario:
1. `_dedupe_locked_profit_state()` - a one-time startup migration that
   merges any existing duplicate rows by **summing** them (every dollar
   in every duplicate row is real skimmed profit; discarding one would
   make real money vanish from the ledger), keeping the oldest row as
   the survivor.
2. `_ensure_trading_bot_state_unique_index()` - adds the real DB-level
   unique index the model always claimed to have, run right after the
   dedupe above (a unique index can't be created while real duplicates
   still exist) - protects every `TradingBotState.bot_name` key across
   the whole app from this exact race recurring, not just locked profit.
   If some other, unrelated key also has real duplicates this migration
   doesn't know how to safely merge, it logs a warning and leaves the
   constraint absent rather than guessing at a merge for data it
   doesn't understand - same defensive pattern already used for
   `crypto_tree_branches.product_id`'s own unique index.
3. `_add_locked_usd()` itself now catches the real `IntegrityError` a
   genuine race produces (once the index exists) and retries as a real
   update against whichever row actually won the race, instead of
   silently creating a second row.

### A second real production crash from the same pattern: duplicate BotPosition rows (crypto_tree_xrp_usd stuck every cycle)

A Railway log screenshot batch surfaced this: `crypto_tree_xrp_usd` was
raising `sqlalchemy.exc.MultipleResultsFound` on literally every cycle,
inside `_load_branch_position()`'s `scalar_one_or_none()` - the same
underlying shape of bug as the locked-profit crash above (a shared table
key ending up with two rows and every scalar_one_or_none() read on it
crashing), but on `bot_positions`, and a different consumer of that
table: `BotPosition.bot` was **never** declared `unique=True` in the
model at all, unlike `TradingBotState.bot_name` (which at least claimed
it) - and critically, `prop_bot.py`'s `prop_apex` and
`crypto_coinbase_bot.py`'s `crypto_coinbase` **legitimately** hold
several real concurrent positions under one shared `bot` value (one row
per open symbol - prop_bot alone can hold up to 8), so a bare unique
index on `bot` the way `trading_bot_state` got one would have been
wrong and broken real multi-position trading immediately. Each
family-tree branch is different: it's a single-position engine where
its own `bot_name` IS meant to be the position's unique key, so more
than one row under the same bot_name is always a bug there specifically
- the fix had to be scoped to just that.

Fixed in `crypto_family_tree_bot.py`, deliberately not touching the
shared `BotPosition` model or `prop_bot.py`/`crypto_coinbase_bot.py`'s
own use of the table at all:
1. `_dedupe_family_tree_positions()` - a one-time startup migration,
   scoped to only the `bot_name`s that currently exist as real
   `CryptoTreeBranch` rows. Every duplicate row's qty is real quantity
   this system can't tell apart from a genuine fill, so it sums the qty
   (at a qty-weighted-average entry price) rather than discarding one
   row's worth - same "never lose real money" reasoning as the
   locked-profit dedupe - and keeps the most recently opened row's
   target/stop.
2. Every read site (`_load_branch_position`, `_update_branch_position_peak`,
   `_raise_branch_stop_to_breakeven`) now orders by `id desc` and takes
   the first row instead of `scalar_one_or_none()` - defense in depth,
   so a stray future duplicate degrades gracefully (uses the most recent
   row) instead of crashing the branch's thread every cycle forever.
3. `_clear_branch_position()` now deletes **every** row under a
   bot_name, not just one.
4. `_save_branch_position()` now calls `_clear_branch_position()` first,
   before inserting - self-healing, so a family-tree branch can never
   leave two rows behind under its own bot_name again, without needing
   a DB-level constraint that would have had to special-case prop_apex's
   and crypto_coinbase's legitimately-multi-row bots.

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

### JUP-USD removed entirely - a second dead pair, with no real successor

Real Railway logs kept showing `crypto_family_tree_bot:[TREE]` warnings
of the shape "X can never fill (PERMISSION_DENIED...) - switching to
JUP-USD instead of retrying forever" - the already-fixed
permanent-rejection auto-switch (see above) correctly moved a branch OFF
its dead coin, but kept landing it ON JUP-USD, which has its own
separate, real, repeatedly-confirmed-live `INVALID_ARGUMENT: Invalid
product_id` rejection - so the branch would just fail again on its very
next cycle. Unlike MATIC-USD, there's no public Coinbase migration
notice for JUP-USD and no successor id to rename it to - it appears to
simply not be a listed product on this account/tier, still unconfirmed
exactly why, but confirmed real and permanent by now from repeated
identical failures across multiple sessions. Removed `JUP-USD` from
`COIN_FAMILY_TREE` outright (36 coins now) - it can never be offered as
a switch target to any branch again. No special migration function was
needed the way `_migrate_matic_to_pol()` was: any branch currently stuck
on `JUP-USD` still gets moved off it automatically by the existing
permanent-rejection auto-switch on its very next cycle, now landing on a
real, live coin instead of bouncing straight into JUP-USD again.

### A stuck branch now switches coins instead of retrying a doomed order forever

Per the account owner's explicit request, after continuing to see the
same real rejections on the dashboard ("remove this from showing up,
stop this from happening"): RNDR-USD's `PERMISSION_DENIED` and
JUP-USD's `Invalid product_id` are real Coinbase rejections that can
never succeed no matter how many more times the identical order is
retried - RNDR because the account/API key genuinely lacks trading
permission on that orderbook (a real Coinbase-side setting, not fixable
in this code), JUP for a reason as yet unconfirmed (unlike MATIC-USD,
there's no public migration notice - it may simply not be listed for
this account/tier). Previously a flat branch hitting either just logged
"buy did not fill - will retry next cycle" forever, staying permanently
stuck and showing the same red rejection on the dashboard indefinitely.

`crypto_btc_compound_bot.py`'s new `_is_permanent_order_rejection()`
recognizes these two real, confirmed-live patterns specifically (not a
guess at every possible Coinbase error code - only ones actually
observed in production) and is checked in `run_branch_cycle()`'s flat-
branch buy path: on a permanent rejection, the branch switches to a
different coin immediately via the same real `find_most_volatile_
unclaimed_coin()` search every other coin-switch already uses, instead
of retrying the same dead order. The dashboard's red "Last order
rejected" banner clears itself naturally once this happens - it's
looked up by the branch's *current* `product_id`, which is now a
different, working coin with no error history.

A transient-looking failure (insufficient funds, a network hiccup, an
unrecognized reason) is deliberately left alone - only these two
specific, confirmed-permanent patterns trigger a switch, so a real but
temporary issue still just retries normally next cycle.

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

### Total Profit KPI, front of the dashboard

Per the account owner's explicit request ("how much is my profit in
all, put that at the top of the dashboard"): a new "💰 Total Profit"
card, first in the KPI row so it's the first number seen. Computed
entirely client-side from data the page already fetches, no new
endpoint - real realized profit (every dollar from every completed sell
ever recorded, summed across all of `/family-tree-status/coin-history`'s
per-coin `total_pnl`) plus real unrealized profit (summed live across
every branch's currently open position, the same `qty * (current_price -
entry_price)` each branch card's own Profit row already computes).
Green when positive, red when negative - the same two lenses the rest
of the dashboard already shows separately, just added into one real
top-line number.

### Total Profit KPI on the Alpaca dashboard too

Per the account owner's explicit follow-up ("show me the profit the
bots are doing as well"), the same "💰 Total Profit" card was added to
`alpaca_dashboard.html`'s KPI row, first card again. The formula is
simpler here than the crypto side's realized+unrealized sum: each
`bot_N` bucket's `pl` (`_bot_pl()` in `routers/trading_dashboard.py` -
already returned per bucket by `/alpaca-overview`) is the real signed
delta between its current `base_capital` and its `starting_capital`
snapshot, and `_rebalance_bots()` keeps every bucket's `base_capital`
continuously synced against the real Alpaca account `equity` - which
already includes unrealized P&L on open positions. So `bots[].pl`
already captures both realized and unrealized profit for that bucket;
Total Profit is just `sum(bots[].pl)` across all 8 buckets, computed
client-side from data the page already fetches. Deliberately does NOT
also add `positions[].unrealized_pl` on top - that would double-count
the unrealized portion already folded into each bucket's `pl`.

### Overbought-entry RSI filter, closing a previously-known gap

Per the account owner's explicit request after seeing real evidence of
losses ("cut the weak coins now, and add a real entry filter so it stops
taking bad setups") - the coin-trade-history table showed PEPE, DOGE,
and one of two XRP trades all as quick losers, exactly the shape of loss
you'd expect from `find_most_volatile_unclaimed_coin()`'s only signal
being "bullish over the last ~25 hours": a coarse, medium-term check
with no protection against buying a coin that had ALREADY pumped hard
and was due to pull back right as it got bought.

Deliberately not a literal copy of `prop_bot.py`'s RSI < 30
oversold-entry filter - that engine buys dips (mean reversion);
`crypto_family_tree_bot.py` buys momentum (already-bullish coins), so
"wait for oversold" would fight the bullish-only selection this engine
is built around. The real analogous fix is the other direction: refuse
a candidate that's ALREADY overbought right now, using the same
threshold `prop_bot.py` already established for crypto specifically
(`CRYPTO_RSI_SELL_ABOVE = 65`, tighter than stocks' 70) - now
`engine.ENTRY_MAX_RSI` in `crypto_btc_compound_bot.py`.

- `_rsi_from_closes()` mirrors `prop_bot.py`'s `get_price_rsi()` formula
  EXACTLY (simple moving-average RSI over the last 14 gain/loss values,
  not Wilder's smoothing) - kept identical on purpose so this is a real
  analogous adaptation, not a different indicator wearing the same name.
  Computed from the same ~25-hour, 5-minute candle fetch ATR already
  uses - no new API calls.
- `get_price_volatility_and_trend()` now returns a 4-tuple
  `(price, atr_pct, is_bullish, rsi)` instead of 3 - `rsi` can
  independently be `None` (too little price history) even when the
  other three fields are real; only one real caller exists in the
  codebase (`find_most_volatile_unclaimed_coin()`), so this didn't touch
  any other consumer.
- `find_most_volatile_unclaimed_coin()` now skips any candidate whose
  RSI is at or above `ENTRY_MAX_RSI`, in BOTH the bullish path and the
  any-volatility fallback - never buy into an already-overbought coin,
  whichever path would have picked it. A candidate with `rsi=None` is
  still eligible - the filter only excludes a CONFIRMED overbought
  reading, not an absence of one. If every unclaimed candidate is
  overbought, correctly returns `(None, None)` rather than buying the
  least-bad option.

Verified with a dedicated offline test (`_rsi_from_closes` checked
against an independently-written reference implementation of the same
formula, plus the core scenario: a coin with the highest ATR AND
bullish AND overbought gets skipped in favor of a real, non-overbought
candidate) - this could only be validated as internally consistent and
mathematically correct, not as an improvement to real trading outcomes,
since running the actual coin-selection backtest against real Coinbase
history requires network access this environment doesn't have (see the
coin-selection backtest section below) - that validation has to happen
live, watched after deploying.

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
   in this starting set stays excluded only until it clears a real bar.
   That bar was originally the SAME one the automatic layer below needs
   to self-heal (its last `AUTO_EXCLUDE_RUN_WINDOW` runs all
   positive-ROI) - per a FURTHER explicit follow-up ("if it become
   profitable faster than that allow it to break free"), it's now the
   SAME single-run bar the automatic layer itself uses: the instant a
   manually-excluded coin's most recent real backtest run turns
   positive, it's tradable again - confirmed against STX-USD's real
   live numbers (-19.1% ROI, worst performer) still correctly excluded,
   since even the faster rule needs a genuinely positive run, not just a
   less-negative one. The default is still the opposite of the automatic
   rule's default though: a coin here with zero real runs on record
   STAYS excluded (the original decision needs real positive evidence to
   be lifted, not just an absence of bad evidence) - whereas a coin the
   automatic rule has never flagged is never excluded in the first place
   just for lacking history. The starting SET itself (which 4 coins
   begin excluded) still only ever changes via another explicit decision
   like the original one.

   **Follow-up addition**: `PEPE-USD` and `WIF-USD` - two of the 10 meme
   coins added when the universe expanded from 26 to 37 (see above) -
   were added to this set within hours of going live, per the account
   owner's explicit choice after seeing real evidence on the coin-history
   dashboard: PEPE-USD lost on its first-ever trade (-$0.99, 0% win
   rate). Both had zero backtest runs on record at the time they were
   added, so the "zero runs = stays excluded" default above applies -
   they need a real positive backtest run to heal back into rotation,
   same as every other manually-excluded coin. `MANUAL_EXCLUDED_COINS`
   is now `{STX-USD, BLUR-USD, UNI-USD, DOT-USD, PEPE-USD, WIF-USD}`.
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

### Alpaca-side auto-exclusion, mirroring the crypto family tree's two-layer system

Per the account owner's explicit follow-up after being shown a real
`alpaca_selection_backtest.py` run live on the dashboard (`USO` +21.6%
ROI/73.1% win rate down to the 1x inverse ETFs `PSQ`/`SH`/`RWM`/`DOG` all
net-negative, since the real 30-day sample wasn't an actual downtrend):
"we can use that as part of the filter." The backtest tool already
existed in shadow mode, but nothing previously read its results
automatically - a symbol could sit at deep negative real backtested ROI
and `prop_bot.py` would still be willing to enter it on the next
RSI-oversold signal. This ports the crypto side's automatic exclusion
layer (not the manual starting-list layer - not requested here) onto the
stock/ETF side:

- New `AlpacaBacktestRun` model (`models.py`), the direct counterpart to
  `CryptoBacktestRun` - one row per symbol per real backtest run,
  `product_id` holding the real ticker (e.g. `"USO"`), matching
  `alpaca_selection_backtest.py`'s own field name.
- `get_effective_excluded_symbols()` (`prop_bot.py`): a symbol
  auto-excludes once its last `AUTO_EXCLUDE_RUN_WINDOW` (3) real runs
  were ALL negative-ROI, and un-excludes the instant its most recent run
  turns positive - contestable/self-healing, same philosophy as the
  crypto side, never a one-way verdict. A symbol with fewer than 3 real
  runs on record is never excluded - not enough evidence yet.
- `_run_scheduled_backtest_and_update_exclusions()`: called from
  `run_prop_cycle()` itself (throttled to once per
  `AUTO_BACKTEST_INTERVAL_SECONDS`, 24h default, same as the crypto
  side's coordinator-loop pattern - `prop_bot.py` has no separate
  coordinator, so the check lives inline in its own cycle instead),
  re-runs the exact real backtest the manual dashboard button triggers
  and persists every symbol's result.
- Enforced in two places: the automatic entry path (`try_open()`'s new
  "MANDATE CHECK 1.5", right after the existing universe-enforcement
  check) and the manual `POST /alpaca-overview/trade-this/{ticker}`
  endpoint (refuses a currently-excluded symbol with a clear reason,
  matching the crypto side's manual spawn-branch endpoint doing the
  same) - so a real, deeply-underperforming symbol can't be entered
  either automatically or on demand while it's excluded.

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

## Status snapshots for Claude sessions with no live network access (status_snapshot.py)

A Claude Code session working on this repo (in a sandboxed cloud
environment, as opposed to the account owner's own machine) generally
has **no live network access** to this Railway deployment, Coinbase, or
Alpaca - confirmed directly: a request to this app's own production URL
got a real `403` at the outbound proxy layer, before it even reached the
app. It also has no live trading API credentials. So by default, such a
session can only reason about the CODE, not real current account state -
real balances, real open positions, real recent P&L are all invisible to
it unless the account owner manually screenshots or pastes them in.

Per the account owner's explicit request ("do what you suggest" -
choosing this over opening up live network access + real trading
credentials to the session, which would also grant the ability to place
real trades directly, a much bigger blast radius than read-only
visibility), `status_snapshot.py` closes this gap through a channel that
already exists both ways: git. The running app itself already has real
DB access and real live Coinbase/Alpaca API access (it IS the app) - it
periodically builds a real status report and pushes it as a git commit,
which any Claude session with normal repo access (which every session
working on this codebase already has) can then read with a plain `git
fetch` + `git show`, no new access needed.

- Runs as a background daemon thread (`status_snapshot.run()`, started
  from `main.py`'s lifespan the same way every other bot module is)
  every `STATUS_SNAPSHOT_INTERVAL_SECONDS` (30 min default).
- Reuses the EXACT SAME functions the live dashboards already call
  (`get_family_tree_status`, `get_coin_trade_history`,
  `get_alpaca_overview` in `routers/trading_dashboard.py`) rather than
  re-deriving real numbers a second way - the snapshot can never show a
  different reality than the dashboards do.
- Writes a human-and-Claude-readable `STATUS.md` (real allocated/locked/
  total-profit figures, a per-branch table with live unrealized P&L,
  real per-coin trade history, real Alpaca bucket P&L and open
  positions) and pushes it to a **dedicated `status-snapshots` branch -
  never `main`** - so it can never trigger a Railway redeploy (Railway
  is configured to deploy on pushes to `main`; a redeploy every 30
  minutes purely from a status commit would be a real, unwanted side
  effect). Force-pushes a single fresh commit each cycle - the branch is
  a moving pointer to "latest real state," not an accumulated history.
- **Read-only by design**: the only thing this module ever writes is a
  markdown file and a git commit. It has no code path that can place an
  order, touch a position, or affect trading in any way - it only reads
  real data that other, already-existing endpoints already expose.
- **Requires setup the account owner has to do, not something a Claude
  session can provision itself**: a GitHub token scoped to just this
  repo (Contents: Read and write - a fine-grained personal access token,
  not a classic all-repos token) added as the `STATUS_SNAPSHOT_GITHUB_TOKEN`
  Railway env var. Without it, `run()` logs once on startup and does
  nothing further - every other part of the app is completely
  unaffected either way.
- **To read the latest snapshot** (from any session with normal git
  access to this repo): `git fetch origin status-snapshots && git show
  origin/status-snapshots:STATUS.md`.
- Verified offline with a dedicated test: the markdown generator against
  real seeded `CryptoTreeBranch`/`CryptoCoinTradeHistory`/
  `TradingBotState` rows, and the actual git add/commit/push sequence
  the function runs against a real local throwaway repo+remote (proving
  the git plumbing itself is correct) - the real GitHub push itself
  couldn't be verified from the sandboxed dev environment this was built
  in (no live network access, the exact gap this feature exists to
  work around), so that part needs confirming once actually deployed.

---

## Real bug found and fixed: a BotPosition with a NULL/invalid symbol was unmanageable (prop_bot.py)

Railway logs showed `alpaca_mean_reversion` exit-check lines reading
"None (LONG)" instead of a real contract code (e.g. "MES (LONG)") -
`⏱️ None (LONG): Max hold time exceeded`, `💰 None (LONG): Peak profit
giveback...`, `📈 None (LONG): RSI exit signal...`. Traced to
`load_open_positions()`, which reloads every `BotPosition` row for
`bot='prop_apex'` at startup keyed directly by `row.symbol`, with no
validation. A row with `symbol` NULL (or any value that isn't a real
`FUTURES` contract code) gets loaded under that literal bad key -
which can never match a real contract again. The exit-check math still
ran (real price/RSI data happened to be available), which is how these
specific log lines could exist at all, but the very next thing every
one of the three exit-management passes in `run_prop_cycle()` did was
a bare `config = FUTURES[contract]` - which raises a real `KeyError`
for a bad key. A position stuck in this state can never actually be
closed through the normal path: real money sitting open on Alpaca that
this bot could see was ready to exit, but couldn't act on.

Fixed in two parts:
1. `load_open_positions()` now skips (and logs loudly, by BotPosition
   `id`, so it's traceable) any row whose `symbol` isn't a real key in
   `FUTURES` - the corrupted key can never enter `open_prop_positions`
   again from this path.
2. The three exit-management passes in `run_prop_cycle()` (daily-loss
   circuit breaker, equity-floor breach, and the main per-cycle exit
   check) now use `FUTURES.get(contract)` and skip-with-a-loud-warning
   instead of the bare `FUTURES[contract]` indexing that would have
   crashed - defense in depth, so a stale key reaching
   `open_prop_positions` through any other path in the future degrades
   to "this one position sits unmanaged, loudly logged" instead of an
   uncaught `KeyError`.

Verified offline: reproduced the exact scenario (a real `BotPosition`
row with `symbol=None` seeded alongside a real valid one), confirmed
the corrupted row is skipped while the real one still loads correctly,
confirmed a non-None-but-still-invalid symbol is caught by the same
guard, and confirmed `FUTURES[None]` really does raise `KeyError` -
proving this is what the old code was hitting.

**Not yet resolved**: which real BotPosition row actually has the bad
symbol, and whether it corresponds to a real position still open on
Alpaca that needs a manual close - this fix stops the bot from
crashing on it and makes it loudly visible in the logs (by row id), but
finding and manually resolving the underlying stuck row/position still
needs a real look, since this session has no live DB or Alpaca access
to do that itself.

---

## Real bug found and fixed: a healthy branch below its own floor could never spawn again (crypto_family_tree_bot.py)

The dashboard showed `crypto_btc_compound` at $121.93 balance vs a
$150.00 floor, holding a genuinely healthy, breakeven-protected
position (+0.09%) - "Next spawn" sat at 100% but never actually fired.
`_maybe_spawn_child()` was correctly refusing (`allocated_usd <
equity_floor` logs "not spawning while unhealthy"), but nothing was
ever going to fix that: the only two paths that ever lower a branch's
floor are `_branch_sell_and_settle()`'s post-sale reset and
`run_branch_cycle()`'s flat-branch self-heal - neither ever fires for a
branch that's actively HOLDING a position, which is exactly BTC's
state. Since this position's own unrealized P&L (+0.09%) couldn't
explain a ~$28 gap between balance and floor, the real cause traces
back to a legitimate, intentional spawn deduction (giving a child its
$50 seed) that happened while the balance was above $150 - not a
trading loss, which is what the floor ratchet actually exists to guard
against. Left as-is, this branch would have stayed locked out of
spawning indefinitely, potentially until its current position happened
to sell on its own schedule.

Fixed in `_maybe_spawn_child()`: when a branch has crossed its own
unlock tier but sits below its floor, it now self-heals the floor down
to the branch's own real current tier (same `math.floor(balance /
BRANCH_FLOOR_TIER) * BRANCH_FLOOR_TIER` formula the other two self-heal
paths already use) and spawns immediately in the same call, instead of
returning and waiting on a sale that might not happen for a long time.
Doesn't touch the held position's own risk management at all - the
floor-breach force-sell path only ever fires when a held position's
OWN stop has also failed, completely unaffected by this fix either
way. Only ever applies once the branch has actually crossed its next
unlock tier - a branch below floor that hasn't earned its way there
yet is left completely untouched, same as before.

Verified offline: reproduced BTC's exact real numbers ($121.93
balance, $150.00 floor), confirmed the floor heals to $100.00 (its own
real tier), confirmed it spawns a real child in that same call instead
of needing another cycle, confirmed the parent's balance still drops
by the real seed amount afterward, and confirmed a branch that hasn't
crossed its own unlock tier yet is left untouched by this code path.

---

## Multiple branches can now share the same coin

Per the account owner's explicit request, after "Trade this" on POL-USD
(a real coin already looking profitable in the backtest) refused with
"POL-USD is already claimed by an existing branch": the original
one-coin-per-branch rule - every branch trades a different coin, so two
branches never fight over or double up on the same one - is now gone.
A coin already held by one branch can be manually traded into again via
"Trade this", auto-spawned onto by a brand-new $50 branch, or picked by
the auto coin-switch-after-exit search, same as any unheld coin.

This was a real, deliberate architecture change, not a small tweak -
confirmed explicitly via a direct choice between "allow it everywhere"
and "just pick a different coin instead" before touching it, given how
much of this system leaned on that invariant.

**What changed:**
1. **The real DB-level UNIQUE index** on `crypto_tree_branches.product_id`
   (`ix_crypto_tree_branches_product_id_unique`, added earlier this same
   session specifically to make coin-claim races impossible) is now
   dropped by `_drop_product_id_unique_index()` - a startup migration
   (same pattern as every other one-time fix in this file), safe to run
   on a deployment that never created the index too. Without dropping
   this, every other change below would still fail at the database layer
   regardless of what the Python-level checks say.
2. **`get_next_eligible_product_id()`** (brand-new $50 branches) no
   longer requires a coin to be unclaimed - still prefers whichever
   eligible coin currently has the FEWEST branches on it (a coin with
   zero branches always wins first, same practical result as before in
   the common case), but degrades to piling onto the least-crowded coin
   once every coin already has at least one branch, instead of refusing
   to spawn at all.
3. **`find_most_volatile_unclaimed_coin()`** (auto coin-switch after an
   exit) no longer excludes an already-held coin from consideration at
   all - always picks the objectively best real-time candidate by
   trend/volatility/RSI, since the whole point of a manual or automatic
   switch is going after what's actually performing, not steering away
   from a coin just because it's already proving itself elsewhere in the
   tree. The one-cycle sale cooldown (`_coin_sale_cooldown_active`) still
   applies, so a branch still can't instantly rebuy the exact coin it
   just sold.
4. **The manual `POST .../spawn-branch/{product_id}`** ("Trade this")
   and **`POST .../spawn-branch`** (auto-pick "Start new $50 branch")
   endpoints both dropped their "already claimed by an existing branch"
   409 refusal.
5. **`_unique_child_bot_name()`** (new) - a branch's `bot_name` has
   always been derived directly from its coin
   (`crypto_tree_{coin}`), which was fine when `product_id` itself was
   unique, but `bot_name` still needs to be unique (it's the real
   per-branch thread/identity key everywhere else in this system:
   `BotPosition.bot`, the coordinator's `_running_threads` dict, etc.).
   Appends `_2`, `_3`, ... until landing on a free name, so a second (or
   third) branch on an already-claimed coin gets a real, distinct
   identity instead of colliding with the existing one. Used by both the
   organic parent-triggered spawn (`_maybe_spawn_child`) and both manual
   spawn endpoints.

**What deliberately did NOT change**: `CryptoTreeBranch.bot_name` itself
is still a real, enforced-unique identity - two branches can share a
coin, but never a name. The orphan-adoption path's own
`already_claimed = await load_branch(bot_name)` check (a DIFFERENT kind
of "claimed" - about bot_name collision, not coin ownership) is
untouched for the same reason. The per-coin trade-history aggregation
(`/family-tree-status/coin-history`) already grouped by `product_id`
rather than by branch, specifically so multiple branches trading the
same coin accumulate onto one real history instead of fragmenting - no
change needed there, it was already built for this.

Verified offline: confirmed a real pre-existing unique index actually
gets dropped (not just skipped) and two branches on the same
`product_id` commit cleanly afterward; confirmed `_unique_child_bot_name`
produces distinct incrementing names; confirmed both coin-selection
functions now return an already-claimed coin when it's genuinely the
best candidate; confirmed a real second branch spawns onto
POL-USD with a distinct name via the exact same code path
`_maybe_spawn_child()` uses; and confirmed the real "Trade this"
FastAPI endpoint itself now succeeds on POL-USD instead of returning
the 409 that was actually seen live.

---

## Real follow-up bug from shared-coin branches: a bot_name collision required manual retry, now self-heals

The account owner hit a real, live error right after the shared-coin
change above shipped: tapping "Start new $50 branch" produced **"Could
not start a new branch: crypto_tree_xrp_usd_2 was just created by
another branch - try again"**, with the explicit ask "fix it" - the
dialog blocked the spawn and needed a manual retry click.

Root cause: `_unique_child_bot_name()` computes the next free name with
a plain `SELECT`, then the caller does a separate `INSERT` - a real gap
a concurrent spawn can land in between and claim the identical name
first. This is now a much more likely real race than before the
shared-coin change: with `product_id` no longer unique, `_maybe_spawn_child()`
runs its catch-up check every single cycle for every branch (not just
after a sell), so the coordinator itself is now constantly a candidate
to race a manual dashboard click - or two branches crossing their own
tier at nearly the same real moment can independently compute the same
"next free" name for the same coin. The `bot_name` unique index (never
dropped - only `product_id`'s was) correctly caught this as a real
`IntegrityError`, but the old code treated ANY collision as a dead end:
the manual endpoints surfaced a 409 requiring the account owner to
click again by hand, and the organic path just gave up until next
cycle - working as designed, but "designed to make a person retry a
race that resolves itself in milliseconds" was never actually the
right design.

Fixed with a new shared `spawn_child_branch_with_retry()` in
`crypto_family_tree_bot.py`: recomputes a fresh `_unique_child_bot_name()`
and retries the insert up to 5 times before giving up, so a transient
collision resolves itself server-side instead of being handed to the
account owner as an error. Both manual spawn endpoints
(`POST .../spawn-branch` and `POST .../spawn-branch/{product_id}` in
`routers/trading_dashboard.py`) now call this helper directly instead
of doing their own single-shot name-then-insert, and only raise (409)
if every one of the 5 attempts genuinely collides - a real, repeated
pileup, not a single unlucky race. `_maybe_spawn_child()`'s own organic
spawn (which keeps the parent's seed deduction atomic with the child
insert, unlike the manual endpoints) got the same retry loop inline,
recomputing the child name each pass while keeping that atomicity -
still falls back to "retry next cycle" only if every one of its 5
attempts also collides.

Verified offline with a dedicated test that reproduces the exact real
scenario: seeds `crypto_tree_xrp_usd`, then simulates a concurrent
spawn winning the race for `crypto_tree_xrp_usd_2` in the exact gap
between the name-check and the insert (a mocked `_unique_child_bot_name`
that inserts a competing row the first time it's called) - confirms the
retry silently lands on `crypto_tree_xrp_usd_3` instead of raising,
confirms the real `POST /family-tree-status/spawn-branch` FastAPI
endpoint itself now succeeds through the same simulated collision
instead of returning the 409 the account owner actually saw, and
confirms a genuine total pileup (every retry attempt collides) still
raises a clear error rather than looping forever or silently dropping
the spawn. Full existing regression suite (10 prior scratch test files,
including both shared-coin-branches tests from the original feature)
re-run clean alongside it.

---

## Real-time "should I sell this?" advisory (💡 Sell advice button)

Per the account owner's explicit request, right after being talked out of
taking a thin, fee-losing profit on BTC by hand ("give me some answers...
put a button there that would give me advice on like now it would be a
good time to sell and then tell me why"): a new 💡 Sell advice button on
every branch card with an open position (including BTC's root card),
revealing a verdict + plain-English reason on tap.

Deliberately NOT a separate heuristic invented for the dashboard -
`compute_sell_advice()` in `crypto_family_tree_bot.py` reuses the exact
same three real exit checks `run_branch_cycle()` evaluates every single
cycle (TARGET hit, STOP hit, PEAK PROFIT GIVEBACK past
`MAX_PROFIT_GIVEBACK_USD`), so the advice can never disagree with what the
bot is actually about to do on its own. Three verdicts:

- **🔴 Good time to sell** - one of the three real automatic exit
  conditions is already true; the bot will do this on its own next cycle
  regardless of whether the account owner acts first.
- **🟡 Watch closely** - real unrealized profit has pulled back at least
  60% of the way toward the giveback cap, but hasn't crossed it yet.
- **🟢 Hold for now** - none of the above; either selling right now
  wouldn't clear real round-trip fees, or it would, but the real target is
  still meaningfully further out for a bigger real win, with the stop
  already protecting the downside in the meantime.

Wired into `GET /family-tree-status`'s existing per-branch `position`
payload as `sell_advice` (`routers/trading_dashboard.py`) - no new
endpoint, no extra fetch on button-click, since the dashboard already
polls this endpoint every 15s and already has the current live price
needed to compute it. `family_tree_dashboard.html`'s button just toggles
visibility of an already-rendered panel.

Verified offline against the exact real BTC numbers from the screenshot
that prompted this (entry $76,925.78, now $77,391.39, target $78,079.67,
breakeven stop, +$1.03 shown profit) - confirms it correctly advises HOLD
(real net profit after fees is only ~$0.25, target is still ~0.89%
further out) - plus dedicated cases for a real TARGET hit, a real STOP
hit, a real giveback-cap breach, approaching-but-not-past the giveback
cap, and a real profit comfortably clear of fees with the target still
far off. Full existing regression suite (11 prior scratch test files) run
clean alongside it.

---

## Sell advice now includes the real coin-selection backtest history too

Per the account owner's explicit follow-up, while looking at the real
`crypto_selection_backtest.py` results table live: "use whatever system
or however this right here is getting its information" to help the sell
advice too. `get_latest_backtest_result()` in `crypto_family_tree_bot.py`
reads the exact same `CryptoBacktestRun` rows that page's table and the
automatic coin-exclusion rule already read (`_compute_auto_excluded_coins`)
- not a new or separately-computed number - and returns the most recent
real run for a coin (trades, win rate, ROI). Wired into
`GET /family-tree-status`'s existing position payload as
`historical_backtest`, alongside (never replacing) the live `sell_advice`
verdict - purely additional real context, e.g. "Real backtest (Aug 24):
33 trades, 48.5% win rate, +4.6% ROI", shown in the same advice panel
under the live reasoning. The verdict itself stays tied only to the live
TARGET/STOP/GIVEBACK checks, exactly as before - this never overrides it.

Verified offline: a coin with no real backtest run on record returns
`None` (not a fabricated number); a coin with multiple real runs on
record returns the MOST RECENT one, matching the real screenshot's
XRP-USD row (33 trades, 48.5% win rate, +4.6% ROI) exactly; and the real
`/family-tree-status` endpoint attaches this context to a branch's
position alongside its still-independent live `sell_advice` verdict.

---

## Real bug found and fixed: spawn retries could still exhaust under real concurrent contention

Right after the collision-retry fix above shipped, the account owner hit
the wall it was supposed to prevent: **"Could not start a new branch:
crypto_tree_xrp_usd_2 collided with another branch on every retry (5x) -
try again"** - all 5 retries failed, not just one. They'd also just
deposited a real $150 into Coinbase specifically to fund more spawns
(confirmed via a real Coinbase balance screenshot - not a funding issue,
`spendable_for_spawn` was never the blocker here).

Root cause: every branch's 30-second cycle timer starts from roughly the
same moment (server boot), so when SEVERAL branches cross their unlock
tier in the same cycle window - realistic with many branches sitting near
100% "Next spawn" at once, as the dashboard was showing - they can all
call `get_next_eligible_product_id()` in the same instant. Since that
function deterministically picks the single least-crowded coin (not a
random tiebreak), several different parent branches converge on the
IDENTICAL target coin and race to spawn a child on it at essentially the
same moment. The original fix's retry loop had zero delay between
attempts, so several racers retrying in immediate lockstep could keep
landing on the same instant as each other's next retry - colliding again
and again instead of naturally spreading out, exhausting all 5 attempts
in a real, sustained pileup rather than a single transient race.

Fixed in both `spawn_child_branch_with_retry()` (manual spawn endpoints)
and `_maybe_spawn_child()`'s inline retry loop (organic per-cycle spawn):
raised attempts from 5 to 12, and added a small random jitter sleep
(0.05-0.4s) before every retry after the first. Real concurrent racers
essentially never pick the same random delay twice in a row, so this
breaks the lockstep almost immediately even with several branches racing
for the same coin at once - each one settles onto a distinct `_N` suffix
within a fraction of a second instead of colliding indefinitely.

Verified offline with a dedicated test that reproduces the actual
concurrency shape of the real failure - 6 real concurrent
`spawn_child_branch_with_retry()` calls fired at once via
`asyncio.gather()`, all racing for the same coin - confirming all 6 now
succeed with 6 distinct real branch names (`crypto_tree_xrp_usd_2`
through `_7`) in under half a second, instead of any of them exhausting
their retries. Full existing regression suite (13 prior scratch test
files) re-run clean alongside it.

---

## Real follow-up: spawn collisions kept exhausting even 12 retries+jitter - the root cause was one level deeper

Right after the 12-attempts+jitter fix above shipped, the account owner
hit the exact same wall again, still at the new numbers: **"Could not
start a new branch: crypto_tree_xrp_usd_2 collided with another branch on
every retry (12x) - try again"**. Not a one-off - happening on a
sustained, repeated basis while several coins were sitting at or near
their spawn tier.

More retries and jitter alone couldn't fix this because the real problem
wasn't purely about *timing* - it was that `get_next_eligible_product_id()`
always deterministically picked the exact same coin. Its tie-break used
fixed `COIN_FAMILY_TREE` list order, so whenever multiple coins were tied
at the lowest branch count (very likely - most coins sit at 0 or 1
branches, and by this point several of the negative-ROI coins from the
earlier backtest screenshot, like LTC/LINK/SEI/ATOM/AAVE, had likely
already been auto-excluded by the automatic backtest-exclusion layer,
narrowing the real eligible pool down to just a couple of standout
positive-ROI coins like XRP-USD), EVERY concurrent spawner - manual
"Start new $50 branch" clicks, "Trade this" on the backtest page, AND the
coordinator's own per-cycle catch-up spawn firing across every eligible
branch - independently computed and agreed on the IDENTICAL target coin.
That funneled all of the tree's real concurrent spawn demand onto one
single coin instead of naturally spreading across whichever coins were
actually tied, guaranteeing sustained, repeated collisions no matter how
many times a losing attempt retried.

Fixed in `get_next_eligible_product_id()`: ties are now broken with a
real random pick (`random.choice`) among every coin AT the minimum
branch count, not the first one in list order. A genuinely unclaimed
coin still always wins outright when it's the sole minimum (no false
randomization there) - this only changes behavior when 2+ coins are
truly tied, spreading real concurrent demand across all of them instead
of funneling it onto one.

Verified offline: with 4 coins tied at count 0, 200 calls spread across
all 4 instead of only ever returning one; a coin that's genuinely the
sole zero-count candidate still always wins with no randomization noise;
and the real concurrency shape that caused the actual failure - 8 real
concurrent `spawn_child_branch_with_retry()` calls with only 3 coins
tied-eligible - now all succeed, spread naturally across all 3 coins
(e.g. 4/3/1) instead of every attempt colliding on a single one. Full
existing regression suite (14 prior scratch test files) re-run clean
alongside it.

---

## Top-N rotating coin pool - the tree now concentrates on its real best performers

Per the account owner's explicit request, right after seeing the real
36-coin backtest table: "doing 15 for now out of 28 and rotate them as
it goes... if it's more profitable then it jumps up there and be able to
get in this place and then we make money off of it." Rather than every
non-excluded coin in `COIN_FAMILY_TREE` being a candidate, the tree now
concentrates spawns and coin-switches on the real top
`TOP_N_ELIGIBLE_COINS` (15 default, env-overridable) coins by latest
backtest ROI - a coin ranks IN the instant a real backtest run puts it in
the top 15, and ranks back OUT the instant a fresher run (the existing
daily automatic run, or a manual "Run Backtest" click) drops it below the
cut. Live, not a snapshot - `_compute_top_ranked_coins()` re-reads
`CryptoBacktestRun` fresh on every call, so a new run's effect is visible
on the very next coin-selection call.

Implementation: `_compute_top_ranked_coins()` reads the single latest
real ROI per coin (one query, ordered by `run_at` descending, first row
per `product_id` kept - deliberately not one query per coin, since this
sits on the hot path for every spawn/coin-switch call across every
branch) and returns the top-N set, or `None` if fewer than
`TOP_N_ELIGIBLE_COINS` coins have any real backtest run yet. That `None`
is a deliberate cold-start guard: `get_effective_excluded_coins()` skips
the rank filter entirely in that case, rather than accidentally excluding
every coin in the tree because most of them still show as "unranked" -
the filter only ever activates once there's real evidence to fill a
top-N cut. Wired into the existing `get_effective_excluded_coins()`
(unioned on top of the manual and auto-exclusion layers, not replacing
them) - both `get_next_eligible_product_id()` and
`find_most_volatile_unclaimed_coin()` already read that one function, so
no other call site needed to change. A coin manually or auto-excluded for
other real reasons stays excluded even if it ranks in the top 15 by ROI
alone - the layers stack, they don't override each other. A branch
already holding a coin that rotates out of the top 15 is never
force-sold, same as every other exclusion layer - it keeps running under
its own rules and simply won't be offered that coin again once it exits.

Verified offline: the cold-start guard correctly skips the filter with
fewer than 15 ranked coins (would otherwise lock the whole tree out of
spawning); a real top-N ranking correctly excludes the bottom performers
once enough real data exists; a fresh backtest run immediately rotates
the eligible set on the very next call (a coin jumping to the best ROI
rotates in, bumping the previous bottom-of-top-N coin out); and the
manual exclusion layer still holds a coin out even when it would
otherwise rank inside the top N by ROI alone. Full existing regression
suite (15 prior scratch test files) re-run clean alongside it.

---

## Real follow-up: spawn collisions on an explicitly-picked coin still exhausted 12 attempts - randomized-tiebreak couldn't help here

Right after the randomized coin-selection tiebreak fix above shipped, a
DIFFERENT real failure showed up: **"Could not start a branch on POL-USD:
crypto_tree_pol_usd collided with another branch on every retry (12x) -
try again"** - this time from the explicit-coin `spawn_family_tree_branch_on_coin`
endpoint ("Trade this" on a specific backtest-page row), not the auto-pick
endpoint the previous fix targeted.

The randomized tiebreak in `get_next_eligible_product_id()` can only help
when the BOT is choosing which coin to spawn on - it has multiple tied
candidates to spread across. This path is different: the caller (a
person tapping "Trade this" on one specific coin, possibly several times,
or racing the auto-spawn coordinator which independently picked the same
coin) hands in one FIXED coin with no alternative to diversify toward.
Every concurrent caller targeting that same explicit coin still computes
the identical sequential "next free" name search, so heavy enough real
contention on one single coin could still exhaust even 12 attempts+jitter
- observed live on POL-USD, which had just become the #1-ranked coin
after the top-N rotation feature shipped, making it a natural magnet for
simultaneous real spawn attempts from multiple sources at once.

Fixed with a second, complementary mechanism in `_unique_child_bot_name()`:
a new `randomize` parameter. The first few retry attempts (0-3) still try
the clean sequential name (`crypto_tree_{coin}_2`, `_3`, ...) for the
common, low-contention case - nothing changes there. From attempt 4
onward, both `spawn_child_branch_with_retry()` and `_maybe_spawn_child()`'s
inline loop switch to a random numeric suffix instead of continuing the
sequential search - a random space large enough that two real concurrent
callers landing on the identical suffix is vanishingly unlikely no matter
how many are racing for that one coin, unlike sequential numbering which
every racer computes identically and can genuinely run out of room under
heavy enough real contention.

Verified offline with the actual pathological shape of the failure: 20
real concurrent `spawn_child_branch_with_retry()` calls all explicitly
targeting the SAME single coin (no diversification possible) - all 20 now
succeed with distinct real names, instead of any exhausting their
retries. Confirmed the zero-contention case is untouched - a real,
uncontested spawn still gets the plain, human-readable name with no
unnecessary randomization. Full existing regression suite (16 prior
scratch test files) re-run clean alongside it.

---

## Real order-rejection reason now visible on spawn-collision errors too, after a third occurrence exposed the old code was guessing

Right after the randomized-suffix fallback above shipped, the account
owner hit the wall a THIRD time - but this occurrence was different in a
way that matters: **"Could not start a new branch:
crypto_tree_xrp_usd_660194 collided with another branch on every retry
(12x) - try again"**. `660194` is one of the new random 6-digit
fallback suffixes (range 1000-999999, ~999,000 values) - a genuine
random collision on that name specifically, let alone on literally every
one of 12 attempts, is statistically close to impossible for real
concurrent contention to produce. That mismatch was the tell: the
`except IntegrityError` handler in both `spawn_child_branch_with_retry()`
and `_maybe_spawn_child()`'s inline loop was silently assuming EVERY
`IntegrityError` on that insert meant "bot_name already taken" without
ever actually checking - if the real cause were something else entirely
(a different constraint, a data problem, anything), the retry loop would
burn through all 12 attempts pointlessly and then report a misleading
"collided with another branch" message, with the actual real error text
thrown away.

Fixed the same way `_describe_order_rejection()` already fixed this
exact class of problem for Coinbase order rejections earlier this
session: capture the real underlying driver error (`e.orig`, the actual
DBAPI exception SQLAlchemy wraps) on every failed attempt, and include it
in the final message - `"...collided with another branch on every retry
(12x) - real DB error: <the actual text> - try again"` for the manual
endpoints, and the same detail appended to `_maybe_spawn_child()`'s log
line for the organic per-cycle path. This does not, by itself, fix
whatever the real underlying cause turns out to be - it makes the NEXT
occurrence immediately diagnosable from the error message itself instead
of requiring another round of guessing at increasingly elaborate
collision-avoidance schemes that were never the actual problem.

Verified offline with a dedicated test that mocks a real `IntegrityError`
wrapping a real driver error (`NOT NULL constraint failed: ...`) and
confirms that exact text now appears in the raised message instead of
being silently discarded. Full existing regression suite (17 prior
scratch test files) re-run clean alongside it.

---

## Per-branch cycle jitter, per the account owner's own real observation about WHY branches keep colliding

After a night of reactive fixes to the same underlying collision (more
retries, randomized coin picks, randomized name suffixes, surfacing the
real DB error), the account owner asked the right root-cause question
directly: is 30 seconds the fastest the cycle can run, or "can we change
it to every coin has its own time cycle... so it don't run into each
other." Checking `_branch_thread_main()` confirmed the real mechanism
behind tonight's whole pattern of collisions: every branch's cycle timer
effectively starts from the SAME moment - the coordinator's startup scan
starts every existing branch's thread back-to-back, and a freshly
spawned branch's thread starts immediately too - and the old code's bare
`time.sleep(CYCLE_SECONDS)` never let that initial clustering drift
apart. Branches that started together stayed in lockstep, cycle after
cycle, for as long as the process ran - which is exactly what kept
multiple branches re-targeting the same spawn candidate at the same
instant, night after night, no matter how much retry logic got added
downstream of it.

`CYCLE_SECONDS` itself (`BTC_COMPOUND_CYCLE_SECONDS` env var, 30s
default) was already changeable without a code change, but lowering it
isn't the right lever - it multiplies real Coinbase API load across
every branch every cycle, and doesn't address the real problem, which is
correlation between branches' timers, not the interval length itself.
Per-coin/per-branch fully independent cycle timing (the account owner's
literal suggestion) would also work but needs a real anchor - `_branch_thread_main()`
already had one available for free: real per-branch execution-time
variance already introduces some organic drift, it just isn't
guaranteed or fast-acting.

Fixed by jittering the recurring sleep itself:
`time.sleep(CYCLE_SECONDS + random.uniform(-CYCLE_SECONDS * 0.1, CYCLE_SECONDS * 0.1))`
- a modest +/-10% (27-33s on the 30s default) that doesn't meaningfully
change real trading responsiveness, but guarantees every branch's cycle
boundary keeps wandering relative to every other branch's. A group of
branches that started in perfect lockstep spreads across the full window
within a handful of real cycles instead of staying correlated
indefinitely - this doesn't replace any of tonight's other collision
fixes, it reduces how often they're even needed in the first place, by
attacking the actual root cause instead of another symptom. Deliberately
does NOT delay a branch's very first cycle (a freshly spawned $50
child still checks for its buy immediately) - only the recurring sleep
between cycles gets the jitter.

Verified offline (real multi-minute thread timing isn't practical to
assert in an automated test): every jittered duration stays within the
documented +/-10% bound across 2000 samples; real statistical spread
across the possible range, not clustered at exactly 30.0s every time;
and a simulated group of branches that started in perfect lockstep
measurably spreads apart (13+ seconds among 6 branches after 5 real
cycles) instead of staying at zero spread, which is what the old fixed
interval would have produced forever. Full existing regression suite (18
prior scratch test files) re-run clean alongside it.

---

## BTC root's next-child requirement lowered to $50, and force-fixed a spawn that had been stuck for hours

Every single screenshot across an entire session showed `crypto_btc_compound`
frozen at Balance $121.93 / Floor $150.00 / "Next spawn" 100% - the exact
stuck state the earlier per-cycle floor self-heal fix was supposed to
resolve on its very first cycle, across several real redeploys since. Per
the account owner's explicit request ("let Bitcoin have its next child at
$50... push that now"), rather than trust the reactive self-heal a
further time, this forces the fix directly.

Two real changes:

1. **`ROOT_UNLOCK_TIER_USD` (new, $50 default)** - root's OWN
   requirement to spawn its NEXT child is now half of the regular
   `UNLOCK_TIER_USD` every other branch uses. `_maybe_spawn_child()` now
   computes `own_increment = ROOT_UNLOCK_TIER_USD if branch is root else
   UNLOCK_TIER_USD` when advancing `next_unlock_tier` after a spawn - a
   spawned CHILD's own first tier is completely unaffected, still the
   regular $100.
2. **`_force_root_spawn_ready()`** (new one-time startup migration, safe
   to run every deploy) - loads root directly, and if it's already
   crossed its tier but is still floor-blocked, forces the exact same
   floor-heal the reactive per-cycle path was supposed to do, then
   immediately calls `_maybe_spawn_child(root)` right there at startup -
   no dependency on root's own thread reaching its next cycle tick. A
   no-op once root has nothing new to spawn.

Verified offline by reproducing the EXACT real stuck numbers from the
live screenshots (Balance $121.93, Floor $150.00, next_unlock_tier
$100.00): confirms the floor force-heals to $100.00 and a real child
spawns in the same startup call; confirms root's own next tier becomes
$150.00 (100 + the new $50 increment), not $200.00; confirms a spawned
child still gets the regular $100 first tier; confirms a completely
separate, non-root branch is totally unaffected by this change (still
uses the regular $100 increment); and confirms the migration is a safe
no-op once root hasn't crossed its own tier yet. Full existing regression
suite (19 prior scratch test files) re-run clean alongside it.

---

## `_force_root_spawn_ready()` hardened after two redeploys still showed no change

After pushing the force-root-spawn fix above, the account owner redeployed
twice (confirmed via a Railway deploy-log screenshot showing a fresh
active deployment) and BTC's floor still showed $150.00, unchanged. The
original version of `_force_root_spawn_ready()` had no error handling of
its own - unlike every OTHER one-time startup migration in this file,
which all wrap their body in `try/except` and log a warning rather than
raise. If this one hit any real, unexpected exception, it could have
silently failed, or - worse - propagated up through `run()`'s startup
sequence and blocked every later step (every branch thread launching,
the coordinator scan loop) from ever running, without any log to explain
why.

Fixed by wrapping the whole function the same defensive way as its
siblings, and adding real diagnostic logging that fires unconditionally
at the top: `root balance $X | next_unlock_tier $Y | floor $Z` before any
decision is made, plus explicit log lines for both the "hasn't crossed
its tier yet" early-return and the "done" completion. This doesn't
change what the fix DOES - it makes the actual live state (and any real
failure) visible in Railway's logs on the very next deploy, instead of
continuing to guess blind at why a fix that tests correctly offline
isn't visibly taking effect live.

---

## The real root cause of the entire night's spawn-collision saga, finally exposed by the earlier diagnostic fix

The diagnostic-error-capture fix added earlier this session (surface
`e.orig` instead of guessing "bot_name taken") finally paid off with a
real, live error text: **"Could not start a new branch:
crypto_tree_pol_usd_138698 collided with another branch on every retry
(12x) - real DB error: `<class 'asyncpg.exceptions.UniqueViolationError'>:
duplicate key value violates unique constraint
ix_crypto_tree_branches_product_id_unique... DETAIL: Key
(product_id)=(POL-USD) already exists."`**

This was never a `bot_name` race. Every single spawn-collision error
across the entire night - the randomized suffixes, the jitter, the
lockstep-contention fixes - was fighting the wrong constraint. The real
conflict was on `product_id`: the OLD one-coin-per-branch uniqueness
index (`ix_crypto_tree_branches_product_id_unique`) from before the
shared-coin feature shipped was **still present in production**, hours
after `_drop_product_id_unique_index()` was supposed to have removed it.
No amount of retrying with a different `bot_name` could ever have
satisfied a conflict on which *coin* a branch holds - every attempt to
spawn a second branch onto an already-claimed coin was doomed from the
first try, regardless of how many random names got tried afterward.

Root cause of the migration's own silent failure: `DROP INDEX IF EXISTS`
never raises in Postgres whether or not anything was actually removed,
so the old version's `log.info("...removed...")` was never real proof -
it fired unconditionally as long as no exception occurred, and nobody
could tell from the logs whether the drop had actually worked.

Fixed in two parts:
1. **`_drop_product_id_unique_index()` now verifies its own real outcome**
   against Postgres's own system catalog (`pg_indexes`) after the drop
   attempt, tries `ALTER TABLE...DROP CONSTRAINT` as a fallback if the
   plain `DROP INDEX` didn't actually clear it, and logs at ERROR level
   (impossible to miss in Railway logs) if the index is confirmed still
   present after both attempts - instead of an unverified INFO line.
2. **Both retry loops** (`spawn_child_branch_with_retry()` and
   `_maybe_spawn_child()`'s inline loop) now recognize a real
   `product_id` conflict specifically (checking the captured error text)
   and fail fast after the very first attempt with an honest, specific
   message - instead of burning all 12 attempts (each with a real jitter
   delay) against a constraint that retrying a name could never satisfy.
   A genuine `bot_name` collision (the real, different failure mode
   already fixed earlier) still retries normally - this fast-fail path
   only triggers on the specific `product_id` conflict.

Verified offline: a real pre-existing unique index is confirmed actually
dropped (not just assumed) and two branches on the same `product_id`
commit cleanly afterward; the migration is a safe no-op on a fresh
deployment or on local SQLite dev (where the Postgres-only verification
query can't run, logged plainly rather than crashing); a real
`product_id`-conflict `IntegrityError` (mocked with the exact real
asyncpg error text) makes both retry loops fail after exactly 1 attempt
instead of 12, with the new honest message; and a genuine `bot_name`
collision (mocked separately) still retries and resolves normally,
confirming the fast-fail path is scoped correctly. Full existing
regression suite (20 prior scratch test files) re-run clean alongside it.

---

## BTC-relative-strength backtest comparison tool (shadow mode, additive only)

Per the account owner's explicit request: does a coin's own "is it up
over the last ~25 hours" signal (the only timing check
`find_most_volatile_unclaimed_coin()` uses) actually mean much in a
market where everything is grinding up together? A coin up 2% while BTC
is up 5% is arguably *underperforming*, not a real buy signal. Added a
real, additive comparison tool to `crypto_selection_backtest.py` -
deliberately NOT wired into live trading or the existing production
backtest pipeline, which `_run_scheduled_backtest_and_update_exclusions()`
and the top-15 coin rotation both depend on for real trading decisions
today.

- **`calculate_relative_strength(coin_closes_window, btc_closes_window)`**
  - real alpha: the coin's simple return over a window minus BTC's real
    return over the identical window, on real historical Coinbase candles.
- **`_closest_close_at_or_before()`** - aligns two different coins' candle
  series by real Unix timestamp (via `bisect`), not array index, since
  real historical candle pages can have small gaps at different points
  for different coins.
- **`backtest_one_coin()`** gained an optional `entry_gate` parameter
  (default `None` = unchanged original behavior) - a callback checked at
  every flat decision point, so the BTC-relative-strength filter can be
  layered on as a real entry-timing gate without duplicating the whole
  replay loop. The existing production call site
  (`_backtest_one_coin_with_semaphore`, feeding the live top-15 rotation)
  passes no gate, so its behavior and result schema are byte-for-byte
  unchanged.
- **`run_btc_relative_strength_comparison()`** - new top-level entry
  point: fetches BTC-USD's own real historical candles once (the one
  real extra API cost versus the existing baseline backtest, which never
  loads BTC's own history), then replays EVERY coin's real history twice
  on the identical data - once as the existing baseline, once gated by
  real BTC-relative strength - so the two are directly, fairly
  comparable. Coins get a free pass on the gate until they have ~25 hours
  of their own real history to compute a window from.
- New route `POST /api/trading-dashboard/crypto-selection-backtest/btc-relative-strength`
  (admin-key gated, same pattern as the existing backtest route) and a
  second button + comparison table on `crypto_selection_backtest.html`
  ("▶ Run BTC-Relative Strength Comparison") showing baseline vs.
  filtered trades/ROI per coin side by side, with the real ROI delta
  color-coded.

Verified offline (no live network access in this sandbox, same
documented gap as the existing backtest tool): the alpha calc is
correct for both real outperformance and real underperformance cases;
timestamp alignment correctly handles a genuine gap in one series;
`backtest_one_coin(entry_gate=None)`'s result schema is provably
unchanged (no new keys); a coin fabricated to consistently beat BTC
trades normally through the filter with zero skips; a coin fabricated to
consistently underperform BTC gets heavily blocked (verified via a
real drop in trade count versus its own baseline, accounting for the
free-pass window); and the full `run_btc_relative_strength_comparison()`
pipeline works end-to-end with mocked candle fetches, confirming the
real 4-tuple fetch signature change flows correctly through the
already-existing production path too (`run_full_backtest()` re-verified
directly against the same mocked data).

---

## THE actual root cause of the entire night's spawn-collision saga: the coordinator thread was never running at all

After the verified `_drop_product_id_unique_index()` fix shipped, the
account owner reported the exact same collision (this time on SOL-USD)
still happening - meaning the migration still hadn't actually run in
production. Asked to search Railway's logs for `product_id_unique`
specifically to see which of the migration's own diagnostic lines had
fired; the account owner searched `tree` instead, and that search
surfaced something far more fundamental: only two real log lines
matched, one of them
`WARNING:pgusa:⚠️ Coinbase bot module for CRYPTO_STRATEGY_MODE='"family_tree"' failed to import - bot will not run`.

That `!r` repr is the tell: `'"family_tree"'` is Python's repr of a
12-character string that itself STARTS AND ENDS WITH a literal `"`
character - not the clean 11-character `family_tree` every `==`
comparison in `main.py`'s lifespan startup (`CRYPTO_STRATEGY_MODE ==
"family_tree"` / `"btc_compound"` / `"multi_pair"`) was written to
expect. The real `CRYPTO_STRATEGY_MODE` Railway env var had been set to
the literal value `"family_tree"`, quote characters included - almost
certainly from pasting a value like `CRYPTO_STRATEGY_MODE="family_tree"`
(the shell-style syntax this very file's own docs use) directly into
Railway's raw value field, which stores exactly what's typed with no
shell-quote stripping. Confirmed this was NOT a real import failure by
checking the import site directly: `import crypto_family_tree_bot` at
the top of `main.py` has its own, differently-worded exception handler
(`logging.warning(f"Failed to import crypto_family_tree_bot: {e}")`),
which would also have matched a `tree` search and did NOT appear in the
results - proving the module loaded fine, and the `else` branch's
warning text ("failed to import") was simply wrong about what was
actually happening.

The real, severe consequence: since none of the three `if`/`elif`
branches matched, `crypto_family_tree_bot_module.run()` was never
called from `main.py`'s startup at all - meaning the coordinator thread,
every per-branch `_branch_thread_main()` cycle, and every startup
migration that lives inside `run()` (including `_force_root_spawn_ready()`,
`_lower_existing_unlock_tiers()`, the dedupe migrations, and - critically -
`_drop_product_id_unique_index()` itself) had never executed even once,
for as long as this malformed value had been set. This is exactly why
BTC's floor stayed frozen at $121.93/$150.00 across multiple real
redeploys despite `_force_root_spawn_ready()` supposedly force-fixing it
on every startup, and why the "verified" index-drop migration never
actually got a chance to drop anything in production - both fixes were
completely correct, they simply never ran. Meanwhile the FastAPI manual
endpoints (`routers/trading_dashboard.py`'s `spawn_family_tree_branch`,
`spawn_family_tree_branch_on_coin`, "Trade this") kept working the whole
time, because those call `crypto_family_tree_bot`'s functions directly
on each HTTP request - independent of whether the background
coordinator thread was ever started - which is why the account owner
could still see real, correctly-worded collision errors (proving that
code path executes) while the coordinator-only fixes silently never ran.

Fixed in two parts in `main.py`:
1. `CRYPTO_STRATEGY_MODE` is now normalized right after being read -
   `.strip().strip('"').strip("'").strip()` - so a value with stray
   surrounding quotes or whitespace pasted into Railway's dashboard can't
   silently disable the entire coordinator thread again.
2. The fallback `else` branch's warning no longer claims "failed to
   import" (misleading - the real 2026-08-24 incident had every module
   loading fine) - it now logs the real loaded/None state of all three
   candidate modules alongside the mode string, so a future mismatch of
   any kind is immediately diagnosable from the log line itself instead
   of requiring another round of cross-referencing a different log
   line's exception text to rule out a real import failure.

Verified offline: the exact real malformed value from the live log
(`'"family_tree"'`, both single- and double-quoted variants, and a
whitespace-padded variant) all normalize back to the clean `family_tree`
string the comparison expects, while an already-clean value is left
unchanged.

**Not yet confirmed live**: whether this was the ONLY reason the
coordinator never ran, or whether the `product_id` unique-index drop
still needs to be watched on the next real deploy now that `run()` can
actually execute - the account owner needs to redeploy and check Railway
logs for the coordinator's real startup lines (`✓ Crypto (Coinbase) bot
thread started | family tree coordinator...`) and the `product_id_unique`
migration's own diagnostic lines to confirm both are now actually firing.

**Confirmed live**: the redeploy worked. BTC's floor healed from the
stuck $150.00 to $100.00 and it spawned children immediately at startup
(POL-USD, plus a `crypto_tree_doge_usd_2` landing on an already-claimed
coin with a distinct suffix name, exactly as designed) - the dashboard
showed 11 real branches holding positions, +$6.53 total profit, BTC's
own next spawn already at 62%. The whole night's fix chain - the
CRYPTO_STRATEGY_MODE normalization getting the coordinator running for
the first time, `_force_root_spawn_ready()`, the floor self-heal, and
the retry/naming logic - is verified working together in production, not
just in offline tests.

---

## BTC-relative-strength filter promoted from shadow mode to live entry selection

The BTC-relative-strength comparison tool (see above) was built and kept
deliberately shadow-mode-only, per the account owner's own explicit
choice at the time - additive, non-interfering, answering "would this
help?" before touching anything live. The account owner then ran it for
real: `POST .../crypto-selection-backtest/btc-relative-strength`, 21
coins, 30 real days, $150/trade, 25h lookback. The real results settled
the question kept open until then:

- Almost everything was net-negative at BASELINE over this real 30-day
  window (only XRP-USD +4.7%, DOGE-USD +3.1%, POL-USD +2.2%, ETH-USD
  +1.0% were positive; SHIB-USD -53.9%, WIF-USD -47.5%, PEPE-USD -42.4%
  were the worst) - a real signal this was a rough real month for alts
  generally, independent of any entry-timing fix.
- The FILTERED replay (only enter when a coin's real return over the
  identical ~25h window beats BTC-USD's own real return over that same
  window) showed a positive ROI change on 15 of the 21 real coins tested
  - some substantially: INJ-USD +24.9pp, SHIB-USD +20.0pp, UNI-USD
  +15.6pp, XLM-USD +13.2pp, ICP-USD +11.8pp. XRP-USD - already the best
  baseline performer - improved in both directions under the filter
  (+4.7% -> +9.5%).
- It also cost a handful of the already-positive/near-positive coins a
  little (DOGE-USD -2.9pp, POL-USD -2.1pp, ETH-USD -1.6pp, ATOM-USD
  -3.3pp), and two others meaningfully (LINK-USD -13.2pp, SUI-USD
  -10.1pp) - not a uniform win, but a real net positive across the real
  sample. Given real evidence and an explicit decision to act on it
  ("wire it into live entries now"), this moved from shadow-mode
  diagnostic to an actual live entry gate.

**Implementation** - deliberately reuses the exact same real comparison
`calculate_relative_strength()`/the backtest's `entry_gate` already
validated offline, adapted for the live 5-minute-candle path instead of
the backtest's hourly one:

- `get_price_volatility_and_trend()` in `crypto_btc_compound_bot.py` now
  returns a 5-tuple `(price, atr_pct, is_bullish, rsi, coin_return)` -
  `coin_return` is the coin's real simple return over the same ~25-hour
  candle window the existing bullish/ATR/RSI checks already use (`(closes[-1]
  - closes[0]) / closes[0]`). Only one real caller exists
  (`find_most_volatile_unclaimed_coin()`), so the signature change is
  safe - same reasoning the RSI-filter addition already used for this
  exact function.
- `find_most_volatile_unclaimed_coin()` in `crypto_family_tree_bot.py`
  now fetches BTC-USD's own `get_price_volatility_and_trend()` ONCE,
  concurrently with every other candidate (added to the same
  `asyncio.gather()` call, not a second round-trip), and requires each
  candidate's `coin_return - btc_return > 0` - the identical `alpha > 0`
  threshold the validated backtest gate used, not a new invented number.
  Checked in BOTH the bullish path and the any-volatility fallback,
  composing with the existing RSI-overbought filter (a candidate must
  pass both, same as before).
- **Fails OPEN** when BTC-USD's own data can't be fetched (a real
  network hiccup) - logs a warning and skips the check that cycle rather
  than blocking every candidate on a missing benchmark, matching the
  backtest gate's own already-validated behavior for missing BTC data.

Verified offline: a candidate that beats BTC wins over a higher-ATR
candidate that doesn't (proving the filter actually changes the
outcome, not just logs); the filter fails open and falls back to
pre-existing behavior when BTC-USD's own lookup raises; and the
RSI-overbought filter still composes correctly alongside the new BTC
filter (an overbought-but-BTC-beating candidate is still skipped in
favor of a candidate that clears both checks). Not yet confirmed against
real live entries - needs watching on the dashboard's coin-switch
behavior after the next redeploy, same as every other real trading-logic
change in this file.

---

## Real bug found and fixed: get_price_rsi() blocked every symbol for the first ~4 hours of every trading day

Confirmed live: clicking "Trade this" on USO - its own real 30-day backtest
ranking was the best on the whole board, 69.2% win rate, +16.5% ROI -
failed with `Could not fetch a live price/RSI for USO: Only 23 of the
required 50 5-min bars are available right now`. Root cause:
`get_price_rsi()` in `prop_bot.py` hard-required 50 real 5-min bars before
returning anything at all - but the function only NEEDS 50 bars for one of
its four outputs (`sma50`), and the 14-period RSI it also computes only
needs 15 closes. The real effect: for roughly the first ~4 hours of every
trading day (250 minutes = 50 real 5-min bars since the session opened),
`get_price_rsi()` returned `None` outright - the automatic scanner skipped
every symbol and the manual "Trade this" endpoint refused every click,
real signal or real cash irrelevant, purely because the trading day was
still young. 23 of 50 bars lines up almost exactly with ~1h53m elapsed
since a 8:30 CDT open, confirming this wasn't a data-fetch failure, just
an overly strict floor.

Fixed by lowering the hard floor to `MIN_BARS_FOR_RSI = 15` (the real
minimum the RSI calculation needs) and making `sma50` optional -
`None` below 50 real bars, a real computed value at or above it. The one
caller that reads `sma50` (`try_open`'s entry validation) was already
written to tolerate it being unavailable, via `data.get("sma50", price)` -
except that fallback only catches a *missing* key, not an explicit `None`,
which is what `get_price_rsi()` now always returns as a key even when
sma50 itself isn't available. Fixed that call site too:
`data.get("sma50") or price`, so an explicit `None` falls back to `price`
the same way a missing key always did.

Verified offline: the exact real 23-bar count from the live USO failure
now succeeds (previously hard-refused); a genuinely too-thin count (10
bars, below the real 15-bar RSI floor) still correctly refuses - this
isn't a removed safety check, just a floor lowered to what's actually
needed; 50+ bars still produce a real `sma50`, unchanged from before; and
the entry-validation call site's fallback correctly substitutes `price`
for an explicit `None`, not just a missing key.

---

## Real, severe revenue bug found and fixed: every paid video order was charged 1/100th of its real price

Found while investigating an unrelated question (a funnel-builder idea for
the video service) - checked the real current prices in
`calculate_quote_price()` (`routers/orders.py`) to ground the discussion in
real numbers, and found it returning raw dollar figures (`"youtube": 750`,
`"social": 500`, etc.). `quote_request.html` correctly shows this to the
customer as "$750". But `VideoQuoteOrder.quote_price` is documented in this
same file's own schema as **cents**, and is consumed everywhere else that
way - `create_checkout`'s Stripe session passes it straight into
`unit_amount` (cents for USD), and the admin dashboard/revenue
totals/worker payout splits all divide it by 100 to show dollars.
`calculate_quote_price()` was the one place that never multiplied by 100 -
meaning every real paid order, since this flow has existed, charged the
customer 1/100th of the price they saw on the quote page: a "$750" youtube
video actually charged **$7.50** at the real Stripe checkout.

Fixed by returning cents from `calculate_quote_price()` (`base_price_dollars
* 100`) - the one function that was wrong, rather than scattering a `*100`
across every consumer that already correctly assumed cents. Also fixed the
one log line at the quote-creation call site that would otherwise have
started printing raw cents as if they were dollars (`$75000` instead of
`$750.00`) once this landed. `quote_request.html`'s own JS pricing
calculator was never touched - it already computes and displays real
dollar amounts client-side for the quote preview and never reads
`quote_price` back from the API response, so the customer-facing quote
page's display is unaffected either way.

Verified offline against every real video type and the rush-delivery
premium: each now returns the correct cents value matching its displayed
dollar price (e.g. `"youtube"` at normal delivery -> 75000 cents = $750.00,
not $7.50).

**Not yet confirmed**: how much real revenue this actually cost historically
- that needs a look at real Stripe payment history/the `video_quote_orders`
table, which this session has no live access to. Every order paid before
this fix shipped charged the 1/100th price; only orders created after this
deploy will charge correctly.

---

## "Right now" trade eligibility on the stock/ETF backtest page

Real friction found live: clicking "Trade this" on USO - its own real
30-day backtest ranking was the best on the board (73.1% win rate, +16.9%
ROI) - refused with `Mandate check failed: RSI 58.9 not oversold
(threshold: 30)`. Not a bug - `try_open`'s real mean-reversion entry rule
only fires on RSI < 30, and USO's RSI was genuinely neutral at that
moment. But the account owner had no way to know that without clicking
and hitting the refusal, and asked directly for the difference to be
visible: "make it to where it shows me a difference to which ones that I
can click and buy."

Fixed by adding a real, read-only dry run of the exact same checks
`manual_open_prop_position` (the real "Trade this" endpoint) already
runs, in the same order - never a second, looser copy of the gate that
could drift out of sync or, worse, quietly become the manual-only bypass
the account owner explicitly said they did NOT want built a few messages
earlier in this same session.

- New `GET /api/trading-dashboard/alpaca-overview/entry-eligibility`
  (admin-key gated): checks kill conditions and margin safety once
  (account-wide, not per-symbol - if either fails, every symbol is
  reported ineligible with that one shared reason, matching how a real
  click would fail identically on all of them), then per symbol: already
  held? in the approved universe? auto-excluded? real live RSI via the
  same `get_price_rsi`, real `validate_entry` mandate check. Returns
  `{eligible, reason, rsi}` per ticker. Never calls `size_position` or
  `execute_futures_trade` - it only ever reads, never places an order.
- `alpaca_selection_backtest.html` gained a "Right now" column
  (✅ Ready (RSI x.x) / ⏳ real reason) and the "Trade this" button is now
  `disabled` until that symbol's real check comes back eligible - loaded
  automatically after every backtest run, plus a manual "🔄 Refresh trade
  status" link since RSI moves over time independent of re-running the
  whole backtest.

Verified offline against the exact real scenario: a mocked USO at RSI
58.9 (the real live value from the screenshot) is correctly reported
ineligible with the real mandate reason; a genuinely oversold symbol is
reported eligible; a symbol already held skips the RSI fetch entirely;
a real kill condition or `STOP_TRADING` correctly blocks every symbol
with one shared reason rather than looking like 11 separate failures.

---

## Higher-timeframe trend comparison for crypto (shadow mode, additive only)

The Alpaca side has always had a real 1-hour SMA20/SMA50 trend
confirmation filter on new entries (`get_higher_tf_trend()` in
`prop_bot.py` - blocks a long entry when the 1-hour trend is DOWN). The
account owner asked directly whether the crypto family-tree side needs
the same thing. Rather than guess, this ports the idea into the existing
shadow-mode backtest-comparison pattern (`run_btc_relative_strength_comparison()`
already established this shape) so it can be tested against real
historical data before touching live entries.

- **`_make_higher_tf_trend_gate(closes, sma_short=20, sma_long=50)`**
  (`crypto_selection_backtest.py`) - the crypto-side analog of
  `get_higher_tf_trend()`, same SMA20/SMA50 pairing. Unlike the Alpaca
  version, needs no separate coarser-timeframe fetch: this backtest
  already replays on real hourly Coinbase candles, so the SMA20/SMA50 is
  computed directly off the same closes array already being replayed -
  no extra API cost versus the plain baseline backtest. Only allows a new
  long entry when SMA20 > SMA50 (a genuine uptrend); free pass for the
  first `sma_long` candles, matching every other gate in this file's
  "don't block on missing history" rule.
- **`run_higher_tf_trend_comparison()`** - same shape as the existing
  BTC-relative-strength comparison: replays every coin's real history
  twice (baseline vs SMA-trend-gated) using the exact same real
  target/stop/breakeven/giveback rules, so the two are directly
  comparable.
- New route `POST /api/trading-dashboard/crypto-selection-backtest/higher-tf-trend`
  (admin-key gated) and a third button + comparison table on
  `crypto_selection_backtest.html` ("▶ Run Higher-Timeframe Trend
  Comparison"), same pattern as the existing backtest/BTC-relative
  buttons on that page.

Verified offline (no live network access in this sandbox, same
documented gap as every other backtest tool in this file - confirmed
again directly via `curl` to `api.exchange.coinbase.com`, a real 403 at
the outbound proxy before reaching Coinbase): the gate correctly
identifies a real uptrend (SMA20 > SMA50) vs downtrend on synthetic
series with hand-computable SMA values; gives a free pass before enough
history exists; actually suppresses real entries during a synthetic
downtrend window and allows them once a synthetic uptrend takes hold
when wired into the real `backtest_one_coin()` replay loop (not just
correct in isolation); and the end-to-end comparison function returns
the correct schema and correctly skips a coin with too little history.

**Not yet confirmed against real historical Coinbase data** - the
account owner needs to click the new button on the live deployed
backtest page and share the results before any real recommendation on
whether to wire this into live entries can be made, the same way the
BTC-relative-strength filter's real 30-day results were what justified
promoting THAT filter from shadow mode to live. This one is still
shadow-mode only; nothing here changes what the live bot buys.

---

## Live "what's bullish right now" coin watchlist (real-time, not backtested)

The account owner looked at the 30-day backtest table on
`crypto_selection_backtest.html` and asked directly: "this is [30-day]
stuff, I need to know what's bullish right now." A real, fair
distinction - the backtest replays 30 days of history to rank coins by
strategy performance; it was never meant to answer "which coins are
trending well at this exact moment."

- **`get_live_coin_snapshot()`** (`crypto_family_tree_bot.py`) - reuses
  the exact same real, live checks `find_most_volatile_unclaimed_coin()`
  already runs at the moment a branch needs to pick a coin (same ~25h
  bullish/ATR/RSI lookup via `engine.get_price_volatility_and_trend()`,
  same `engine.ENTRY_MAX_RSI` overbought filter, same BTC-relative-
  strength alpha check with the same fail-open behavior on a missing
  BTC-USD lookup, same `get_effective_excluded_coins()` and
  `_coin_sale_cooldown_active()` checks) - just reports every coin's
  live status instead of only returning the single best pick. Read-only,
  never places an order.
- New route `GET /api/trading-dashboard/family-tree-status/coin-watchlist`
  (admin-key gated) and a new "🟢 What's bullish right now" panel at the
  TOP of `crypto_selection_backtest.html` (above the 30-day backtest
  section), auto-loaded on page open plus a manual refresh button. Each
  row shows live trend/25h return/RSI/ATR%/BTC-relative alpha and an
  "✅ Eligible now" badge when a branch exiting its current coin at this
  exact instant would actually be allowed to buy that coin - or the
  specific real reason it's blocked (excluded, cooling down, overbought,
  trailing BTC) when it isn't.

Verified offline (no live network access in this sandbox - the same
documented gap as every backtest tool in this file): a coin that's
bullish/not-overbought/beating-BTC is correctly eligible now; an
overbought coin is blocked even while bullish; a coin trailing BTC's own
real return is blocked; an excluded coin and a cooling-down coin are both
blocked regardless of otherwise-good numbers; a coin with a real fetch
failure is reported with `price=None` without crashing the rest of the
snapshot; a failed BTC-USD lookup fails open (never blocks on a missing
benchmark, matching the live picker's own documented behavior); and
eligible-now coins sort first in the returned list.

---

## Manual "Add cash to BTC" - root's own carve-out for a gap every other coin already has a workaround for

The account owner noticed BTC (the tree's permanent root) never showed up
as something they could actively buy more of, and asked directly. The
real reason: since branches can now share a coin, clicking "Trade this"
on a coin you already hold effectively adds more capital to it by
starting a second branch there - but root can NEVER have a sibling
branch (it's the sole permanent foundation), so it had no equivalent path
at all to receive more capital on demand. Discussed scoping this to every
branch vs. just root - decided root-only, since every other coin already
has the "Trade this" workaround and replicating a blended-entry/target/
stop recompute for every branch's own risk math wasn't worth it for a gap
that doesn't actually exist there.

- **`POST /api/trading-dashboard/family-tree-status/root-add-cash`**
  (`{amount}`, admin-key gated) - places a real market buy for the
  requested amount via the exact same `engine.place_market_buy()` every
  automatic entry already uses, then blends it into root's existing
  position with a real quantity-weighted average entry price (or opens a
  fresh position if root happens to be flat), and recomputes target/stop
  off that new blended entry using the same real ATR-based formula every
  fresh buy already uses - so the breakeven ratchet and peak-profit
  giveback tracking stay correctly anchored to root's true cost basis
  afterward.
- Refused (400) if the amount isn't positive, or exceeds the real free
  spendable cash currently sitting outside every branch's own allocated
  balance - the exact same `spendable_for_spawn` calculation (real
  Coinbase cash balance, minus locked profit, minus every FLAT branch's
  own `allocated_usd`) the dashboard's "Start new $50 branch" button is
  already gated on, so this can only ever deploy real money that isn't
  already working somewhere else in the tree. A real Coinbase order
  rejection surfaces the actual captured reason via a 502 (same
  `_last_order_error` pattern used elsewhere), and never touches
  `allocated_usd` or the position row when the order didn't fill.
- New "💰 Add cash to BTC" button on root's card in
  `family_tree_dashboard.html` - shown whenever root has an open position
  (independent of profit state, unlike selling - adding cash never risks
  locking in a loss) and also when root is momentarily flat (the endpoint
  opens a fresh position in that case).

Verified offline against a real throwaway SQLite DB (not mocked ORM
calls, so the actual blended-entry math and `allocated_usd` bookkeeping
round-trip through real rows the same way the bot's own internal helpers
do in production): adding cash to an existing position correctly blends
a quantity-weighted entry and recomputes target/stop off it; adding cash
to a flat root opens a fresh position at the real fill price; a request
exceeding real free spendable cash is refused with the exact real dollar
figure; a non-positive amount is refused; and a real order rejection
surfaces the captured reason via 502 without touching the branch's
balance or position.

---

## "Add cash" generalized from root-only to every branch

Right after shipping root-only "Add cash to BTC", the account owner said
directly: "put that add cash button on all of them why not it won't hurt
it's up to me to use it or not." The underlying real buy/blend/recompute
logic never actually depended on being root, so this was a clean
generalization rather than a rebuild:

- `POST /api/trading-dashboard/family-tree-status/root-add-cash`
  (`{amount}`) is now `POST /api/trading-dashboard/family-tree-status/add-cash/{bot_name}`
  (`{amount}`) - works identically for root or any other branch. Same
  real market buy, same quantity-weighted blended entry, same
  target/stop recompute off the new blended entry, same real
  free-spendable-cash refusal (real balance minus locked profit minus
  every FLAT branch's own `allocated_usd`, including the target branch
  itself if it's currently flat).
- `family_tree_dashboard.html`'s "💰 Add cash to BTC" button is now
  "💰 Add cash to {coin}" on every branch card - root and non-root alike,
  both when holding a position and when momentarily flat.

Verified offline against a real throwaway SQLite DB: root still works
exactly as before (regression check); a real non-root branch (XRP)
blends its OWN entry price and updates its OWN `allocated_usd`, proving
the generalization is real rather than just accepting an ignored
`bot_name` param; and a nonexistent `bot_name` is refused with a clean
404.

---

## Real bug found and fixed: concurrent branches could ask Coinbase for more cash than genuinely existed (INSUFFICIENT_FUND)

The account owner shared a real dashboard screenshot showing several
branches (POL, DOGE, XRP) all displaying the same real rejection at
once: **"⚠️ Last order rejected: INSUFFICIENT_FUND: Insufficient balance
in source account."** Multiple branches rejecting with the identical
real Coinbase error in the same window pointed at a shared-resource race,
not a per-coin problem.

Root cause: each branch's flat-branch buy path (`run_branch_cycle`)
computes `spend = min(branch.allocated_usd, real_balance - locked_usd)`
against its OWN snapshot of the real Coinbase USD balance, fetched fresh
each cycle. With every branch running as an independent, jittered
background thread (`_branch_thread_main`) and nothing coordinating the
one shared real cash pool between them, several branches can
legitimately decide "I can afford this" off the same real balance
snapshot within the same real moment - Coinbase itself has no concept of
one branch "reserving" cash while another's order is in flight, so
whichever order lands second (or third) after the balance has already
moved gets a real, honest rejection. This is also asymmetric with the
sell side: `place_market_sell()` has always clamped its qty against a
fresh real held-balance check immediately before submitting; `place_market_buy()`
had no equivalent - it just spent whatever the caller told it to,
whatever the real balance actually was at the moment the order fired.

Fixed in `place_market_buy()` (`crypto_btc_compound_bot.py`): fetches the
real current USD balance immediately before submitting and clamps
`usd_amount` down to it, mirroring `place_market_sell()`'s existing
pattern. Returns `None` outright (no order placed) if the real balance
is `<= 0` after the clamp. Fails open on a real balance-fetch failure -
uses the original requested amount unchanged rather than blocking a
buy on a data hiccup, matching every other "don't block on missing data"
gate in this codebase. This doesn't eliminate the race outright (two
branches could still both clamp against the identical real balance a
moment before either order lands), but it moves the real balance check
to the last possible instant before the order actually goes out - the
same defensive placement the sell-side clamp already uses - so a branch
now asks Coinbase for at most what's genuinely real right now, and a
partially-covered spend fills for what's actually available instead of
getting rejected outright.

Verified offline (no live network access in this sandbox): a requested
spend exceeding the real balance is clamped down to the exact real
balance in the order actually submitted; a spend already within the real
balance is submitted unchanged; a real balance-fetch failure doesn't
block the buy (original amount used); and a real $0 balance returns
`None` without ever calling the order-placement path. Full existing
regression suite re-run alongside it - the only failures were pre-existing,
unrelated scratch-test staleness from earlier in this session (stale
fixture data, a renamed function, tests needing a local Postgres not
running in this sandbox), none touching `place_market_buy`.

---

## Real, urgent gap found and fixed: the crypto family-tree bot had no STOP_TRADING kill switch

The account owner hit a real moment of distress at the dashboard - Total
Profit -$163.73 across 21 branches - and said directly: "I'm losing
money bad stop this." Checking how to actually pause the bot exposed a
real gap: `prop_bot.py` and `crypto_coinbase_bot.py` have always
respected the `STOP_TRADING` Railway env var as a real, immediate kill
switch on new entries - `crypto_family_tree_bot.py` (the bot actually
shown on that dashboard) never checked it at all. Setting the existing
env var would have paused the OTHER two bots while leaving the one
actually losing money completely unaffected.

Fixed by wiring the same `STOP_TRADING` convention into the family-tree
bot, scoped narrowly and deliberately:

- **`run_branch_cycle()`'s flat-branch buy path** now checks
  `STOP_TRADING` first thing, before any real balance/price lookup or
  order - a flat branch simply sits in cash instead of buying. Placed
  deliberately AFTER every real exit check (STOP HIT, TARGET, breakeven,
  floor-breach) in the function's control flow, not before - an
  already-open real position's protection must never pause, only new
  capital deployment does. Since a sold position's automatic rebuy goes
  through this exact same flat-branch path on its next cycle, this one
  check also correctly stops branches from re-entering after they exit
  while the switch is on - proceeds just sit as cash instead.
- The three manual endpoints that deploy new capital -
  `add_cash_to_branch`, `spawn_family_tree_branch`,
  `spawn_family_tree_branch_on_coin` - now also refuse (400) while
  `STOP_TRADING=true`, matching the precedent already set by Alpaca's
  own manual "Trade this"/entry-eligibility endpoints. A manual sell
  (`close_family_tree_branch`, `root-take-profit`) is untouched - taking
  profit or cutting a loss by hand must always stay available regardless
  of the switch.

Verified offline: a flat branch does not buy while `STOP_TRADING=true`
(no real order placed); the identical branch buys normally once the
var is unset, proving the gate doesn't leak into steady-state behavior;
critically, an open position's real STOP-LOSS exit still fires and sells
normally while `STOP_TRADING=true` - proving existing protection is
never paused, only new entries are; and all three manual endpoints
correctly refuse with a clear 400 while the switch is set.

**To actually stop new crypto entries right now**: set `STOP_TRADING=true`
in Railway's env vars and redeploy. Existing positions keep running under
their real stop-loss/target/breakeven protection either way - this only
stops new money from going in.

---

## Higher-timeframe (hourly SMA20/SMA50) trend filter promoted to live entry selection

Right after the BTC-relative-strength promotion above, the account owner
asked whether the crypto side needed the same 1-hour trend confirmation
the Alpaca side already has. The comparison tool (see the shadow-mode
section above) was run for real: 18 coins, 30 real days, SMA20/SMA50 on
hourly candles, $150/trade. Real results: **15 of 18 coins improved with
the filter, only 3 got worse** (SOL-USD -4.9pp, DOGE-USD -8.0pp,
LINK-USD -5.7pp) - several improvements were large: ADA-USD +24.6pp,
DOT-USD +23.2pp, SHIB-USD +18.8pp, TIA-USD +16.5pp, UNI-USD +15.0pp.
Honest caveat that came with it: this filter mostly CUTS LOSSES rather
than creates wins - most filtered coins are still net-negative on this
real (rough, alt-unfriendly) 30-day sample; only XRP-USD, ADA-USD, and
ETH-USD end up net-positive after filtering. Given a real, mostly-
consistent 83% hit rate and the account owner's explicit "yes," this
moved from shadow-mode diagnostic to a real live entry gate, the same
way the BTC-relative-strength filter did.

Implementation - deliberately uses a SEPARATE real hourly-candle fetch,
not the existing ~25h/5-minute data every other live check already uses:

- **`engine._fetch_hourly_closes(session, product_id, count=50)`**
  (`crypto_btc_compound_bot.py`) - real Coinbase candles at
  `granularity=3600`, trimmed to the most recent `count` hourly closes.
  Kept separate from the existing `_fetch_candles()` (5-min candles) on
  purpose: the real backtest that justified this filter was computed on
  a real 50-HOUR window, and a 5-min-candle substitute would be a
  different, unvalidated filter wearing the same name.
- **`engine.get_higher_tf_trend(session, product_id, sma_short=20, sma_long=50)`**
  - returns `True` (uptrend), `False` (downtrend), or `None` if there
  isn't yet enough real hourly history. Callers must fail OPEN on `None`
  - only a CONFIRMED downtrend blocks anything, matching every other
  "don't block on missing data" gate in this codebase.
- **`find_most_volatile_unclaimed_coin()`** (`crypto_family_tree_bot.py`)
  now fetches this concurrently for every candidate (added to the same
  outer `asyncio.gather`, not a second sequential round-trip) and skips
  any candidate with a confirmed downtrend, composing with the existing
  RSI-overbought and BTC-relative-strength filters (a candidate must
  clear all three).
- **`get_live_coin_snapshot()`** (the "🟢 What's bullish right now" live
  watchlist) updated the same way, reporting a real `higher_tf_uptrend`
  field per coin and folding it into `eligible_now` - so the live
  watchlist never disagrees with what the picker actually does.
  `crypto_selection_backtest.html`'s watchlist panel shows a real
  "Downtrend (1H)" reason badge when this is what's blocking a coin.

Real cost: roughly doubles the live API calls during a coin-switch
search (every candidate now needs both its existing 5-min fetch AND a
new hourly fetch) - acceptable since coin-switches only happen on branch
exits, not every cycle for every branch.

Verified offline (no live network access in this sandbox): a real
Coinbase-shaped hourly candles response is parsed and trimmed correctly;
SMA20/SMA50 correctly identifies a real uptrend vs downtrend; a fetch
failure returns `None` (fails open), never `False`; the live picker
actually SKIPS a candidate with a confirmed downtrend even though every
other check would have picked it, and picks the next-best real
candidate instead; a candidate whose hourly-trend lookup fails is still
correctly treated as eligible (fails open in the real live path, not
just in the isolated function); and the live watchlist's `eligible_now`
correctly reflects a confirmed downtrend. Not yet confirmed against real
live trading outcomes - needs watching on the dashboard's coin-switch
behavior after the next redeploy, same as every other live trading-logic
change in this file.

---

## Every-other real spawn now reinforces the weakest branch instead of always starting a new one

Per the account owner's explicit request, after looking at the real
"Branch Ranking by Allocated Balance" list and seeing a wide spread
between BTC at 74% toward its next tier and several POL branches sitting
at 18-46%: "I want the next spawn to spawn into the coin that is weaker
to help it build it back up... do that with every other spawn." Clarified
via two direct questions before building: (1) alternate every OTHER real
spawn, not every spawn; (2) "weaker" ranked by lowest real percentage
toward the branch's own next spawn tier (`allocated_usd / next_unlock_tier`
- the exact number already shown on the dashboard's "Next spawn" bars),
not raw dollar balance.

- **`_next_spawn_is_reinforcement()`** - a real, persisted counter
  (`SPAWN_ALTERNATION_STATE_KEY`, same generic `TradingBotState` bucket
  `locked_usd`/equity floors already use) that survives restarts. Every
  2nd real spawn (count 2, 4, 6, ...) is a reinforcement turn; odd counts
  are normal. A rare cross-thread race incrementing this could
  occasionally skip/repeat a parity - accepted as a soft allocation
  preference, not a financial safety invariant worth real locking for.
- **`_pick_weakest_branch_for_reinforcement()`** - picks the branch with
  the lowest `allocated_usd / next_unlock_tier` ratio, explicitly
  excluding the branch that's doing the spawning itself (it just crossed
  its OWN tier and is about to have that tier raised, so it would often
  wrongly look "weakest" right at this moment and end up reinforcing
  itself - pointless, functionally identical to not spawning at all).
- **`_deploy_seed_into_weakest_branch()`** - places a real market buy for
  the $50 seed into the weakest branch's current coin and blends it into
  its existing position (or opens a fresh one if flat) - the same real
  quantity-weighted blended-entry and target/stop recompute logic the
  dashboard's "Add cash to {coin}" button already uses, reimplemented
  here (not shared code - `add_cash_to_branch` lives in
  `routers/trading_dashboard.py` behind a FastAPI dependency, and this
  path needed to work standalone from the bot module without touching
  already-tested, deployed endpoint code).
- Wired into **`_maybe_spawn_child()`**, right after the existing
  floor-self-heal block and before the normal coin-search: on a
  reinforcement turn, the spawning branch's $50 seed deduction and
  `next_unlock_tier` increment happen exactly as a normal spawn's do, but
  instead of inserting a new `CryptoTreeBranch` row, the $50 gets a real
  buy into the weakest existing branch. A failed real buy (rejected
  order, missing price data) refunds the $50 and rolls back the tier
  increment on the parent - no real dollars silently deducted with
  nothing to show for it. A reinforcement turn with no OTHER branch yet
  to reinforce (e.g. the very first spawn in a fresh tree) falls through
  to a normal new-branch spawn instead of silently skipping the
  opportunity. The manual "Start new $50 branch" / "Trade this" endpoints
  are completely untouched - this only changes the automatic,
  tier-crossing spawn path.

Verified offline against a real throwaway SQLite DB (not mocked ORM
calls): spawn #1 is normal (new branch created); spawn #2 correctly
skips creating a branch and reinforces the real weakest candidate
(verified by seeding a real 36%-weak branch alongside a real 90%-strong
one and confirming the buy went to the weak one); the reinforced
branch's real `allocated_usd` and `BotPosition` (blended entry, qty)
update correctly; spawn #3 alternates back to normal; a failed real buy
during a reinforcement turn correctly refunds the seed and rolls back
the tier increment; and a reinforcement turn with no other branch to
reinforce falls through to a normal spawn. Full existing regression
suite re-run clean alongside it, including confirming the manual
spawn/add-cash endpoints are unaffected.

---

## Reinforcement rule revised: persistent (not alternating), until every branch clears 50%

Right after the every-other-spawn reinforcement feature shipped, the
account owner watched it live and pushed back: a spawn made a brand-new
branch instead of reinforcing the real weak POL branches they were
pointing at. That was actually the every-OTHER-spawn rule working
exactly as built (half of all spawns are normal, by design) - but the
account owner's real intent, once restated plainly, was different from
the literal "every other" they'd first said: "I know I said every other
coin but... help your weakest one out until everyone is above 50% and
then it is fine until a new coin[s gets weak again]."

Replaced the persisted alternation counter with a threshold check:

- **`_tree_needs_reinforcement()`** (replaces `_next_spawn_is_reinforcement()`)
  - scans every real branch and returns `True` if ANY branch's real
  `allocated_usd / next_unlock_tier` is below `REINFORCEMENT_THRESHOLD_PCT`
  (50% default, `TREE_REINFORCEMENT_THRESHOLD_PCT` env-overridable). No
  more alternating counter/`TradingBotState` row for this - the tree's
  own real state each spawn IS the check now.
- **Every real spawn** reinforces the weakest branch for as long as this
  returns `True` - not just every other one. Once every branch is at or
  above the threshold, spawning goes back to normal (new branches) until
  some branch's real balance eventually drops back below 50% again (e.g.
  from a real stop-loss or floor-breach exit).
- `_pick_weakest_branch_for_reinforcement()` and
  `_deploy_seed_into_weakest_branch()` are unchanged - only the "should
  this spawn reinforce at all" decision changed, not how the reinforcement
  itself is picked or executed.

Verified offline against a real throwaway SQLite DB: two consecutive real
spawns BOTH reinforce (not alternating) while a real branch is still
below 50%, each one correctly targeting whichever real branch is
currently weakest; once every branch is confirmed at/above 50%, the next
spawn correctly creates a new branch again instead of reinforcing.

---

## Real bug found and fixed: a branch could get permanently stuck retrying a sell that could never fill (phantom position from cross-branch balance drift on a shared coin)

Real Railway log screenshots showed the same pattern recurring across
multiple coins, every cycle, forever: `crypto_btc_compound_bot:[BTC-COMPOUND]
POL-USD: clamping sell qty 325.01000000 -> real held balance 0.00000000`,
immediately followed by `nothing sellable after balance/precision clamp
(qty was 0.0)`, and on the family-tree side `crypto_family_tree_bot:[TREE]
crypto_tree_bch_usd: TARGET HIT but sell did not fill - will retry next
cycle` / the same for `crypto_tree_doge_usd_3`. The account owner asked
directly why real branches kept losing and what was going wrong.

Root cause traces back to the earlier shared-coin-branches change (see
above): multiple branches can now hold the same real coin simultaneously,
but Coinbase's real balance for that coin is POOLED across every branch
holding it, while each branch's own `BotPosition.qty` is tracked
separately with zero live reconciliation between the two. Once the real
pooled balance for a coin (POL-USD, BCH-USD, DOGE-USD all hit this) drops
to genuinely 0 - another branch on the same coin sold its share first, or
enough small real-world drift (fees taken in the asset itself, rounding)
accumulated - any branch STILL holding a stale tracked position for that
coin hits its TARGET or STOP, tries to sell, and `place_market_sell()`'s
existing real-balance clamp correctly reduces the doomed sell to 0 qty and
refuses to place it. Before this fix, `_branch_sell_and_settle()` treated
that refusal exactly like any other transient failure - log a warning and
retry the IDENTICAL sell next cycle - which can never succeed, since the
real balance backing it is never coming back. The branch was stuck in a
genuine infinite loop: unable to sell (nothing real left to sell), unable
to buy (still shown as holding a position), completely unmanaged for as
long as the process kept running.

Fixed in two parts:
1. `place_market_sell()` (`crypto_btc_compound_bot.py`): when the real
   balance/precision clamp reduces qty to 0, it now also tags
   `_last_order_error[product_id] = "NOTHING_TO_SELL: ..."` before
   returning `None` - reusing the exact same dict/dashboard-banner
   mechanism `_describe_order_rejection()` already established for real
   Coinbase rejections, so this needed no new UI.
2. `_branch_sell_and_settle()` (`crypto_family_tree_bot.py`): on a failed
   sell, now checks whether `_last_order_error` for that coin carries the
   `NOTHING_TO_SELL` tag. If so - a CONFIRMED phantom position, not a
   guess - it self-heals: clears the stale `BotPosition` via the existing
   `_clear_branch_position()`, starts the same one-cycle sale cooldown a
   real sale would, and (for a non-root branch) picks a new coin through
   the exact same `find_most_volatile_unclaimed_coin()` a normal exit
   already uses. Root (BTC) still never coin-switches, matching its
   existing "stays on BTC-USD by design" behavior - only its stale
   position gets cleared. Deliberately does NOT touch `allocated_usd` and
   does NOT write a `CryptoCoinTradeHistory` row - no real trade happened,
   so there's no real fill price/qty to record, and inventing one would
   fabricate P&L that never actually occurred; the real dollar impact (if
   any) of the underlying cross-branch drift is left exactly where it
   already was, on whichever branch's real trade actually consumed the
   balance. Any OTHER real sell failure (no tag, or a different reason)
   still falls through to the original "will retry next cycle" behavior,
   completely unaffected.

Verified offline with a dedicated test suite reproducing the exact real
log pattern: `place_market_sell()` correctly tags `NOTHING_TO_SELL` only
when the real balance genuinely clamps qty to 0 (not when a real balance
covers the sell); `_branch_sell_and_settle()` correctly clears the stale
position and switches to a new real coin for a non-root branch, while
root's position clears but it stays on BTC-USD and never calls the
coin-switch search; `allocated_usd` is left completely untouched and no
`CryptoCoinTradeHistory` row is written in either case; the one-cycle sale
cooldown starts for the phantom coin same as a real sale; and a genuine,
untagged transient sell failure is completely unaffected - position stays
intact, still retries next cycle exactly as before. Full existing
regression suite re-run alongside it; the only failures were confirmed
pre-existing/stale via `git stash` comparison against the prior commit
(unrelated to this change - a return-signature mismatch in an outdated
test's mock, a renamed function, and a stale hardcoded spawn-tier
constant), none touching `place_market_sell` or `_branch_sell_and_settle`.

**What this does NOT fix**: the underlying cross-branch balance
drift itself - multiple branches sharing a coin with a pooled real
balance and no live reconciliation between their individually-tracked
quantities is still the real architecture (a deliberate choice from
earlier in this session). This fix stops a branch that lands in that gap
from being stuck forever and gets it back to real, working trading
immediately - it does not prevent the gap from occurring again on a
different coin in the future. A structural fix (e.g. tracking real
per-branch balance shares, or reconciling against the real pooled balance
on every cycle) would be a much bigger change and hasn't been requested.

---

## DB-vs-Coinbase reconciliation report, made visible on the dashboard

Right after the phantom-position self-heal fix above, the account owner
shared a pasted third-party analysis of the same real log evidence
(POL-USD clamping to a real balance of 0, "TARGET HIT but sell did not
fill"), arguing the dashboard's "22 branches holding positions" was
dangerously misleading since a DB `BotPosition` row proves the bot
THINKS it holds an asset, not that Coinbase actually has it. That
specific diagnosis was correct and matched what had already been found
and fixed - this makes it directly visible instead of only discoverable
reactively when a stuck sell surfaces it.

`get_reconciliation_report()` (`crypto_family_tree_bot.py`) is
deliberately grouped by ASSET CURRENCY, not by individual branch: since
branches can legitimately share a coin (see "Multiple branches can now
share the same coin" above), Coinbase's real balance for an asset is
POOLED across every branch holding it - comparing one branch's own qty
against the full pooled balance would flag a false "mismatch" on every
shared coin, which by now is most of the tree. The correct comparison is
real_balance vs. the SUM of every branch's tracked qty for that same
asset - `SHORTFALL` only fires when the real pooled balance is
meaningfully below what the tree's combined tracked positions say it
holds (a small proportional tolerance absorbs real fee/rounding dust, not
a strict equality check). A real balance-fetch failure is reported
`unchecked` with the actual error, never silently treated as either ok or
a shortfall. Read-only - fetches real balances, never places an order or
touches the DB.

Real efficiency fix caught before this ever shipped: the dashboard polls
`/family-tree-status` (and now this new endpoint) every 15 seconds, and
`get_asset_balance()` re-fetches and re-paginates the ENTIRE real
Coinbase `/accounts` list from scratch on every call - calling it once
per distinct held currency (potentially 15-20+ with the top-N rotation
active) would have turned one dashboard refresh into a dozen-plus
redundant real Coinbase API calls, every 15 seconds, forever. New
`get_all_asset_balances()` (`crypto_btc_compound_bot.py`) walks the real
account pages exactly once and returns every currency's balance in one
dict, and the reconciliation report uses that single batched fetch
instead of looping `get_asset_balance()` per currency. A currency absent
from the real account list entirely (Coinbase genuinely has no account
for it) is correctly treated as a real 0 balance, not silently skipped -
if the tree tracks a position in it, that's a real SHORTFALL.

New `GET /family-tree-status/reconciliation` (admin-key gated) and a new
"🔍 Reconciliation" panel on `family_tree_dashboard.html`, placed right
under the KPI row for visibility - a green banner when every real asset's
Coinbase balance covers its tracked sum, a red banner naming exactly how
many assets are short when it doesn't, and a per-asset table (branches
sharing it, DB tracked qty, real Coinbase balance, status).

**On the rest of that pasted analysis** (evaluated critically before
acting, same as every other pasted-tool proposal this session):
- **The dual-bot-engine concern was checked directly against the real
  code and does NOT hold**: `main.py`'s startup is a single
  `if/elif/elif` on `CRYPTO_STRATEGY_MODE` (`family_tree` /
  `btc_compound` / `multi_pair`) - only ONE of
  `crypto_family_tree_bot_module.run` / `crypto_btc_compound_bot_module.run`
  / `crypto_coinbase_bot_module.run` is ever started as a thread, never
  more than one. With `CRYPTO_STRATEGY_MODE=family_tree` (confirmed live
  this session), `crypto_btc_compound_bot.py`'s own `run()` loop is never
  invoked at all - that module is only imported and used as a shared
  `engine` library (its buy/sell/target functions) by
  `crypto_family_tree_bot.py`, not run as an independent bot. The root
  branch reusing the literal bot_name `"crypto_btc_compound"`
  (`ROOT_BOT_NAME`) is a deliberate continuity choice (see
  `ensure_root_exists()`), not two processes fighting over one position -
  there is only ever one thread (the root branch's own
  `_branch_thread_main`) writing to that bot_name's `BotPosition` row
  under this strategy mode.
- **The "central execution lock / single coordinator serializing every
  branch's orders" rewrite was deliberately NOT built.** The real risk it
  targets - two branches racing to spend/sell against the same pooled
  real balance at once - is already defended at the point that actually
  matters: `place_market_buy()` and `place_market_sell()` both fetch the
  real Coinbase balance immediately before submitting and clamp to it
  (see the INSUFFICIENT_FUND fix above), so no single order can ever ask
  for more than genuinely exists at that instant. A global lock
  serializing 23 branch threads through one gate would add meaningful
  latency and a single point of failure to a live-money system for a
  marginal additional safety margin over what the per-order clamp already
  provides, and doesn't change the fundamental shared-balance dynamic -
  it only changes how the race is arbitrated. Not ruled out as a future
  option if reconciliation data shows the per-order clamp isn't holding
  up, but not warranted as an immediate rewrite under real financial
  pressure.
- **"Pause all new BUY orders until reconciliation is fixed" was left as
  the account owner's own call, not applied automatically.** The
  `STOP_TRADING=true` kill switch already exists for exactly this (see
  above) - the new reconciliation panel now gives a real, direct answer
  to whether it's actually needed, instead of guessing.

Verified offline against a real throwaway SQLite DB: two branches sharing
one coin are correctly SUMMED into one tracked_qty figure (not reported
as conflicting per-branch numbers); a real balance that covers the
tracked sum is `ok`; a genuine shortfall is flagged `SHORTFALL`; a tiny
real fee/rounding dust gap stays within tolerance and is NOT a false
positive; a currency entirely absent from the real batched balances
(no Coinbase account for it) is correctly treated as a real 0 balance,
not silently skipped; a TOTAL real balance-fetch failure marks every
currently-held asset `unchecked` with the actual error text rather than
false-`ok` or false-`SHORTFALL`; `shortfall_count` reflects only genuine
shortfalls; and SHORTFALL rows sort first. Full existing regression
suite re-run alongside it.

---

## Real crash found via the new status-snapshot feature: Alpaca position fields were raw strings, not numbers

Right after the account owner finished setting up `STATUS_SNAPSHOT_GITHUB_TOKEN`
(a real multi-step GitHub token walkthrough, done live over several
messages), the status-snapshot branch still never appeared. Railway logs
confirmed the daemon WAS starting correctly (`"starting - snapshot every
1800s, branch 'status-snapshots'"` - the token/wiring itself was fine),
but every cycle immediately failed: `WARNING:status_snapshot:[STATUS-
SNAPSHOT] cycle failed: unsupported operand type(s) for -: 'str' and 'str'`.

Root cause: `get_alpaca_overview()`'s `positions` payload
(`routers/trading_dashboard.py`) passes Alpaca's raw REST API position
fields (`qty`, `avg_entry_price`, `current_price`, `market_value`,
`unrealized_pl`, `unrealized_plpc`) straight through with no type
conversion - and Alpaca's real API returns these as JSON STRINGS (e.g.
`"avg_entry_price": "150.25"`), not numbers, unlike `account.get("equity")`
etc. a few lines above in the same function, which correctly wraps
everything in `float(...)`. This bug has existed since this endpoint was
built - it was invisible on `alpaca_dashboard.html` purely because
JavaScript's `-` operator silently coerces strings to numbers
(`"150.25" - "100" === 50.25` is valid JS), so nothing there ever broke.
`status_snapshot.py`'s new `_build_alpaca_section()` was the first REAL
Python consumer of this payload to do actual arithmetic on it
(`(current - entry) * qty`), and Python has no such coercion - hence the
exact real crash.

Fixed with a new `_safe_float()` helper in `routers/trading_dashboard.py`,
applied to every numeric position field in the endpoint's response -
converts a real Alpaca-style numeric string to an actual float, and
returns `None` (never a fabricated `0`) on a genuinely missing or
unparseable value. `alpaca_dashboard.html`'s own JS is completely
unaffected either way (`Number(p.unrealized_pl || 0)` and `fmtUsd(...)`
both already handle a real number just as well as a string) - this fix
only changes the payload's actual type to match what it always claimed to
be.

Verified offline: `_safe_float()` correctly parses real Alpaca-style
numeric strings; returns `None` (not 0, not a crash) for a real missing
or garbage value; passes through a value that's already numeric
unchanged; and a dedicated reproduction confirms the exact real crash -
subtracting two raw Alpaca-style numeric strings raises the identical
real `TypeError` seen in the logs - and confirms the same math succeeds
once both values are run through `_safe_float()` first, matching what
the fixed endpoint now does before ever returning the payload. Full
existing regression suite re-run alongside it; the only failures were
confirmed pre-existing via `git stash` comparison against the prior
commit (an unrelated mock gap in one scratch test, and the same
already-known stale `bot_name` collision in another), neither touching
this fix.

**Not yet confirmed live**: whether the status-snapshot branch actually
appears after this redeploy - needs the account owner to redeploy once
more and a follow-up `git fetch origin status-snapshots` from a session
with normal repo access.

**Update - a second real bug found immediately after the first was
fixed**: the crash above was resolved, but the very next log line
revealed the actual root blocker: `WARNING:status_snapshot:[STATUS-
SNAPSHOT] no .git directory found in this deployment - cannot push`.
Real, confirmed live: Railway's deployed container does not include a
`.git` directory at all - Railway hands the running app its source
files, not a full git checkout with history (no `.dockerignore` exists
in this repo, so this isn't a build-config choice - it's how Railway's
build/deploy pipeline works). `push_snapshot()`'s original design
assumed the app's own directory (`REPO_DIR`) was a real git working
tree, which holds in local dev but never in this actual deployment.

Fixed by recognizing this dependency was never actually necessary:
since `SNAPSHOT_BRANCH` only ever holds a single force-pushed commit
with no shared history requirement (a deliberate design choice already
documented in this module - "never accumulated history"), `push_snapshot()`
now does a fresh `git init` in its own throwaway `tempfile.TemporaryDirectory()`
for every single push, writes `STATUS.md` there, commits, and force-pushes
from there - completely independent of whatever git state (or lack of
one) exists in the real running app's own directory. `REPO_DIR` and the
`.git`-directory existence check are both gone; `_run_git()` now takes an
explicit `cwd` per call instead of a module-level default. This is also
strictly safer than the original approach - it never writes into or runs
git commands against the real deployed app's own source directory.

Verified offline with a dedicated test that reproduces the exact real
Railway state (a real directory confirmed to have no `.git`, `os.chdir`'d
into for the test) and a real local bare "remote" repo standing in for
GitHub (the real `github.com` push URL redirected to it via a thin
`_run_git` wrapper, so no real network or token is touched) - confirms
`push_snapshot()` now succeeds from that exact no-`.git` state, confirms
the pushed branch's `STATUS.md` contains the real generated content via
plain `git show`, and confirms a second, different push correctly
force-overwrites the branch with fresh content (proving the throwaway-
repo approach works repeatedly, not just once). The old test's Case 5
(previously asserting `push_snapshot()` "correctly refuses when `.git`
is missing") tested the exact bug being fixed here and was rewritten to
assert the corrected behavior instead.

**Confirmed live**: after this fix, Railway logs showed the crash from
the first bug was gone (no more `unsupported operand type(s)` line) -
the `.git` gap is what remained, now also fixed. Still needs one more
real redeploy and a follow-up `git fetch origin status-snapshots` to
confirm the actual push succeeds end-to-end in production, the one part
that could never be verified from this sandboxed dev environment (no
live network access to GitHub or Railway).

---

## Reinforcement rule revised again: unconditional, not gated by any threshold

After the account owner had the 50%-threshold reinforcement rule explained
back to them (with a visual artifact showing the two spawn states), they
spotted the real gap in it directly: a branch sitting stuck partway - say
60-90% toward its own next tier - never dips below the 50% help line, so
it never gets reinforced either. It just sits there waiting on its own
trades to eventually move it, potentially indefinitely. Asked directly
via a clarifying question (three real options: always help the weakest
regardless of any threshold, raise the threshold higher, or add a
separate time-based "stuck too long" detector) - the account owner chose
the first: **always help whichever branch is currently weakest, no
threshold, full stop.**

`REINFORCEMENT_THRESHOLD_PCT` and `_tree_needs_reinforcement()` are both
removed entirely - there's no longer a percentage gate to check.
`_maybe_spawn_child()` now always calls
`_pick_weakest_branch_for_reinforcement()` first on every single real
spawn; a brand-new branch only ever gets created in the one case where
there's genuinely no OTHER branch left to reinforce (a fresh tree, or a
tree of exactly one branch) - the existing `None` fallback path, already
built for the very first spawn ever, now also covers this case. This is
the third real revision of this same rule this session: every-other-spawn
(alternating) -> reinforce-until-50%-then-normal -> now unconditional.

Verified offline against a real throwaway SQLite DB: a branch stuck at
70% (which the OLD 50%-threshold rule would have completely ignored) now
correctly receives the real reinforcement seed; a SECOND consecutive real
spawn also reinforces (this time targeting a different branch at 95%,
proving there's truly no threshold left at all, not just a higher one);
the picker still correctly excludes the spawning branch itself; and a
tree with no other branch to reinforce still correctly falls through to
a normal new-branch spawn. Full existing regression suite re-run
alongside it; the two failures seen were confirmed pre-existing/stale via
`git stash` comparison against the prior commit (both from the coin
universe having grown since those tests were written - unrelated to this
change).

---

## A third real status-snapshot bug: the container never had a `git` binary at all

After the `.git`-directory fix redeployed, a new and much simpler real
error showed up on the very next cycle:
`WARNING:status_snapshot:[STATUS-SNAPSHOT] snapshot push failed: [Errno 2]
No such file or directory: 'git'`. `push_snapshot()` shells out to the
real `git` CLI via `subprocess.run(["git"] + args, ...)` - but the
`Dockerfile`'s base image is `python:3.11-slim`, which does not ship
`git`, and the `apt-get install` list here never included it (only
`ffmpeg`, `fonts-dejavu-core`, `build-essential`, `python3-dev`). Every
earlier fix in this saga (the `.git`-directory workaround, the throwaway-repo
rewrite) was correct about the repository *state* but couldn't have worked
regardless, since the `git` binary itself was never present in the real
deployed container to run any of those commands.

Fixed by adding `git` to the Dockerfile's `apt-get install` line -
one line, no other changes needed. This is the real, final piece: the
throwaway-repo `push_snapshot()` logic was already correct, it just had
no `git` executable to actually invoke.

**Confirmed live**: the redeploy landed and the `status-snapshots` branch
finally exists on GitHub with real content (`git fetch origin
status-snapshots && git show origin/status-snapshots:STATUS.md`) - the
full chain (code, `.git`-independence, and the `git` binary) works
end-to-end in production. First real snapshot read this way: 23 crypto
branches, Total Profit -$349.27, Alpaca Total Profit +$29.36.

---

## Real feedback loop found via the first real status-snapshot read: reinforcement kept feeding a losing coin

The very first real `STATUS.md` snapshot read via git (see above) surfaced
something the dashboard's per-branch view never made visible at a glance:
**15 of the tree's 23 real branches are holding POL-USD**, and POL-USD's
real per-coin trade history is dismal - 39 trades, 12.8% win rate,
-$310.66 - by far the largest single chunk of the tree's real -$349.27
total loss. The account owner asked directly how this much capital ended
up concentrated in one coin and asked me to find out.

**Root cause of the concentration**: POL-USD became the real #1-ranked
coin by 30-day backtest ROI right at the moment the top-15 rotation
feature shipped, in the middle of the shared-coin-branches +
spawn-collision-retry saga already documented above - a real "spawn
storm" where a wave of concurrent branch spawns all independently landed
on the same top-ranked coin. Its real LIVE performance turned out
nothing like its backtested ranking.

**Root cause of why it kept getting WORSE, not just staying bad**: the
"always reinforce the weakest branch" feature (see the three reinforcement
revisions above) picks the weakest branch purely by lowest real
`allocated_usd / next_unlock_tier`, then buys MORE of THAT branch's
current coin - `_pick_weakest_branch_for_reinforcement()` and
`_deploy_seed_into_weakest_branch()` never checked coin exclusion at
all. Since a losing coin keeps its branches weak, and weak branches are
exactly what reinforcement targets, this was a real closed loop: POL-USD
loses -> its branches stay weakest -> reinforcement pours another real
$50 seed back into POL-USD -> repeat. Coin exclusion (manual or
automatic) provided zero protection against this specific path, since
reinforcement never consulted it.

Fixed in `_pick_weakest_branch_for_reinforcement()`: now also excludes
any candidate branch whose current coin is in
`get_effective_excluded_coins()` (the same manual+automatic exclusion set
every other coin-selection path already respects), falling through to
the next real weakest branch on a coin that's actually still eligible.
If literally every other branch is on an excluded coin, correctly
returns `None` (falls through to a normal new-branch spawn) rather than
reinforcing an excluded coin anyway. A branch already stuck on POL-USD
is NOT force-sold by this fix - it keeps trading normally under its own
target/stop/breakeven protection, it just stops being handed fresh
capital via reinforcement.

**Deliberately not done in this pass** (the account owner's own choice,
via a direct multi-select question): POL-USD was NOT added to
`MANUAL_EXCLUDED_COINS` this time, and existing POL-USD branches were
left completely untouched/still trading - only the reinforcement
loophole itself was closed. Whether POL-USD is *currently* sitting in
the automatic exclusion layer or the top-15 rotation is unconfirmed from
this sandbox (no live backtest-table access) - worth checking directly
on the `crypto_selection_backtest.html` page.

Verified offline against a real throwaway SQLite DB: the numerically
weakest branch is correctly skipped when its coin is excluded, in favor
of the next real weakest branch on an eligible coin; a tree where every
OTHER branch is on an excluded coin correctly returns `None` instead of
reinforcing the excluded coin anyway; and with no exclusions active at
all, behavior is completely unchanged from before this fix - the plain
numeric weakest still wins. Full existing reinforcement regression test
re-run clean alongside it.

---

## Alpaca exit-rule sensitivity comparison - checking whether the tight peak-giveback cap is why 4 months of real trading only made ~$29-50

The account owner raised a real, pointed concern: $980 deposited into the
Alpaca account roughly 4 months ago, and real profit sitting at only
~$29-50 since - "I'm almost not convinced any of this works." That's a
fair read of the real numbers (roughly 3-5% total, not annualized, for a
system carrying real market risk and constant engineering attention).

Checked why, directly in the code: `alpaca_mean_reversion.py`'s
`should_exit_position()` force-closes any position that has EVER shown a
real profit the moment it gives back `max_giveback_pct` (0.5% today) from
its best point - independent of whether the real 2% profit target has
been reached. The real profit target exists, but a position rarely gets
there, because a 0.5% wobble on the way up force-exits it first. This is
a deliberate design (it's what stops a winner from round-tripping into a
loss), but it also means the strategy is structurally close to incapable
of capturing a real 2%+ move - it's built to bank tiny wins repeatedly,
not let anything run.

Rather than just recommend loosening it on a guess, built a real,
additive comparison tool (same shadow-mode-only posture as every other
backtest tool in this codebase - never touches live trading, places no
order) so the account owner can decide with real evidence:

- `_replay_symbol()` in `alpaca_selection_backtest.py` gained two optional
  params, `giveback_pct`/`profit_target_pct`, defaulting to the module's
  own real live constants (`MAX_GIVEBACK_PCT`/`MIN_PROFIT_TARGET_PCT`) -
  the existing `run_full_backtest()` call site passes neither, so it's
  completely unaffected by this change.
- `run_exit_rule_sensitivity_comparison()` (new): fetches real Alpaca
  history ONCE per symbol (not once per scenario - the same real bars
  replayed multiple times, not re-fetched), then replays 3 real
  scenarios against it via the bot's own real `should_exit_position()` -
  `EXIT_RULE_SCENARIOS`: current (0.5% giveback / 2% target, today's real
  live rule), moderate (1.5% / 3%), loose (2.5% / 4%). Returns both a
  per-scenario TOTAL (summed across every real symbol - the direct
  answer to "would this have made more real money over the last 30
  days") and a per-symbol breakdown.
- New route `POST /api/trading-dashboard/alpaca-selection-backtest/exit-rule-comparison`
  (admin-key gated, same pattern as every other backtest route) and a
  new "▶ Run Exit-Rule Sensitivity Comparison" button + two tables
  (scenario totals, then per-symbol breakdown) on
  `alpaca_selection_backtest.html`, right under the existing backtest
  button.

Verified offline against a real, hand-computed price path (RSI
monkeypatched to a fixed, stateless sequence - 20 on the entry bar, 50 on
every bar after, so the RSI-entry/RSI-exit logic never introduces
unpredictable noise into a test whose whole point is isolating the
giveback/target math): confirms the unchanged call site produces
byte-for-byte identical output to before this change; confirms a real,
hand-verified climb-with-a-pullback price path exits the tight scenario
early on the pullback (+0.9%) while the moderate and loose scenarios ride
through it to their own further, real, larger targets (+3.2% and +4.5%
respectively) - proving this is a real, demonstrable effect in the actual
live exit function, not a coincidence; and confirms the comparison's
totals correctly SUM real P&L across every symbol and match the schema
the dashboard JS expects.

**Not yet run against real historical data** - same documented gap as
every other backtest tool in this codebase (no live network access from
this sandbox to Alpaca's market-data API). The account owner needs to
open `/alpaca-selection-backtest-view` after the next redeploy and click
the new comparison button themselves, then share the real results before
any decision to actually loosen the live `MAX_GIVEBACK_PCT`/
`MIN_PROFIT_TARGET_PCT` constants gets made - this tool only informs that
decision, it doesn't make it or change live behavior on its own.

---

## Real decision made from the exit-rule comparison: loosened prop_bot.py's live giveback/target to "moderate"

The account owner ran the comparison tool above live, against real 30-day
Alpaca history across all 11 symbols. Real results:

| Scenario | Trades | Win rate | Total P&L |
|---|---|---|---|
| current (0.5% giveback / 2% target) | 352 | 51.4% | $48.26 |
| moderate (1.5% giveback / 3% target) | 340 | 54.4% | $52.38 |
| loose (2.5% giveback / 4% target) | 340 | 54.4% | $53.13 |

Moderate beat current on BOTH real P&L (+$4.12) and win rate (+3 points) -
not just a looser rule taking more risk for more reward, a genuinely
better real outcome on this sample. Loose only added another $0.75 over
moderate for meaningfully more risk exposure - fast diminishing returns
past moderate. Per-symbol, the win wasn't uniform (USO actually did
*worse* looser: $27.58 -> $25.09-$25.84; DIA/IWM/RWM/DOG/SH stayed
negative in every scenario regardless of exit rule) - the aggregate gain
came from GLD/QQQ/SLV/PSQ improving more than USO/DOG/SH gave back.

Given this real evidence, the account owner asked me to combine it with
the real live context (4 months, $980 in, only ~$29-50 real profit) and
make the call. Changed `prop_bot.py`'s single real `mr_should_exit()` call
site (the live per-cycle exit check every open position goes through) from
`min_profit_target_pct=0.02` / `max_giveback_pct=0.005` to `0.03` / `0.015`
- the "moderate" scenario, not "loose" - moderate captured almost all of
the real benefit ($4.12 of the total $4.87 available) without loose's
extra risk for only 75 more cents. `stop_loss_pct` (0.3%, tighter than the
backtest tool's own 1.5% mirror constant) and every other parameter at
this call site are unchanged - only the two constants actually tested.

Scoped narrowly and deliberately: `alpaca_swing_bot.py` has its own,
separate exit logic (never calls `should_exit_position` at all) and
`mean_reversion_strategy.py` has its own older, unused implementation of
the same idea (confirmed via a real grep - not imported anywhere in the
live app) - neither was touched, since the real comparison evidence only
applies to the function and call site it actually replayed.

**Not yet confirmed against forward-looking real trading** - this is a
real, live parameter change now shipped, but its actual effect can only
be judged by watching real trades over the coming weeks, the same as any
other live strategy change in this file. The 30-day backtest sample is
real evidence, not a guarantee of the same magnitude going forward.

---

## Branch consolidation: merging every branch sharing a coin into one

Per the account owner's explicit request ("come up with a mechanism where
all of them can help each other"): with the shared-coin-branches feature
live, up to 15 real branches had piled onto POL-USD alone during the
top-15-rotation spawn storm - each one independently tracking its own qty
against ONE pooled real Coinbase balance for that coin, the exact
structural gap already behind both the phantom-position self-heal and the
DB-vs-Coinbase reconciliation SHORTFALLs found earlier this session.
Rather than build a new, separate "help each other" mechanic, the real fix
was to remove the fragmentation causing the risk in the first place -
turning many thin, individually floor-fragile branches on the same coin
into one bigger, sturdier one is real branches helping each other by
combining forces, and it structurally eliminates the cross-branch drift
risk for good on every coin it's applied to.

`consolidate_branches_by_coin()` (`crypto_family_tree_bot.py`): for every
`product_id` held by 2+ real branches (BTC-USD, and any group containing
the root branch, are always skipped - root never shares its coin by
design):
- `allocated_usd`: SUMMED across the group - no money created or
  destroyed, pure bookkeeping consolidation.
- `next_unlock_tier`: the MAX of the group's tiers, deliberately
  conservative so the merge itself (not new capital) doesn't artificially
  trigger a spawn/reinforcement none of the branches separately earned.
- `equity_floor`: recomputed fresh from the NEW combined balance via the
  same real tier formula every other floor self-heal in this file already
  uses - never just the max of the old floors, which could exceed the
  combined balance's own real bracket and immediately block trading.
- Position: every branch's real tracked qty (if holding) is SUMMED into
  one real quantity-weighted average entry price - the same blended-entry
  math the "Add cash" button already uses - then target/stop are
  recomputed fresh off that blended entry via a live price/ATR fetch.
- Survivor: the branch with the LARGEST `allocated_usd` keeps its
  identity (matches the existing "strongest sibling" throne philosophy
  already used elsewhere in this codebase); every other branch in the
  group is deleted after being folded in.

`dry_run=True` (the default, both for the function and the new
`POST /family-tree-status/consolidate-branches` endpoint) computes and
returns the full real plan WITHOUT touching the database or placing any
order, so it can be reviewed before it executes; `dry_run=False` executes
it for real. `family_tree_dashboard.html`'s reconciliation panel gained a
"🔗 Combine branches sharing a coin" button that always previews the real
plan first (coin, survivor, merged-away branches, combined balance,
blended position) with an explicit "✅ Confirm and combine for real"
step before anything is touched - never a single-click destructive
action. Per-coin trade history (`/family-tree-status/coin-history`) is
completely unaffected either way - it's already tracked by coin, not by
branch, so nothing there is lost when branches merge.

Verified offline against a real throwaway SQLite DB seeded with the exact
real shape from STATUS.md (3 branches on POL-USD - 2 holding real
positions, 1 flat - plus a lone, untouched XRP-USD branch and root alone
on BTC-USD): dry_run computes the correct real plan (survivor = highest
balance, combined balance summed, tier = max, floor recomputed, blended
entry quantity-weighted) without touching the database at all; execute
actually deletes the merged-away branches and updates the survivor's
bookkeeping AND blended position correctly; BTC-USD/root and the lone
XRP-USD branch are both left completely untouched throughout; and a
second call after consolidating correctly finds nothing left to merge.

**Not yet run against real production data** - the account owner needs to
open the family tree dashboard, click "🔗 Combine branches sharing a coin"
to preview the real plan for POL-USD (15 branches)/SOL-USD (5)/XRP-USD
(2), and confirm it before it actually executes against the real tree.

---

## Real, severe bug found and fixed: the entire Alpaca dashboard was broken (HTTP 422 on every load)

The account owner shared a real screenshot: `/alpaca-dashboard` stuck on
"Loading account summary..." with a bright red "Error: HTTP 422" at the
top of the page - every stat card empty. Root cause: an earlier edit this
session (adding the `_safe_float()` helper "right before
`get_alpaca_overview()`" to fix the Alpaca-position-fields-were-strings
crash) inserted the new helper function literally BETWEEN the
`@router.get("/alpaca-overview", ...)` decorator line and the
`async def get_alpaca_overview(...)` line it was meant to decorate -
so the decorator silently attached itself to `_safe_float` instead. Since
`_safe_float(v)` takes one plain, untyped, no-default parameter, FastAPI
registered IT as the real `/alpaca-overview` route handler and started
demanding a required query parameter `v` on every request - which the
dashboard's JS never sends, producing a 422 "field required" on every
single real load. The actual `get_alpaca_overview()` function was left
completely undecorated - not bound to any route at all.

Fixed by moving `_safe_float()`'s definition back above the decorator, so
`@router.get("/alpaca-overview", ...)` correctly applies to
`get_alpaca_overview` again. Verified via a real AST parse of the whole
router file confirming exactly one `GET /alpaca-overview` route now
exists, bound to the correct function, with no duplicate route
registrations anywhere else in the file (checked the entire file for the
same decorator-before-helper pattern recurring elsewhere - this was the
only occurrence).

This is a reminder to visually re-check the lines immediately around an
insertion when adding a helper function next to an existing decorated
endpoint, not just confirm the diff "looks right" from the added lines
alone.

---

## Real, deliberate decision: retired active Alpaca trading for a real buy-and-hold SPY position

After walking through real evidence together - the exit-rule comparison,
a real HYSA-vs-bot-vs-S&P comparison at several deposit sizes, and a plain
"why can't my bot beat the S&P" explanation - the account owner made a
real, explicit call: stop active trading on the Alpaca side entirely
("I don't want those eight active bot buckets anymore... does it serve
any purpose for real") and put the whole account into a single real
buy-and-hold SPY position instead. Confirmed the exact mechanics via two
direct questions before touching anything: close every real open position
immediately (not wait for natural exits), and deploy 100% of the real
freed cash into SPY.

**New persisted flag**: `is_alpaca_passive_mode()`/`set_alpaca_passive_mode()`
in `prop_bot.py` - a real DB-backed flag (reusing the same generic
`TradingBotState` bucket `locked_usd`/equity-floor state already lives in),
deliberately NOT a Railway env var like `STOP_TRADING` - a manually-pasted
env var is exactly the class of bug that silently disabled the crypto
coordinator earlier this session (stray quote characters); a flag this
code sets itself for a permanent, deliberate retirement doesn't have that
failure mode. Checked by BOTH real bots that place trades on this shared
account:
- `prop_bot.py`'s own `run()` outer loop - skips `run_prop_cycle()`
  entirely when passive (same "skip everything" semantics `STOP_TRADING`
  already has at that exact spot) - no new entries AND no exit-management
  on the one remaining real position, which is exactly what a genuine
  buy-and-hold needs (nothing should ever auto-sell it).
- `alpaca_swing_bot.py`'s own `run()` outer loop - this bot places real
  trades on the SAME account completely independently of `prop_bot.py`
  and had no `STOP_TRADING` awareness at all, so stopping only `prop_bot.py`
  would have left it free to keep trading real money out of the same
  shared cash pool. Now checks the same flag every cycle.
- The manual "Trade this" endpoint (`manual_open_prop_position`) also
  refuses while passive, matching its existing `STOP_TRADING` refusal.
- Manual close (`close_alpaca_position`) is deliberately UNCHANGED - the
  account owner can always sell the SPY position by hand if they ever
  want to, same "manual sell always stays available" principle used
  everywhere else in this codebase.

**New endpoint**: `POST /api/trading-dashboard/alpaca-overview/liquidate-and-buy-spy`
(admin-key gated) - a real, one-way action:
1. Reads every real open position straight from Alpaca's own `/v2/positions`
   (not from either bot's own internal tracking), so it closes everything
   regardless of which bot opened it - `alpaca_swing_bot.py` never
   persists to `BotPosition` at all, so this is the only reliable source
   of truth for "what's actually open right now."
2. Closes each one via the same real `DELETE /v2/positions/{symbol}?cancel_orders=true`
   the existing manual close-one endpoint already uses, records each
   real realized P&L as a `Payment` row (same bookkeeping pattern), and
   cleans up `prop_bot.py`'s own `open_prop_positions`/DB row for any
   symbol it was tracking.
3. Sets passive mode to `True` BEFORE placing the SPY buy - closes the
   real window where something could otherwise race in and open a new
   position between "everything closed" and "SPY bought."
4. Buys real SPY with 99.5% of the real freed cash via a real Alpaca
   *notional* (dollar-amount) market order - Alpaca computes the real
   fractional share count itself, so this never needs a separately-fetched
   price that could go stale between fetch and execution. The 0.5% buffer
   mirrors the same real-balance-clamp caution the crypto side's
   `place_market_buy()` already uses.
5. Refuses to attempt a SPY buy if less than $1 of real cash is free
   after closing (a real edge case, not expected here) - passive mode
   stays set either way, so nothing resumes trading on its own regardless.

The resulting SPY position needs no separate app-level tracking - since
passive mode means nothing ever reads `open_prop_positions` to manage it
again, the dashboard's existing real positions list (already sourced
directly from Alpaca) shows it accurately without any new bookkeeping.

`alpaca_dashboard.html` gained a real banner + button: before retiring,
shows "🔒📈 Retire active trading & buy real SPY" with a clear explanation
and a real double-confirmation (this is a genuinely irreversible action,
not a toggle); after, shows "🔒📈 Active trading retired" and points to
the Open Positions table where the real SPY holding now appears. The
existing 8-bucket "Buckets" grid was already `display:none` in this file
from earlier in the session, so no further hiding was needed there - the
account owner's mental model of "8 buckets" was the underlying dashboard
bookkeeping abstraction (`TradingBotState` bot_1..bot_8, purely
proportional shares of one real account, see the earlier "Alpaca-side
unlock" section above), not something separately visible to hide.

Verified offline: `is_alpaca_passive_mode()`/`set_alpaca_passive_mode()`
round-trip correctly against a real throwaway SQLite DB; the real
`liquidate_alpaca_and_buy_spy()` endpoint function, called directly with
a fake Alpaca session standing in for the real HTTP calls, correctly
closes every real position with hand-verified P&L math, records a real
`Payment` row per close, sets passive mode `True` before the buy, computes
the real SPY spend as exactly 99.5% of the real freed cash, and posts a
real notional buy order for it; and the too-little-real-cash edge case
correctly sets passive mode without attempting a real SPY buy. Full
existing Alpaca regression tests (position-string-fields fix, exit-rule
comparison) re-run clean alongside it.

**Not yet run against the real account** - the account owner needs to
open `/alpaca-dashboard` after the next redeploy and click "🔒📈 Retire
active trading & buy real SPY" themselves to actually execute this
against real money; this is a genuinely irreversible action once
confirmed.

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
