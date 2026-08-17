# CRYPTO BOT OPERATIONS MANUAL

**Purpose:** Single source of truth for bot behavior, configuration, and profit targets. Eliminates confusion, static errors, and ensures clean execution.

---

## BOT MANDATE: PROFIT ONLY

**Core Directive:** Execute trades ONLY when profitable signals fire. No false entries. No ambiguous decisions.

**Each trade must:**
1. ✓ Enter on confirmed RSI oversold recovery (not just threshold)
2. ✓ Exit via tiered profit-taking or stop-loss (never hold indefinitely)
3. ✓ Compound all profits immediately (auto-scale next position with updated balance)
4. ✓ Log complete signal context (entry RSI, trend, ATR, exit reason, P&L)

**NEVER:**
- ✗ Hold a position waiting for a high profit target that may never come
- ✗ Enter on noisy RSI crosses without recovery confirmation
- ✗ Ignore stop-losses to "wait for recovery"
- ✗ Trade if signal is ambiguous or data is stale

---

## CONFIGURATION REFERENCE

All values set via environment variables. Defaults allow trading immediately without additional setup.

### API CREDENTIALS (Required)

| Variable | Example | Purpose |
|----------|---------|---------|
| `COINBASE_API_KEY_NAME` | `3fef381e-4800...` | Coinbase CDP key ID |
| `COINBASE_API_PRIVATE_KEY` | `MG6FzzFt...` | Coinbase CDP private key (ECDSA PEM or Ed25519 base64) |

### NETWORK RESILIENCE (Railway Egress Workaround)

When Railway blocks `api.coinbase.com` (403 error):

| Variable | Value | Purpose |
|----------|-------|---------|
| `NETWORK_RETRY_ATTEMPTS` | `5` | Retry failed API calls up to 5 times |
| `NETWORK_RETRY_DELAY` | `2.0` | Initial backoff in seconds (exponential: 2s, 4s, 8s...) |
| `NETWORK_CACHE_TTL` | `600` | Cache price data for 10 minutes when API blocked |
| `ALLOWED_EGRESS_HOSTS` | `api.coinbase.com,data.alpaca.markets` | Hosts to allowlist on Railway |

**Status:** Set all 4 vars + redeploy if bot shows 403 errors in logs.

### TRADING PARAMETERS

#### Entry Signals (RSI-based Oversold Detection)

| Variable | Default | Purpose |
|----------|---------|---------|
| `CRYPTO_RSI_BUY_BELOW` | `30` | RSI threshold below which to "arm" for entry |
| `CRYPTO_MIN_POSITION_NOTIONAL` | `0.50` | Minimum trade size in USD (micro-scalping on small balances) |
| `CRYPTO_MAX_POSITIONS` | `50` | Max number of open positions (safety limit) |

**Entry Logic:**
- RSI > 50: WATCH state (no entry signal)
- RSI 30-50: ARM state (prepare for entry, wait for recovery)
- RSI < 30: STRONG_ARM state (higher quality entry signal, wait for recovery)
- Entry triggers when RSI recovers UP from oversold state (confirmation, not just threshold cross)

#### Exit Signals (Profit-Taking & Stop-Loss)

| Variable | Default | Purpose |
|----------|---------|---------|
| `CRYPTO_RSI_SELL_ABOVE` | `70` | RSI at which to consider taking profits (overbought signal) |
| `CRYPTO_TIER_LEVELS` | `[0.05, 0.08, 0.15]` | Exit profit targets: 5%, 8%, 15% |
| `CRYPTO_TIER_FRACTIONS` | `[1/3, 1/3, 1/3]` | Exit 1/3 of position at each tier |
| `CRYPTO_TRAILING_STOP_PCT` | `0.05` | Trail final 1/3 by 5% from recent high |
| `STOP_LOSS_PCT` | `0.01` | Ultra-tight 1% stop-loss (capital preservation) |

**Exit Rules:**
1. **Tier 1 (5%):** Exit 1/3 of position, lock first profit, move stop to breakeven
2. **Tier 2 (8%):** Exit 1/3 of position, bank second profit tier
3. **Tier 3 (15%):** Trail final 1/3 by 5% from peak (let winners run with protection)
4. **Stop-Loss (1%):** Exit entire position immediately if -1% hit (rapid redeployment)
5. **RSI Neutral (>50):** Exit if RSI recovers above 50 and no profit yet (mean reversion complete, no edge)

