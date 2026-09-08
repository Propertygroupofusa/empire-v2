# Railway Bot Recovery - Direct Commands

## Quick Deploy (Copy & Run)

```bash
# Restart the crypto-trading service
railway down crypto-trading && sleep 3 && railway up crypto-trading

# Tail logs to verify recovery
railway logs crypto-trading --tail 200 --follow
```

---

## Recovery Verification Checklist

Monitor these three conditions sequentially in the logs:

### ✓ CONDITION 1: Clean Loop & Semaphore Bind (T+0-10s)

**Expected log output:**
```
2026-09-08 06:25:04,949 [__main__] INFO: ✓ Event loop initialized (before module imports)
2026-09-08 06:25:05,443 [__main__] INFO: ✓ Crypto grid bot module loaded
```

**Failure indicators (DO NOT IGNORE):**
```
RuntimeError: Task is attached to a different loop
RuntimeError: asyncio.locks.Semaphore object at 0x... is bound to a different event loop
```

**Command to check:**
```bash
railway logs crypto-trading --tail 50 | grep -E "Event loop|Semaphore|bound to|RuntimeError"
```

---

### ✓ CONDITION 2: Order Book Sync (T+10-30s)

**Expected behavior:**
- Old buy orders from $2,563.43 allocation should be canceled or refreshed
- Grid positions should NOT be orphaned at old levels
- New orders should issue at 33% of previous size (matching new $850 allocation)

**Log indicators to find:**
```
[grid_bot] Syncing orders with new allocation
[grid_bot] Canceled pending orders from previous cycle
[crypto_grid_1] DOGE-USD: Resized grid from 10 levels to 10 levels at ~$135.81
[crypto_grid_3] ETH-USD: Resized grid from 10 levels to 10 levels at ~$277.71
```

**Command to check:**
```bash
railway logs crypto-trading --tail 100 | grep -E "order.*cancel|reconcil|resiz|allocation"
```

---

### ✓ CONDITION 3: Buying Power & State Transition (T+30-60s)

**Expected log output:**
```
[bot_monitor] Account status: Buying power: $167.48 (up from $0.23)
[bot_monitor] Equity: $1,017.48 | Cushion above $1,000 floor: $167.48
[circuit_breaker] State transition: PAUSED → NORMAL
[bot_runner] ✓ Circuit breaker cleared - new entries enabled
```

**Failure indicators:**
```
[ERROR] Buying power still < $100
[WARNING] Circuit breaker still in PAUSED state
[bot_monitor] Equity below $1,000 floor - HALT
```

**Command to check:**
```bash
railway logs crypto-trading --tail 100 | grep -E "buying.power|equity|circuit.*clear|PAUSED|NORMAL"
```

---

## Real-Time Monitoring Script

Run this to watch all three conditions simultaneously:

```bash
#!/bin/bash
echo "Monitoring bot recovery..."
echo ""

# Watch for all three conditions
railway logs crypto-trading --tail 300 --follow | grep -E \
  "Event loop initialized|Semaphore|bound to different|
   canceled|reconcil|resized|allocation|
   buying.power|Equity|circuit.*clear|PAUSED.*NORMAL|✓"
```

**Or use the automated script:**
```bash
bash /home/user/empire-v2/deploy_recovery.sh
```

---

## Timeline Expectations

| Time | Expected Event | Log Pattern |
|------|---|---|
| T+0s | Bot starts, event loop init | `✓ Event loop initialized` |
| T+1-2s | Modules load, no Semaphore errors | `✓ Crypto grid bot module loaded` |
| T+10-15s | Order book syncs, old orders canceled | `canceled pending orders` |
| T+20-30s | Grid resizes to new sizes | `Resized grid.*allocation` |
| T+30-45s | First cycle with new sizes completes | `buying.power.*167.48` |
| T+45-60s | Circuit breaker clears | `State transition: PAUSED → NORMAL` |
| T+60+ | Trading resumes at safe leverage | `New entry orders firing` |

---

## Troubleshooting

### If Semaphore errors appear:
```
RuntimeError: asyncio.locks.Semaphore object is bound to a different event loop
```
✗ The lazy Semaphore fix may not have taken effect. Verify:
- `crypto_selection_backtest.py` lines 81-89 have lazy creation function
- `bot_runner.py` lines 27-36 initialize event loop BEFORE imports

**Re-run:**
```bash
railway logs crypto-trading --tail 50 | head -20
```

### If buying power stays at $0.23:
```
[WARNING] Buying power: $0.23 | Cushion: $17.48 (DANGER)
```
✗ The bot may not have read the new allocations. Check:
- Database has new allocations: `sqlite3 empire.db "SELECT allocated_usd FROM crypto_grid_branches WHERE active=1;"`
- Expected: ~$850 total (not $2,563.43)

**If still at $2,563.43:**
```bash
# Verify allocations in database were actually updated
sqlite3 /home/user/empire-v2/empire.db "SELECT SUM(allocated_usd) FROM crypto_grid_branches WHERE active=1;"
```

### If circuit breaker stays PAUSED:
```
[WARNING] Circuit breaker state: PAUSED (buying power insufficient)
```
✗ Margin recovery hasn't completed yet. Wait another 30-60 seconds and re-check.

---

## Success Criteria

✅ **Recovery is successful when ALL three conditions are met:**

1. **Event loop initialized** - No asyncio binding errors in first 10 seconds
2. **Order book synced** - Old orders canceled, new orders at reduced sizes
3. **Buying power at ~$167.48** and **Circuit breaker NORMAL** - Margin buffer restored

**Expected state after recovery:**
- Total allocation: $850.00
- Leverage: 0.84x
- Buying power: ~$167.48
- Cushion above floor: ~$167.48
- Circuit breaker: NORMAL
- New trades firing at 33% of previous grid sizes

---

## Next: Monitor Trading at New Scale

Once recovery is confirmed, monitor these metrics over the next 50+ trades:

```bash
# Watch P&L by coin
railway logs crypto-trading --tail 100 | grep -E "DOGE|STX|ETH|BTC|AAVE.*pnl|closed"

# Watch win rate
railway logs crypto-trading --tail 50 | grep "win.*rate\|trades.*closed"

# Watch for any new errors
railway logs crypto-trading --tail 50 | grep "ERROR\|WARNING\|FAIL"
```

**Goal:** Confirm that win rate stays above 75% and profit factor > 1.10 at the reduced scale before re-scaling again.
