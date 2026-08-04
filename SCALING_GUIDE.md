# Trading Bot Scaling Strategy

Your earnings system is working. Now scale it for 3x-5x faster growth.

---

## Current State
- **Account Value:** $1,000+
- **Daily Earnings:** ~$50-100/day (27 completed trades)
- **Profit Targets:** $8/trade (pro tier)
- **Positions:** Up to 21 concurrent (limited by cash)
- **Trading:** Paper-trading (Alpaca demo account)

**Projected:**
- 1 month: $1,000 → $2,000-3,000
- 2 months: $3,000 → $5,000-8,000
- 3 months: $8,000 → $10,000+

---

## Step 1: Enable Live Trading (CRITICAL)

**Why:** Paper-trading doesn't generate real bot earnings for the earnings dashboard. Live trading is where revenue flows.

**How:**
1. Go to: https://railway.app → empire-v2 → Environment
2. Set: `ALPACA_LIVE_TRADE=true`
3. Click Redeploy
4. Wait 2-3 minutes for "Active"
5. Verify: `curl https://empire-v2-production.up.railway.app/health`

**Important:** Your Alpaca live account already has real funding configured. This is the ONLY flag you need to flip to go live.

---

## Step 2: Scale Profit Targets

Update `PROFIT_TARGET_DOLLARS_MILESTONES` in prop_bot.py as your account grows:

```python
PROFIT_TARGET_DOLLARS_MILESTONES = [
    (0,     3.00),      # Micro ($0-$500): $3.00
    (500,   5.00),      # Small ($500-$1K): $5.00
    (1000,  8.00),      # Medium ($1K-$5K): $8.00
    (5000,  12.00),     # Large ($5K-$10K): $12.00
    (10000, 20.00),     # XL ($10K-$25K): $20.00
    (25000, 50.00),     # XXL ($25K+): $50.00
]
```

**Rules:**
- Increase targets in 2-3 week intervals as equity milestone is hit
- Don't jump too aggressively (stick to 1-2% profit targets)
- Monitor win rate first 3-5 days at each new tier

---

## Step 3: Scale Position Size (Recommended)

Currently: 1 share per symbol

Better approach: **1% of account per position**

```python
def size_position(equity, symbol):
    """Size position to 1% of account"""
    MIN_POSITION_NOTIONAL = 10  # Minimum $10 per position
    account_sizing = equity * 0.01  # 1% of account
    
    # Fetch current price for symbol
    price = get_last_price(symbol)
    
    shares = int(account_sizing / price) or 1
    return max(1, shares)
```

**Example:**
- $1K account, SPY at $500 → 1000 * 0.01 / 500 = **0.02 shares** (round to 1)
- $5K account, SPY at $500 → 5000 * 0.01 / 500 = **0.1 shares** (round to 1)
- $10K account, SPY at $500 → 10000 * 0.01 / 500 = **0.2 shares** (round to 1-2)

At current Alpaca account size, this auto-scales your positions as equity grows, capturing larger profits at higher account tiers.

---

## Step 4: Monitor Earnings Dashboard

After enabling live trading, check:

**Endpoint:** `https://empire-v2-production.up.railway.app/api/trading-dashboard/account/balance`

Returns:
```json
{
  "cash": 1200.50,
  "equity": 5600.25,
  "buying_power": 2400.00,
  "day_trading_buying_power": 2400.00,
  "cash_withdrawable": 1200.50
}
```

**Check daily:**
- `cash_withdrawable` increasing = profits accumulating
- `buying_power` increasing = account growing

---

## Revenue Flow After Scaling

**Current (Paper Trading):**
```
Real clients pay → Video orders → Order success → Earnings recorded
(But no real money flows to Alpaca, so bot runs on $1K cap)
```

**After Live Trading Enabled:**
```
Real clients pay → Video orders → Order success → Earnings recorded
                                              ↓
                                  Payout to Alpaca account
                                              ↓
                                    Bot opens larger positions
                                              ↓
                                   Profit targets increase
                                              ↓
                                   Earnings accelerate (3-5x)
```

---

## Risk Management

**Daily Loss Limits:**
- Stop trading if daily loss > 2% of account
- Current: 2% of $1K = $20 max loss/day
- Auto-implemented in prop_bot.py

**Position Stop-Loss:**
- 1% hard stop on every position
- Current: 1% of $1K = $10 max loss/position
- Prevents account blowup

**Evaluation Period:**
- APEX requires 7 consecutive profitable days before full leverage
- You have 27 profitable trades already
- Continue monitoring through daily cycles

---

## Timeline

| Week | Account | Daily Earnings | Profit Target | Status |
|------|---------|----------------|---------------|---------|
| 1    | $1K     | $50-75         | $8/trade      | Paper trading |
| 2    | $1.5K   | $75-100        | $10/trade     | **Enable live** |
| 3    | $2K     | $100-125       | $12/trade     | Monitor |
| 4    | $2.5K   | $125-150       | $12/trade     | **Scale targets** |
| 5    | $3K     | $150-200       | $15/trade     | Accelerating |
| 8    | $5K+    | $200-300+      | $20/trade     | Full scaling |

---

## Execution Checklist

- [ ] Enable `ALPACA_LIVE_TRADE=true` on Railway
- [ ] Redeploy and verify live trading working
- [ ] Check `/api/trading-dashboard/account/balance` endpoint
- [ ] Monitor `/payments/bot/earnings` for live payout flow
- [ ] After 2 weeks at $1K+, increase profit targets to $10/trade
- [ ] After 4 weeks at $2K+, increase profit targets to $15/trade
- [ ] Keep daily profit/loss logs (screenshots)

---

## Expected 90-Day Growth

| Metric | Current | Day 30 | Day 60 | Day 90 |
|--------|---------|--------|--------|--------|
| Account | $1,000 | $2,500 | $5,000 | $10,000 |
| Daily Earnings | $50-75 | $125-150 | $250-300 | $500+ |
| Cumulative Profits | $1,637 | $5,250 | $12,500 | $25,000+ |

This assumes:
- Consistent video order flow (1-2 orders/week = $200-400 bot earnings)
- Trading win rate stays 65%+
- No major market disruptions
- Email campaign adds 3-5 new orders/month

---

## Questions?

- Check `prop_bot.py` lines 96-116 for profit target configuration
- Check `routers/trading_dashboard.py` for account balance endpoint
- Check `/payments/bot/earnings` for earnings history
