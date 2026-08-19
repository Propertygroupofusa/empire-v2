# 🚀 Trading System Activation Guide

## Status: ✅ COMPLETE - All 3 Systems Ready

You now have a complete automated trading system with live monitoring and P&L tracking.

---

## What You Got (All 3 Delivered)

### 1️⃣ **Pull Request for Deployment**
- **GitHub PR:** https://github.com/Propertygroupofusa/empire-v2/pull/191
- **Status:** Draft (ready for merge)
- **Commits:** Includes bot activation + monitoring + dashboard

### 2️⃣ **Real-Time Trade Monitoring** 
**File:** `trade_alerts.py`

Features:
- ✅ Slack webhook integration (trades send alerts)
- ✅ Email notifications support
- ✅ Monitors Coinbase & Alpaca bots
- ✅ Auto-alerts on entry/exit signals
- ✅ Daily trade summaries

### 3️⃣ **Live P&L Dashboard**
**Route:** https://empire-v2-production.up.railway.app/trading/dashboard

Features:
- ✅ Beautiful real-time P&L display
- ✅ Shows realized + unrealized P&L
- ✅ Lists open positions
- ✅ Shows last 20 trades
- ✅ Auto-refreshes every 30 seconds
- ✅ Mobile responsive

---

## Quick Setup (3 Steps)

### Step 1: Add Environment Variables to Railway

Go to: https://railway.app/dashboard

1. Click **empire-v2** project
2. Click **main-app** service
3. Go to **Variables** tab
4. Add these 6 variables:

```
COINBASE_API_KEY_NAME
organizations/5b4914b8-0d95-498b-ad08-ff87106f81ad/apiKeys/0f4f16dd-c9c7-41b7-b00b-b6f7450f3974

COINBASE_API_PRIVATE_KEY
MAr6+DY1bDU5Yw5wFC5xwo3L0k1SapmhgSAJpxg0jfd9+PR+YFVeL2QZUeOY62AAgfupXaSZtDA8nr1ET0TqLg==

ALPACA_API_KEY
AKNWJPHFWXORCPNJB2Q7PYPEFA

ALPACA_SECRET_KEY
FTcguboUp9jZMMtbvVzphn6Akke7brF25gbsZUqqrmmN

ALPACA_BASE_URL
https://api.alpaca.markets

ALPACA_LIVE_TRADE
true
```

### Step 2: Redeploy

Click **Redeploy** in Railway dashboard
Wait 2-3 minutes for "Active" status

### Step 3: Access Dashboard

Open: https://empire-v2-production.up.railway.app/trading/dashboard

---

## Your Trading Bots

### Coinbase Crypto Bot (24/7)
- **Symbols:** BTC/USD, ETH/USD, SOL/USD, XRP/USD, AVAX/USD, LINK/USD, DOGE/USD, SHIB/USD, NEAR/USD, MATIC/USD
- **Strategy:** RSI-based mean reversion
- **Risk:** 0.1% stop-loss, $10 daily max loss
- **Capital:** Compounds automatically (reinvests profits)
- **Your Account:** USD balance from Coinbase

### Alpaca Futures Bot (Market Hours)
- **Symbols:** MES (S&P 500), MNQ (Nasdaq), MGC (Gold)
- **Strategy:** Mean reversion with momentum
- **Risk:** 0.1% stop-loss, $10 daily max loss
- **Capital:** Your $800 account
- **Mode:** LIVE TRADING (real money)

---

## Monitoring URLs

Once deployed:

```
📈 Dashboard:     https://empire-v2-production.up.railway.app/trading/dashboard
📡 Health Check:  https://empire-v2-production.up.railway.app/health
🔍 Analytics:     https://empire-v2-production.up.railway.app/crypto/analytics
📋 P&L JSON API:  https://empire-v2-production.up.railway.app/trading/api/pnl-summary
```

---

## What You'll See

### Dashboard Display
```
💹 Trading Dashboard
📈 Total Daily P&L: +$245.50
├─ Realized P&L: +$235.00 (green)
├─ Unrealized P&L: +$10.50 (green)
├─ Open Positions: 3
└─ Today's Trades: 8
```

### Example Trade
```
🟢 BTC/USD
   Entry:  $40,500 @ 0.01 BTC
   Entry Price: $40,500
   Size: 0.01
   P&L: +$450.00
   Time: 14:32:15
```

---

## Optional: Slack Alerts

To get trade alerts on Slack:

1. Go to your Slack workspace
2. Settings → App Directory
3. Create Incoming Webhook
4. Copy webhook URL
5. Add to Railway Variables:

```
SLACK_WEBHOOK_URL
https://hooks.slack.com/services/YOUR/WEBHOOK/URL
```

---

## Risk Management

**Per Trade:**
- Stop-loss: 0.1% (automatic exit if down 0.1%)
- Take-profit: No cap (let winners run)

**Daily:**
- Max loss: $10 per day
- Auto-stops trading if limit hit

**Position Sizing:**
- Automatic based on account balance
- Coinbase: Compounds (profits reinvested)
- Alpaca: Fixed at $800 starting

---

## Troubleshooting

### Bots not trading?
1. Check Railway logs
2. Verify API credentials in Variables tab
3. Confirm Coinbase/Alpaca account has funds
4. Check broker API status

### Dashboard showing no trades?
1. Wait for bots to execute first trade (depends on RSI signal)
2. Refresh dashboard (Ctrl+F5)
3. Check /crypto/analytics for bot logs

### Alerts not sending?
1. Verify SLACK_WEBHOOK_URL is correct
2. Check Railway logs for errors
3. Ensure Slack webhook is active

---

## Support

Issues? 
- Check Railway logs for error messages
- Verify all 6 environment variables are set
- Contact Del: delfarrell591@gmail.com

---

## What's Next?

✅ Add env vars to Railway
✅ Click Redeploy
✅ Wait 2-3 minutes
✅ Open dashboard
✅ Watch trades execute
✅ Monitor P&L accumulate

**You're now live with real money trading!** 🚀
