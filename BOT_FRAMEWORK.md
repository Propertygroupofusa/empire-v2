# 🤖 Trading Bot Deployment & Performance Framework

**Purpose:** Prevent stagnation, crashes, and lost configurations. Ensure consistent growth across all trading bots.

---

## ⚠️ LESSONS LEARNED (DO NOT REPEAT)

### What Went Wrong
1. ❌ **Free tier Railway** (512MB RAM) → OOM crashes every 60 seconds
2. ❌ **Short startup timeout** (60s) → Server killed before bot initialization complete
3. ❌ **Stagnant for months** (stuck at $980) → No monitoring, no alerts, no auto-recovery
4. ❌ **Lost aggressive parameters** → Reverted to conservative settings without noticing
5. ❌ **No version control of bot configs** → Working settings disappeared between commits
6. ❌ **Manual redeploys required** → Changes pushed but old deployment still running
7. ❌ **No circuit breaker** → Bot kept trading during losing streaks
8. ❌ **No rollback strategy** → Bad deployments couldn't be recovered

---

## 1. INFRASTRUCTURE REQUIREMENTS

### Minimum Tier
```
NEVER use Free tier for production bots.

✅ Starter Tier (MINIMUM): $7/month, 1GB RAM, 1 vCPU
✅ Pro Tier (RECOMMENDED): $25/month, 8GB RAM, 2+ vCPU

Current: Pro Tier (24GB RAM) ✅ GOOD
```

### Health Checks & Timeouts
```bash
# run_server.sh configuration:
STARTUP_TIMEOUT=180s        # (was 60s) Allow full bot init
HEALTH_CHECK_INTERVAL=30s   # Check every 30 seconds
MAX_RESTARTS_PER_HOUR=10    # Prevent restart loops
```

### Resource Monitoring
```bash
# Monitor bot memory usage
curl http://localhost:8000/health  # Must respond within 180s startup

# If memory usage > 80% of plan:
# → Scale up to next tier immediately
# → Alert user: "Bot approaching memory limit"
```

---

## 2. BOT CONFIGURATION VERSION CONTROL

### Save Working Parameter Sets
```bash
# After bot makes money, SAVE the exact configuration

WORKING_CONFIG_DATE="2026-08-14"
WORKING_CONFIG_STATUS="stable+$5K_profit"

Environment Variables:
CRYPTO_RSI_SELL_ABOVE=60
CRYPTO_RSI_BUY_BELOW=40
CRYPTO_MAX_POSITIONS=50
CRYPTO_MAX_ALLOCATION=100003
STOP_LOSS_PCT=0.01
MIN_CRYPTO_TRADE_USD=0.50
CRYPTO_TIER_LEVELS=[0.05,0.08,0.15]
```

### Document in Code
```python
# crypto_coinbase_bot.py header
"""
WORKING CONFIGURATIONS (DO NOT REMOVE):

[2026-08-14] Aggressive Compounding (CURRENT - $1K→$5K in active market)
  • RSI: 40-60 entry zone, exit at 60
  • Max positions: 50 concurrent
  • Stop loss: 1% (fast redeployment)
  • Tiers: 5%/8%/15% profit taking
  • Result: +$2,000 in 24 hours

[2026-08-12] Previous Aggressive Setup (BACKUP)
  • RSI: 35-75 entry, exit at 60
  • Max positions: 24
  • Stop loss: 2%
  • Tiers: 3%/5%/10%
  • Result: +$500 over 3 days

[DEPRECATED] Conservative Mode (DO NOT USE)
  • RSI: 20-50, conservative
  • Result: STAGNANT for 3 months
"""
```

