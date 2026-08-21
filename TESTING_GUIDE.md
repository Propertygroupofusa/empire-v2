# Trading Bot Testing Guide — Alpaca + Crypto

Complete testing workflow from paper → micro live → full deployment.

## Quick Start

```bash
# Stage 1: Paper trading (0 risk)
./test_alpaca_paper.sh      # Alpaca stock bot, dry-run
./test_crypto_paper.sh      # Crypto bot, pre-flight only

# Stage 2: Micro live ($5-10 per trade)
./test_alpaca_micro_live.sh  # Alpaca with $5 position cap
./test_crypto_micro_live.sh  # Crypto with $10 position cap

# Stage 3: Full deployment (unrestricted position sizing)
# → See "Full Deployment" section below
```

---

## Testing Progression

### 🟢 Stage 1: Paper Trading (24 hours, $0 risk)

**Alpaca Bot:**
```bash
./test_alpaca_paper.sh
```

✅ What it does:
- Uses paper trading account (virtual $25k balance)
- No real orders placed
- Tests entry/exit signal logic
- Simulates stop-loss triggers
- Logs all "would-be" trades

✅ What to watch for:
- Pre-flight test passes (5-10 seconds)
- Bot detects entry signals (within 5-30 minutes)
- Correct position sizes logged
- Stop-loss triggers on losing positions

✅ How long:
- 1-2 trading days to see full entry→exit cycle
- Can cancel anytime with Ctrl+C

❌ If pre-flight FAILS:
```
❌ PRE-FLIGHT TEST FAILED — ...
❌ FAILED to fetch account balance: HTTP 403
```
→ Check ALPACA_API_KEY and ALPACA_SECRET_KEY are valid

---

**Crypto Bot:**
```bash
./test_crypto_paper.sh
```

✅ What it does:
- Runs pre-flight test only (no trading)
- Verifies Coinbase API access
- Checks USD balance
- Tests orders endpoint
- Bot exits cleanly

✅ Expected output:
```
✅ Coinbase access verified | Available USD: $123.45
✅ Orders endpoint accessible
✅ Capital sufficient for trading
```

✅ How long:
- 10-15 seconds (one cycle)

❌ If pre-flight FAILS:
```
❌ FAILED to fetch Coinbase balance: ...
❌ FAILED: HTTP 403
```
→ Check COINBASE_API_KEY_NAME and COINBASE_API_PRIVATE_KEY

---

### 🟡 Stage 2: Micro Live Testing (24-48 hours, ~$5-10 total risk)

**Prerequisites:**
- ✅ Alpaca paper test passed (or skipped if confident)
- ✅ Account balance verified ($980+ for Alpaca, $10+ for Crypto)
- ✅ Live API credentials configured

---

**Alpaca Micro Live:**
```bash
./test_alpaca_micro_live.sh
```

⚠️ **This places REAL orders on your $980 account**

Configuration:
- Position size: $5.00 per trade (capped)
- Stop-loss: -0.5% (intraday) / -2% (swing)
- Max risk per position: $0.025-$0.10
- Max total loss if all positions stop: ~$0.30

What to watch:
```
BUY 0.01 SYMBOL @ $XX.XX | Order ID: abc123
  ✓ Order confirmed by Alpaca
  ✓ Position logged with entry price
  ✓ Stop-loss order placed

[5 minutes later]
SELL 0.01 SYMBOL @ $XX.XX (Stop-loss hit)
  ✓ Exit executed at stop level
  ✓ P&L: -$0.02
```

Duration: 1-2 trading days (market hours only)

---

**Crypto Micro Live:**
```bash
./test_crypto_micro_live.sh
```

⚠️ **This places REAL orders on your Coinbase account**

Configuration:
- Position size: $10.00 per trade (capped)
- Stop-loss: -1% hard stop
- Max risk per position: $0.10
- Max total loss if all positions stop: ~$0.50

