# Testing Quick Reference Card

## Run Tests

```bash
# Paper mode (0 risk)
./test_alpaca_paper.sh       # Alpaca stocks, dry-run
./test_crypto_paper.sh       # Crypto pre-flight only

# Micro live ($5-10 per trade)
./test_alpaca_micro_live.sh  # Alpaca, real $5 positions
./test_crypto_micro_live.sh  # Crypto, real $10 positions
```

## What to Expect

| Stage | Bot | Risk | Orders | Duration |
|-------|-----|------|--------|----------|
| Paper | Alpaca | $0 | Dry-run (logged only) | 1-2 days |
| Paper | Crypto | $0 | None (pre-flight only) | 15 seconds |
| Micro | Alpaca | $0.30 max | Real $5 positions | 1-2 days |
| Micro | Crypto | $0.50 max | Real $10 positions | 24-48 hrs |
| Full | Alpaca | Unlimited | Real $147 positions | Forever |
| Full | Crypto | Unlimited | Real $50 positions | Forever |

## Success Indicators

✅ **Pre-Flight Passes:**
```
✅ Coinbase access verified | Available USD: $XXX.XX
✅ Orders endpoint accessible
✅ Capital sufficient for trading
```

✅ **Entry Detected:**
```
RSI: 28 (oversold) | SMA: bullish trend
→ BUY 0.01 SYMBOL @ $XXX.XX [Order ID: abc123]
```

✅ **Stop-Loss Works:**
```
Price hits -0.5% (intraday) or -2% (swing)
→ SELL 0.01 SYMBOL @ $XX.XX (Stop-loss)
P&L: -$0.02
```

## Common Issues

| Problem | Solution |
|---------|----------|
| Pre-flight fails (HTTP 403) | Check API key/secret valid |
| No entry signals for hours | Wait, signal filters strict, RSI must be oversold |
| Order placed but not filled | Market condition or Coinbase delay, check dashboard |
| Stop-loss not triggering | Price hasn't hit stop level yet, be patient |

## Emergency Stop

```bash
# Pause (active positions stay, no new orders)
export STOP_TRADING=true

# Kill (hard stop, all processes)
pkill -f "python alpaca_swing_bot.py"
pkill -f "python main.py"
```

## Monitoring Logs

```bash
# Watch Alpaca bot in real-time
tail -f /tmp/empire-server.log | grep -E "BUY|SELL|STOP"

# Watch crypto bot in real-time
tail -f /tmp/empire-server.log | grep -E "CRYPTO|BUY|SELL"

# Check for errors
tail -50 /tmp/empire-server.log | grep -i "error\|failed"
```

## Decision Tree

```
Start here
    ↓
Does pre-flight pass? 
    ├─ NO  → Check API credentials & network
    └─ YES → Continue
            ↓
        Paper test signal detected?
            ├─ NO  → Wait 24-48 hrs, signal filters strict
            └─ YES → Continue
                    ↓
                Paper mode 1-2 days passed?
                    ├─ NO  → Keep running, gather more data
                    └─ YES → Ready for MICRO LIVE
                            ↓
                        Run micro live test 1-2 days
                            ├─ Total loss > $1.00? → Fix issues, retry
                            └─ Total loss < $1.00? → Ready for FULL DEPLOYMENT
                                                      ↓
                                                      Remove position caps
                                                      Deploy full bots
                                                      Monitor 2-3 days
                                                      ✅ LIVE ✅
```

## Critical Config Values

**Alpaca:**
```
Paper:        ALPACA_LIVE_TRADE=false
Micro:        ALPACA_MAX_POSITION_SIZE=5.0
Full:         (unset or higher value)
Stop-loss:    -0.5% (intraday) / -2% (swing)
Risk:         1.5% per trade ($14.70 on $980)
```

**Crypto:**
```
Paper:        CRYPTO_BOT_DISABLED=true
Micro:        CRYPTO_MAX_ALLOCATION=10.0
Full:         (unset, compounds)
Stop-loss:    -1% hard stop
Risk:         1% per trade
Pairs:        BTC/USD, ETH/USD + 26 others
```

## Timing

**When to run tests:**

| Bot | Best Time | Why |
|-----|-----------|-----|
| Alpaca paper | Any time | Market hours not required for paper |
| Alpaca micro | Before 9:30 AM ET | Start before market open |
| Crypto paper | Any time | 24/7 market doesn't matter for pre-flight |
| Crypto micro | Any time | 24/7 crypto trading |

**How long to run:**

| Test | Min Duration | Why |
|------|--------------|-----|
| Alpaca paper | 1 day | Need to see full entry→exit cycle |
| Crypto paper | 15 seconds | Just runs pre-flight test |
| Alpaca micro | 1 day | Need to see real order flow at least once |
| Crypto micro | 24 hours | 24/7 market, signals might take time |

## Money at Risk by Stage

```
Paper mode:     $0.00
Micro Alpaca:   ~$0.30 max ($5 × 1 position, capped at -0.5%)
Micro Crypto:   ~$0.50 max ($10 × 5 positions, capped at -1%)
Full Alpaca:    Unrestricted (~$147/position)
Full Crypto:    Unrestricted (~$50/position)
```

## Pre-Deployment Checklist

- [ ] Paper tests completed (Alpaca 1-2 days, Crypto 15 sec)
- [ ] Pre-flight test passes 100% consistently
- [ ] At least 2-3 entry signals detected in paper mode
- [ ] Stop-losses working (if tested)
- [ ] Micro live test completed (loss < $1.00 total)
- [ ] No "HTTP 403" or "Connection refused" errors
- [ ] Both API credentials confirmed working
- [ ] Position caps removed from env vars
- [ ] Ready to monitor for first 2-3 days of full deployment

---

**Ready to test?** Start with:
```bash
./test_alpaca_paper.sh
./test_crypto_paper.sh
```

Good luck! 🚀