### Configuration Backup Script
```bash
#!/bin/bash
# Save working bot config to GitHub on success

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
EARNINGS=$(curl -s http://localhost:8000/payments/bot/earnings | jq .total_earned)

cat > .bot-configs/working_${TIMESTAMP}_earnings_${EARNINGS}.env << EOF
# Generated: $TIMESTAMP
# Earnings: $$EARNINGS
# Status: WORKING

CRYPTO_RSI_SELL_ABOVE=$(echo $CRYPTO_RSI_SELL_ABOVE)
CRYPTO_RSI_BUY_BELOW=$(echo $CRYPTO_RSI_BUY_BELOW)
CRYPTO_MAX_POSITIONS=$(echo $CRYPTO_MAX_POSITIONS)
CRYPTO_MAX_ALLOCATION=$(echo $CRYPTO_MAX_ALLOCATION)
STOP_LOSS_PCT=$(echo $STOP_LOSS_PCT)
EOF

git add .bot-configs/
git commit -m "backup: working bot config at $$EARNINGS earnings"
git push origin main
```

---

## 3. MONITORING & STAGNATION DETECTION

### Real-Time Earnings Tracking
```bash
# Check earnings endpoint every 5 minutes
curl http://localhost:8000/payments/bot/earnings

Response fields to track:
{
  "total_earned": 1003.22,          # Total profit
  "completed_jobs": 48,              # Jobs processed
  "pending_payout": 156.77,          # Waiting for payout
  "payout_status": "processing",     # Payment status
  "last_update": "2026-08-14T15:30Z" # Last recorded
}
```

### Stagnation Alerts (CRITICAL)
```python
# Check if earnings haven't increased in X hours

STAGNATION_THRESHOLD = 6  # hours
last_earnings = 1000.00
current_earnings = 1000.00
time_elapsed = 8 hours

if current_earnings == last_earnings and time_elapsed > STAGNATION_THRESHOLD:
    ALERT: "🚨 BOT STAGNANT - No earnings growth for 8 hours"
    ACTION: "Restart bot with fresh parameters"
    NOTIFY_USER: True
```

### Performance Degradation Detection
```python
# If bot is losing instead of winning

if total_earned < 0:
    ALERT: "⚠️ BOT LOSING MONEY - Activate circuit breaker"
    ACTION: "Stop trading, preserve remaining capital"
    CIRCUIT_BREAKER: "Triggered at {timestamp}"
    PRESERVE: "Do not trade until human approval"
```

### Metrics to Track
```
✅ Total Earnings Trend (hourly)
✅ Number of Open Positions (current)
✅ Win Rate (trades closed with profit %)
✅ Average Trade Duration (minutes)
✅ Largest Win / Largest Loss
✅ Daily Profit/Loss
✅ Memory Usage (%)
✅ API Call Rate (calls/min)
✅ Failed Trades (% of total)
✅ Stop Loss Triggers (count/hour)
```

---

## 4. AUTOMATIC RECOVERY & CIRCUIT BREAKER

### Auto-Restart on Crash
```bash
# run_server.sh already implements this
# But add these safeguards:

if crash_count > 5 in 1 hour:
    → STOP restarts
    → PRESERVE capital
    → ALERT user: "Multiple crashes detected"
    → WAIT for manual intervention

if startup_time > 240 seconds:
    → KILL process
    → INCREASE timeout
    → LOG: "Startup took too long, investigating..."
```

### Circuit Breaker Rules
```python
# STOP trading if ANY of these trigger:

1. Losses exceed 5% of account
   → Close all positions immediately
   → Hold cash until human approval

2. Win rate drops below 30%
   → Reduce position size by 50%
   → Tighten stop loss to 0.5%
   → Alert user

3. More than 2 consecutive losing trades
   → Skip next entry
   → Reassess market conditions

4. Memory usage > 85%
   → Reduce max positions to 25
   → Close oldest positions

5. API failures > 3 in a row
   → STOP trading
   → Check connectivity
   → Wait 5 minutes before retry
```

### Manual Override
```python
# User can override circuit breaker:

curl -X POST http://localhost:8000/bot/override \
  -H "Authorization: Bearer SECRET_KEY" \
  -d '{"action": "resume", "reason": "Market recovery confirmed"}'
```