What to watch:
```
BUY 0.0001 BTC @ $XX,XXX | Order ID: order-123
  ✓ Order confirmed by Coinbase
  ✓ Position logged with entry price
  ✓ Stop-loss order placed

[30 minutes - 48 hours later]
SELL 0.0001 BTC @ $XX,XXX (Stop-loss or profit target)
  ✓ Exit executed
  ✓ P&L: -$0.05 or +$0.30
```

Duration: 24-48 hours (crypto runs 24/7)

---

### 🔴 Stage 3: Full Deployment (Unlimited, Full Position Sizing)

After both micro tests pass:

**Alpaca Full:**
```bash
export ALPACA_LIVE_TRADE=true
export ALPACA_BASE_URL=https://api.alpaca.markets
# Remove ALPACA_MAX_POSITION_SIZE (let it use full $147/position)
python alpaca_swing_bot.py
```

Position sizing reverts to:
- ~$147 per swing trade (15% of $980)
- ~$49 per intraday trade (5% of $980)
- Risk per trade: $14.70 (1.5% of $980)

**Crypto Full:**
```bash
export CRYPTO_BOT_DISABLED=false
# Remove CRYPTO_MAX_ALLOCATION (let it compound)
python main.py
```

Position sizing reverts to:
- ~$50 per position (balance-dependent)
- Risk per trade: $0.50 (1% stop-loss)

---

## Safety Features That Protect You

### Pre-Flight Tests
```
Alpaca: Verifies API access, account balance, positions endpoint
Crypto: Verifies API access, Coinbase balance, orders endpoint

If ANY test fails:
  → Bot exits immediately
  → No trades placed
  → No capital at risk
```

### Stop-Loss Automation
```
Alpaca Intraday:  -0.5% hard stop → Exit immediately
Alpaca Swing:     -2.0% hard stop → Exit immediately
Crypto:           -1.0% hard stop → Exit immediately

Example:
  Entry: BUY $100 worth
  Price drops to $99.50 (0.5% loss)
  Stop triggers: SELL all shares immediately
  Loss capped at $0.50
```

### Position Sizing Limits
```
Alpaca: Max 1 concurrent position (micro account safe mode)
Crypto: Max 5 concurrent positions (5 pairs × $10 = $50)

Each position sized at ~1.5% risk (Alpaca) or 1% (Crypto)
```

### Order Validation
```
Alpaca: Confirms order ID returned before logging entry
Crypto: Confirms order accepted by Coinbase before logging

If order rejected:
  → Position NOT logged
  → No stop-loss set
  → Cash remains available
```

---

## Troubleshooting

### Pre-Flight Test Fails with "HTTP 403"

**Alpaca:**
```
❌ FAILED to fetch account balance: HTTP 403
```

Solutions:
1. Check API key is valid for LIVE account (not paper)
2. Verify key has full read/trade permissions
3. Check ALPACA_BASE_URL is correct
4. Try regenerating API key in Alpaca dashboard

**Crypto:**
```
❌ FAILED to test orders endpoint: HTTP 403
```

Solutions:
1. Check COINBASE_API_KEY_NAME is correct
2. Verify COINBASE_API_PRIVATE_KEY matches the key
3. Ensure key type is "CDP" (not legacy)
4. Try regenerating key in Coinbase dashboard

---

### Orders Not Placing (Stays in "waiting for signal")

**Alpaca:**
- Market might not be open (paper mode requires market hours)
- Signal filters too strict (RSI too high, volume too low)
- Insufficient buying power
- Check logs for "Skipping entry — <reason>"

**Crypto:**
- No oversold RSI signal yet (RSI > 30, need < 30)
- Check logs for "No entries found"
- Bot runs 24/7 but signals are rare
- Wait longer for entry opportunity

---

### Stop-Loss Not Triggering

**How to test stop-loss:**

Alpaca (paper mode):
```
1. Let bot place entry
2. Manually buy same symbol on Alpaca to move price
   (or wait for real market move against position)
3. Watch for stop-loss exit in logs
```

Crypto (live micro):
```
1. Let bot place real entry ($10)
2. Wait for 1% price move down
3. Watch logs for stop-loss exit order
4. Verify fill on Coinbase
```

---

## Testing Checklist

