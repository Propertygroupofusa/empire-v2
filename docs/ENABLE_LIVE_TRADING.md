# ENABLE LIVE ALPACA TRADING — Step-by-Step Deployment

**Date:** 2026-07-22  
**User Decision:** Go live with Alpaca full capital (stocks/ETFs)  
**Status:** Ready to deploy (requires Railway env var change)

---

## What's Changing

### Prop Bot (Alpaca)
```
BEFORE: ALPACA_LIVE_TRADE=false  (paper trading, no real capital)
AFTER:  ALPACA_LIVE_TRADE=true   (LIVE TRADING, real capital deployed)

BEFORE: ALPACA_BASE_URL=https://paper-api.alpaca.markets
AFTER:  ALPACA_BASE_URL=https://api.alpaca.markets
```

### Result
- Bot will place **real buy/sell orders** on your Alpaca account
- Real capital will be at risk (24/5 market hours)
- Profits and losses are real money
- Possible losses: $1K-$5K+ in minutes if strategy underperforms

---

## Prerequisites

✅ ALPACA_API_KEY configured in Railway  
✅ ALPACA_SECRET_KEY configured in Railway  
✅ Capital deposited in Alpaca account  
✅ User accepts rapid loss potential  

---

## Deployment Steps

### Step 1: Go to Railway Dashboard
1. Open https://railway.app
2. Navigate to **empire-v2** project
3. Click **main-app** deployment

### Step 2: Set Environment Variables
In the **Variables** section, add/update:

```
ALPACA_LIVE_TRADE=true
ALPACA_BASE_URL=https://api.alpaca.markets
```

### Step 3: Redeploy
1. Click **Redeploy**
2. Wait for "Active" status (~2-3 minutes)

### Step 4: Verify Live Trading Is Active
```bash
curl -H "X-Admin-Key: YOUR_ADMIN_KEY" \
  https://empire-v2-production.up.railway.app/trading_dashboard/status
```

Look for:
```json
{
  "mode": "LIVE",
  "account": {...real equity data...},
  "positions": [...real positions...],
  "todays_orders": [...real trades placed today...]
}
```

### Step 5: Monitor First Hour
- Watch **Trading Dashboard**: https://empire-v2-production.up.railway.app/trading_dashboard.html
- Refresh every 10 seconds
- Verify bot is:
  - ✅ Placing real orders (ES/QQQ/SPY positions)
  - ✅ Generating entry/exit signals
  - ✅ Capturing real P&L

---

## If Something Goes Wrong (Emergency Stop)

Set immediately in Railway:
```
STOP_TRADING=true
```

This **pauses all trading immediately** (both Alpaca and Tradovate bots).

---

## Monitoring During Live Trading

### Key Metrics to Track
- **Current Equity:** Total account value
- **Cash Available:** Buying power
- **Open Positions:** Live holdings (should see ES/QQQ/SPY)
- **Today's Trades:** Filled orders with entry/exit prices
- **P&L:** Real profit/loss (updates every 10 seconds)

### Expected Behavior (First Day)
- **Best case:** +$500 to +$5K profit (trending market)
- **Normal case:** -$200 to +$1K (choppy/sideways market)
- **Worst case:** -$2K to -$5K (against-trend market, bad entry timing)

### If Losses Exceed -$5K in One Day
1. Set `STOP_TRADING=true` (emergency stop)
2. Analyze bot strategy (RSI thresholds may need adjustment)
3. Review Alpaca logs for order timing issues
4. Consider adjusting RSI_BUY_BELOW / RSI_SELL_ABOVE thresholds

---

## Current Bot Configuration

**Prop Bot (Alpaca) Settings:**
```
Strategy: RSI + trend confirmation (SMA5 > SMA10)
Entry: RSI < 45 + bullish trend
Exit: RSI > 55 or bearish trend flip
Symbols: SPY (S&P 500), QQQ (Nasdaq), GLD (Gold)
Position Size: 1-5 shares per trade (scaled by available cash)
Timeframe: 5-minute bars
Update Interval: Every 5-10 seconds
```

---

## After First Day of Live Trading

### If Profitable (+$500+)
✅ System is working  
✅ Continue monitoring  
✅ Can increase position size if desired  

### If Flat (-$500 to +$500)
🟡 Normal variance  
🟡 Continue monitoring for 3-5 days  
🟡 May need minor RSI threshold adjustment  

### If Losing (-$1K to -$5K)
⚠️ Strategy underperforming  
⚠️ Set `STOP_TRADING=true` immediately  
⚠️ Investigate:
  - Is RSI signal too weak? (Loosen thresholds to 50/50?)
  - Are positions closing too early? (Adjust SMA logic?)
  - Is trend detection inverted? (Check SMA direction)

---

## Rollback (If Needed)

If live trading is causing unacceptable losses:

1. **Immediate Stop:**
   ```
   STOP_TRADING=true
   ```

2. **Revert to Paper Trading:**
   ```
   ALPACA_LIVE_TRADE=false
   ALPACA_BASE_URL=https://paper-api.alpaca.markets
   Redeploy
   ```

3. **Analyze & Re-optimize:**
   - Review 1-5 days of real trade logs
   - Adjust RSI thresholds
   - Test new parameters in paper mode
   - Re-enable live only after proven improvement

---

## Support & Monitoring

**Real-time Status:**
- Trading Dashboard: https://empire-v2-production.up.railway.app/trading_dashboard.html
- API Docs: https://empire-v2-production.up.railway.app/docs

**Logs & Debugging:**
- Railway deploy logs (check for bot startup errors)
- Alpaca Dashboard (https://alpaca.markets) — review all orders placed by the bot

**Contacts:**
- Alpaca API Docs: https://alpaca.markets/docs/api/
- Email: delfarrell591@gmail.com

---

**NEXT ACTION:** Go to Railway dashboard → set `ALPACA_LIVE_TRADE=true` → Redeploy → Monitor trading dashboard

**Expected Result:** Real trades within 1-2 minutes of deployment