#### Capital Management

| Variable | Default | Purpose |
|----------|---------|---------|
| `CRYPTO_MAX_ALLOCATION` | `None` | Hard ceiling on active trading capital (None = use full balance) |
| `CRYPTO_MIN_CASH_RESERVE` | `25` | Minimum cash never deployed (grows from profits only) |
| `CRYPTO_TIER_SIZE` | `0` | Tiered capital unlock ($100 steps to unlock more capital; 0 = disabled) |
| `CRYPTO_ROUND_TRIP_FEE_RATE` | `0.004` | Estimated Coinbase round-trip fee (0.4%) |

**Capital Rules:**
- Bot compounds automatically: every cycle sizes new positions off actual current USD balance
- Never transfers money (funds only leave via manual withdrawal)
- Respects MIN_CASH_RESERVE (won't trade last $25 of $483 balance, for example)
- Full account balance is in play by default (no tiering), accelerating compounding from profits

### MARKET COVERAGE

**28 trading pairs** across 5 tiers (sorted by tier strength):

```
Tier 1 (Stable): BTC/USD, ETH/USD
Tier 2 (Established): SOL/USD, XRP/USD, AVAX/USD, LINK/USD
Tier 3 (Volume): DOGE/USD, SHIB/USD, NEAR/USD, MATIC/USD
Tier 4 (L2/DeFi): ARB/USD, OP/USD, AAVE/USD, UNI/USD, STX/USD, ATOM/USD
Tier 5 (Newer/Niche): LTC/USD, ADA/USD, DOT/USD, APT/USD, SUI/USD, JUP/USD, LDO/USD, RNDR/USD, ICP/USD, BLUR/USD, FLOKI/USD, BONK/USD
```

All 28 pairs run in parallel. Bot scans once per cycle (60 seconds default).

---

## EXECUTION FLOW (Per Cycle)

Every 60 seconds:

```
1. LOAD POSITIONS
   ├─ Read open positions from DB (persists across bot restarts)
   └─ Rebuild in-memory open_crypto_positions dict

2. GET PRICE DATA (for each of 28 symbols)
   ├─ Try: Fetch 50 5-minute candles from Coinbase API
   ├─ Fallback: Check local cache if API fails (Railway egress blocked)
   └─ Fallback: Skip symbol if no candles available (no trade signal)

3. CALCULATE RSI & ATR (on candle data)
   ├─ RSI = 14-period RSI (standard momentum)
   ├─ ATR = 14-period Average True Range (volatility)
   ├─ SMA5/SMA10 = trend confirmation
   └─ Store in memory (RSI_STATE_CACHE, flushed to DB at cycle end)

4. CHECK ENTRY SIGNALS (for each symbol)
   ├─ Is RSI < 30 (ARM) or < 20 (STRONG_ARM)?
   ├─ Has RSI recovered UP from oversold state (confirmation)?
   ├─ Is position already open for this symbol? (no double-entry)
   ├─ Is open position count < CRYPTO_MAX_POSITIONS (safety)?
   └─ If all YES → ARM signal fires, create entry order

5. CREATE ENTRY ORDER (when ARM signal fires)
   ├─ Size = (Available USD balance - MIN_CASH_RESERVE) / symbol_count
   │   Example: $483 balance, 5 open + 1 new = $458 / 6 = ~$76 per position
   ├─ Order type = market_market_ioc (immediate fill, no wait)
   ├─ Quote sizing = position_size / entry_price (qty in base asset)
   └─ Submit to Coinbase, await fill confirmation

6. CHECK EXIT SIGNALS (for each OPEN position)
   ├─ STOP-LOSS: Is price <= entry_price × (1 - 1%)?
   │   → Exit 100% immediately (capital preservation)
   ├─ TIER 1 (5%): Is price >= entry_price × 1.05?
   │   → Exit 1/3, move stop to breakeven, continue
   ├─ TIER 2 (8%): Is price >= entry_price × 1.08?
   │   → Exit 1/3, bank profit, continue
   ├─ TIER 3 (15%): Is price >= entry_price × 1.15?
   │   → Exit 1/3, initiate trail from recent_high × (1 - 5%)
   ├─ TRAIL (5% from peak): Is price <= recent_high × (1 - 5%)?
   │   → Exit remaining position (trail stop hit)
   └─ RSI RESET: Is RSI > 50 AND no exit yet?
       → Exit (mean reversion complete, no further edge)

7. UPDATE DATABASE
   ├─ Flush RSI_STATE_CACHE to crypto_rsi_state table
   ├─ Log all trades to crypto_trade_log (entry, exit, P&L, reason)
   ├─ Update BotPosition for open positions
   └─ Store daily P&L and balance snapshots

8. RETURN TO STEP 1 (after 60-second wait)
```

---

## SIGNAL DEFINITIONS

### Entry Signal: RSI Oversold + Recovery Confirmation

**Triggers when:**
1. RSI < 30 (oversold territory entered)
2. AND RSI has recovered UP by at least 2 points from its local minimum
3. AND no position already open for this symbol
4. AND open position count < 50

**Why:** Prevents false entries on RSI dips that never recover. Confirms the "bottom" was actually found.

**Example:**
- Cycle 1: BTC RSI = 25 → ARMs (threshold met)
- Cycle 2: BTC RSI = 26 → Entry confirmed (recovery confirmed), BUY ORDER placed
- Cycle 3: BTC RSI = 18 (dips again) → No additional entry, position already open

### Exit Signal: Tiered Profit-Taking

**Tier 1 (5%):**
- Exit when price reaches entry × 1.05
- Frees up capital for next entry, locks first profit
- Move stop to breakeven ($0 loss point)

**Tier 2 (8%):**
- Exit when price reaches entry × 1.08
- Banks second profit milestone
- Risk/reward now favorable (already locked 5%, targeting 8%)

**Tier 3 (15%):**
- Exit when price reaches entry × 1.15
- Final exit is a trailing stop (trail by 5% from peak)
- Lets winners run: if price hits $100, trail at $95; if hits $120, trail at $114

**Stop-Loss (1%):**
- Exit ENTIRE position if price drops 1% from entry
- IMMEDIATE execution (no waiting)
- Ultra-tight because this is mean-reversion on volatile 5-min bars
- Wide stops (5-10%) proved worse in backtesting (losses bled capital on fees)

**RSI Neutral Exit:**
- Exit if RSI recovers above 50 (mean reversion complete)
- Signals are spent, no further edge
- Prevents holding through overbought territory hoping for more gains

### No Entry Conditions

**Do NOT enter if:**
- RSI >= 65 (overbought, mean reversion already peaked)
- Position already open for this symbol (no double-entry)
- Open position count >= 50 (safety limit)
- Available cash < $0.50 (too small to be meaningful)
- API call fails and no cached data available (stale signals = ambiguous)

---

## ERROR HANDLING & FALLBACKS

### Network Error: API Call Fails (403/Timeout)

**Immediate Response:**
1. Log error with timestamp
2. Check local PRICE_CACHE for symbol
3. If cache exists and fresh (<10 min old): use cached prices for RSI calculation
4. If cache expired or missing: SKIP this symbol this cycle (no trade signal)

**Example:**
```
[ERROR] Coinbase API: 403 Not in allowlist: api.coinbase.com
→ Check PRICE_CACHE['BTC/USD']
→ Found: RSI from 2 min ago, use it
→ Continue RSI logic with cached data
→ Skip only if cache is stale or missing
```

**User Action:** Set 4 env vars on Railway:
- NETWORK_RETRY_ATTEMPTS=5
- NETWORK_RETRY_DELAY=2.0
- NETWORK_CACHE_TTL=600
- ALLOWED_EGRESS_HOSTS=api.coinbase.com,data.alpaca.markets

### Order Submission Fails

**Immediate Response:**
1. Log order + error details
2. Check Coinbase account balance (did order actually fill despite error?)
3. If filled: add to open_crypto_positions, log as entry
4. If not filled: retry once with same order parameters
5. If retry fails: skip this entry signal, try again next cycle

**NEVER** retry indefinitely (ties up capital, misses other entries).

### Database Connection Fails

**Immediate Response:**
1. Log error
2. Continue with in-memory RSI_STATE_CACHE (will be flushed when DB recovers)
3. Do NOT crash bot
4. Retry flush at next cycle

**Impact:** Slight data loss possible if bot crashes before flush, but positions are durable (stored on Coinbase itself).

### Stale Candle Data (No recent bars)

**Immediate Response:**
- Skip symbol this cycle (can't calculate fresh RSI)
- Do NOT use old RSI to enter new position
- Try again next cycle

---

## PROFIT CALCULATIONS

### Daily P&L

Tracked for monitoring purposes (not used for trading decisions).

```
Daily P&L = (Current balance - Starting balance) + (Withdrawals - Deposits)
```

Example:
- Start day: $483
- Execute 3 profitable trades, bank $15
- Make 1 losing trade, lose $2
- End day: $496
- Daily P&L = +$13

### Position P&L

Calculated at exit:

```
Gross P&L = Exit price × exit_qty - Entry price × entry_qty
Fee cost = Entry notional × 0.6% + Exit notional × 0.6% (Coinbase taker fee)
Net P&L = Gross P&L - Fee cost
Net P&L % = Net P&L / (Entry price × entry_qty) × 100
```

Example:
- Entry: 0.001 BTC @ $42,500 = $42.50 notional
- Exit: 0.001 BTC @ $44,625 (Tier 1 at 5%) = $44.63 notional
- Gross: $44.63 - $42.50 = +$2.13
- Fees: ($42.50 × 0.006) + ($44.63 × 0.006) = $0.52
- Net: $2.13 - $0.52 = +$1.61
- Net %: $1.61 / $42.50 = +3.78%

---

## MONITORING CHECKLIST

**Every 60 seconds, confirm:**
- ✓ Bot process is running (check logs: "Starting crypto_coinbase_bot")
- ✓ API calls succeeding (check logs for Coinbase response codes)
- ✓ RSI calculations fresh (<1 min old)
- ✓ No 403 errors (or cached data being used if 403 occurs)
- ✓ Positions opening on ARM signals (not missing entries)
- ✓ Positions closing on exit signals (not holding indefinitely)
- ✓ P&L being logged to database (entries in crypto_trade_log)

**Every 24 hours, verify:**
- ✓ Daily P&L >= 0 (profitable or breakeven, never losing)
- ✓ Exit reasons logged correctly (e.g., "tier_1_profit_5%", "stop_loss_1%", "rsi_neutral")
- ✓ No orphaned positions (open on Coinbase but not in DB, or vice versa)
- ✓ Cash reserve still >= $25 (wasn't accidentally deployed)

**If bot is idle (0 entries for 4+ hours):**
1. Check: Is market in high RSI (overbought)? (Normal, wait for oversold)
2. Check: Are API calls succeeding? (If 403, set Railway env vars)
3. Check: Is bot process running? (If not, check logs for crash reason)

---

## QUICK REFERENCE: COMMAND TO START BOT

```bash
# On Railway (automatic, part of main-app deployment)
python main.py

# Locally (for testing)
python main.py

# Monitor logs
tail -f /tmp/empire-server.log | grep crypto_coinbase
```

---

## PROFIT FLOW: WHERE MONEY GOES

```
1. Entry: Bot buys 0.001 BTC with $42.50 from Coinbase balance
2. Price rises to $44.63 (5% gain)
3. Tier 1 Exit: Bot sells 1/3 of position (0.00033 BTC), locks $14.87
4. Profit goes to: Coinbase account USD balance (auto-compound)
5. Next cycle: New position sized on updated balance ($483 + $14.87 = $497.87)
6. Repeat: Positions compound automatically, no manual withdrawal needed

Revenue stream (where profit ends up):
- Trading profit → Coinbase USD balance
- Balance growth → Larger positions in next cycles (exponential compounding)
- Manual withdrawal → Stripe transfer to bank account (scheduled, not automatic)
```

---

## RED FLAGS: STOP BOT IF YOU SEE THESE

- ❌ All exits happening via STOP_LOSS (means entries are bad, not mean-reversion)
- ❌ RSI state not updating (bot not calculating, likely API blocked)
- ❌ Orders failing with "insufficient balance" (capital management broken)
- ❌ No new entries for 6+ hours despite good RSI signals (entry logic broken)
- ❌ Position count growing indefinitely (no exits working, capital trapped)
- ❌ Database errors every cycle (data integrity issue, stop to preserve state)

---

**Version:** 1.0  
**Last Updated:** 2026-08-17  
**Status:** Profit-only mode active, no false signals