```
STAGE 1 - ALPACA PAPER:
□ Start bot with ./test_alpaca_paper.sh
□ See "PRE-FLIGHT... PASSED" message
□ Wait for first entry signal (5-30 min)
□ Confirm position logged correctly
□ Wait for exit (manual or stop-loss)
□ Check P&L calculation
□ Run for 1-2 trading days
□ Stop with Ctrl+C

STAGE 1 - CRYPTO PAPER:
□ Start bot with ./test_crypto_paper.sh
□ See all 3 pre-flight tests pass
□ Bot exits cleanly (no crash)
□ Verify USD balance shown
□ Takes ~15 seconds total

STAGE 2 - ALPACA MICRO LIVE:
□ Run ./test_alpaca_micro_live.sh
□ Type 'yes' to confirm
□ Wait 10-second safety countdown
□ See "PRE-FLIGHT... PASSED"
□ Monitor for first real order (Order ID: ...)
□ Verify order on Alpaca dashboard
□ Watch for stop-loss or profit exit
□ Run for 1-2 trading days
□ Verify total loss < $0.30
□ Stop with Ctrl+C

STAGE 2 - CRYPTO MICRO LIVE:
□ Run ./test_crypto_micro_live.sh
□ Type 'yes' to confirm
□ Wait 10-second safety countdown
□ See "PRE-FLIGHT... PASSED"
□ Monitor for first real order (Order ID: ...)
□ Verify order on Coinbase app
□ Watch for stop-loss or profit exit
□ Run for 24-48 hours
□ Verify total loss < $0.50
□ Stop with Ctrl+C

FINAL - FULL DEPLOYMENT:
□ All micro tests completed successfully
□ No more than $1.00 total losses across all tests
□ Remove position size caps from env vars
□ Restart both bots for full sizing
□ Monitor for 1-2 days before leaving unattended
```

---

## Emergency Stop

**If something looks wrong at any stage:**

```bash
# Option 1: Graceful pause (bot won't place new orders)
export STOP_TRADING=true

# Option 2: For crypto bot only
export CRYPTO_BOT_DISABLED=true

# Option 3: Hard kill
pkill -f "python alpaca_swing_bot.py"
pkill -f "python main.py"
```

Active positions stay open (to respect stop-losses). Bot won't open new ones.

---

## Testing Timeline Example

```
Friday, Aug 21:
  09:00 - Start alpaca_swing_bot paper mode
  09:05 - Pre-flight passes ✓
  09:15 - First entry signal detected
  09:20 - Position fills, logs entry
  10:00 - Position shows +$0.50 P&L (won)
  10:05 - Profit target hit, exit fills
  → Continue running... waiting for next signal

  14:00 - Start crypto_coinbase_bot pre-flight
  14:00 - All 3 tests pass ✓
  14:01 - Bot exits (pre-flight only mode)

Saturday, Aug 22-23:
  Run alpaca paper test another full day
  Monitor RSI signals, position entries, exits
  Verify stop-losses work if position goes negative

Monday, Aug 25 (Market Opens):
  09:00 - Start alpaca_swing_bot MICRO LIVE
  09:10 - Pre-flight passes ✓
  09:30 - Market opens, bot scans
  09:45 - Entry signal, places REAL $5 order
  09:46 - Order fills, logged with Order ID
  10:00 - Position shows +$0.10 P&L
  10:05 - Exit signal, SELL fills
  → Continue monitoring for 1-2 full trading days
  
  Tuesday, Aug 26:
  24-hour mark for crypto micro test running
  Monitor for Coinbase order confirmations
  Verify stop-losses triggering
  Check real fills vs logged prices
  
  Wednesday, Aug 27 (If all tests passing):
  Remove position size caps
  Deploy full bots
  Run with full position sizing
  Monitor for 2-3 days before full automation
```

---

## Questions?

- **Pre-flight failing?** → Check API credentials and internet access
- **No entry signals?** → Market/signal conditions, wait 24-48 hours
- **Stop-loss not working?** → Position not deep enough in loss yet, be patient
- **Confused about timing?** → Check timezone (Alpaca uses ET, Crypto is UTC)

Good luck! 🚀