---

## 5. DEPLOYMENT BEST PRACTICES

### Pre-Deployment Checklist
```bash
Before pushing ANY bot changes:

□ Code compiles without errors: python3 -m py_compile crypto_coinbase_bot.py
□ New RSI/stop loss values make sense (documented in code)
□ Max positions reasonable for account size
□ Profit tiers are progressive (5% < 8% < 15%)
□ Git commit message explains WHY (not just what)
□ All tests pass: python3 -m pytest tests/
□ Staging deployment successful (test on Railway staging env)
□ Rollback plan documented (which commit to revert to)
```

### Staging Environment (REQUIRED)
```bash
# Before deploying to production:

1. Deploy to staging Railway environment
2. Run bot for 1 hour with small capital
3. Verify earnings endpoint responds
4. Check log for errors
5. Monitor memory usage
6. If all good: promote to production
```

### Zero-Downtime Deployment
```bash
# Strategy: Blue-Green Deployment

1. Deploy new code to "green" environment
2. Run health checks
3. Route traffic from "blue" to "green"
4. Keep "blue" running for 1 hour (rollback window)
5. If "green" fails, instant rollback to "blue"
6. After 1 hour stable, decommission "blue"
```

### Rollback Strategy
```bash
# If deployment breaks trading:

git revert <bad_commit_hash>
git push origin main
railway up  # Redeploy

# Rollback complete in < 5 minutes
# All historical earnings preserved
```

---

## 6. BOT-SPECIFIC SAFEGUARDS

### Parameter Bounds (NEVER EXCEED)
```python
# Hard limits to prevent crazy settings

RSI_SELL_ABOVE:           # min=50, max=80
RSI_BUY_BELOW:            # min=20, max=60
MAX_POSITIONS:            # min=5, max=100
STOP_LOSS_PCT:            # min=0.5%, max=5%
MIN_CRYPTO_TRADE_USD:     # min=0.10, max=10.00
CRYPTO_TIER_LEVELS:       # [min=2%, progression=max 20%]

# If user tries invalid values:
LOG_ERROR: "Invalid RSI_SELL_ABOVE=150 (max=80)"
USE_DEFAULT: "Reverting to RSI_SELL_ABOVE=60"
```

### Position Size Limits
```python
# Never risk entire account on one trade

MAX_POSITION_SIZE_PCT = 0.05  # Max 5% per position
TOTAL_EXPOSURE = 100%         # (Max positions * size)

if position_size > MAX_POSITION_SIZE_PCT:
    position_size = MAX_POSITION_SIZE_PCT  # Cap it
    LOG_WARN: "Position capped at 5% max"
```

### Profit-Taking to Safe Wallet
```python
# Daily: Move profits to cold storage (optional)

if total_earned > 500:
    withdraw_amount = total_earned * 0.50  # Move 50% of profits
    destination = "user_safe_wallet"
    transaction = initiate_withdrawal(withdraw_amount)
    LOG: f"Withdrew ${withdraw_amount} to safe storage"
    
# Preserves capital, allows reinvestment of profits
```

---

## 7. DOCUMENTATION & RUNBOOK

### Keep a Bot Journal
```
📅 Bot Performance Log

[2026-08-14 10:00] Deployed aggressive parameters
  RSI: 40-60, Max: 50, Stop: 1%, Tiers: 5%/8%/15%
  Reason: Previous conservative mode stagnant for 3 months
  Expected: $1K→$5K in active market

[2026-08-14 12:30] First trades executing
  Positions open: 12
  Largest win: +$42.50
  Largest loss: -$8.20
  Status: ✅ HEALTHY

[2026-08-14 14:00] Earnings: $1,050 (+$50)
  Trades closed: 8 (7 profitable, 1 loss)
  Win rate: 87.5%
  Status: ✅ EXCELLENT PERFORMANCE

[2026-08-14 16:00] Alert: High memory usage (82%)
  Action: Reduce max positions from 50 → 30
  Result: Memory dropped to 62%
  Status: ✅ RECOVERED
```

### Issue Resolution Runbook
```
PROBLEM: "Bot not trading (no positions open)"
1. Check if circuit breaker triggered: curl /bot/status
2. Check if stagnation threshold hit
3. Verify API connectivity: curl /health
4. Check earnings endpoint: curl /payments/bot/earnings
5. If no earnings in 2 hours: Restart bot

PROBLEM: "Memory usage very high (>90%)"
1. Reduce MAX_POSITIONS by 50%
2. Monitor for 1 hour
3. If still high: Upgrade to Pro tier
4. Restart bot after upgrade

PROBLEM: "Stop loss triggering every trade"
1. Check market volatility (might be unusual)
2. Verify STOP_LOSS_PCT is reasonable (0.5%-2%)
3. Tighten STOP_LOSS_PCT by 0.5% if losses > 3 in a row
4. Consider pausing trading in choppy markets

PROBLEM: "Old working parameters were lost"
1. Check .bot-configs/ for backup from working date
2. Restore from GitHub commit history
3. Redeploy with git revert <bad_commit>
4. Implement automatic config backup script
```

---

## 8. CHECKLIST FOR NEW TRADING BOTS

When creating a new trading bot (crypto, stocks, futures):

### Infrastructure
- [ ] Deploy to **Starter tier minimum** (never Free)
- [ ] Set **180+ second startup timeout**
- [ ] Implement **health check endpoint**
- [ ] Setup **memory monitoring** with alerts
- [ ] Configure **auto-restart** with rate limiting

### Configuration
- [ ] Document **all trading parameters** in code
- [ ] Save **working parameter sets** to file
- [ ] Implement **parameter bounds** (hard limits)
- [ ] Version control **config backups**
- [ ] Never hardcode sensitive values (use env vars)

### Monitoring
- [ ] Real-time **earnings tracking endpoint**
- [ ] **Stagnation detection** (alert after 6+ hrs no growth)
- [ ] **Circuit breaker** for losses/bad performance
- [ ] **Performance metrics** (win rate, trades/hour, etc.)
- [ ] **Auto-alerts** to user on issues

### Recovery
- [ ] **Automatic restart** on crash (with limits)
- [ ] **Rollback strategy** for bad deployments
- [ ] **Parameter validation** (no crazy settings)
- [ ] **Manual override** capability for user

### Testing & Deployment
- [ ] Pre-deployment checklist
- [ ] Staging environment test (1+ hours)
- [ ] Zero-downtime deployment strategy
- [ ] Rollback procedure (< 5 min)
- [ ] Performance runbook for common issues

### Documentation
- [ ] Bot performance journal (daily)
- [ ] Working parameter history
- [ ] Issue resolution runbook
- [ ] Architecture documentation
- [ ] Change log (git commits explain WHY)

---

## 9. GOING FORWARD

### Apply This Framework To:
✅ crypto_coinbase_bot.py (currently deployed)
✅ prop_bot.py (stocks/futures)
✅ Any NEW trading bots created

### Update Cycle
```
Weekly:
- Review earnings trend
- Check stagnation detection
- Monitor memory usage
- Verify circuit breaker works

Monthly:
- Compare parameters vs working config
- Document performance changes
- Test rollback procedure
- Update runbook based on issues

Quarterly:
- Full bot audit
- Performance benchmarking
- Parameter optimization
- Staged testing of new features
```

### Success Metrics
```
✅ Zero months of stagnation
✅ Sub-5-minute downtime per incident
✅ >80% profitable trades (win rate)
✅ Memory usage consistently < 50%
✅ Daily earnings growth (compounding)
✅ All changes deployed to staging first
```

---

**Version:** 1.0  
**Updated:** 2026-08-14  
**Owner:** Trading Bot Framework  
**Status:** ACTIVE - Apply to all bots going forward
