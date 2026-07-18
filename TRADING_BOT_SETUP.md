# Trading Bot Deployment Guide

## Overview

**Empire v2** includes a production-ready crypto trading bot designed for your live $980 Alpaca account (APEX_589296).

### Active System: Bot #2 (Crypto Scalper)

- **24/7 Crypto Trading** — BTC, ETH, SOL, AVAX, DOGE, LINK
- **Cycle Time:** 15 minutes (vs hourly stocks)
- **Strategy:** RSI + Bollinger Bands + Volume confirmation
- **Stop Loss:** 0.8% | **Take Profit:** 2.4% (3:1 ratio)
- **Position Size:** Up to 4 concurrent positions
- **Daily Trades:** 6-10 per day
- **Compounding:** Every profit reinvested

### Deployment Roadmap

| Portfolio | Status | Timeline |
|-----------|--------|----------|
| $980 | Starting (Live) | Day 1 |
| $1,000 | 🌿 Hit first milestone | ~5 days at 2%/day |
| $5,000 | 💪 Serious capital | ~50 days |
| $10,000 | 🔥 5 figures | ~80 days |
| $50,000 | 🚀 Halfway | ~140 days |
| $100,000 | 🏆 **TARGET** | ~160 days |

---

## Setup Instructions

### 1. **Verify Environment Variables on Railway**

Required variables (already configured):
```
ALPACA_API_KEY=<your live key>
ALPACA_SECRET_KEY=<your live secret>
ALPACA_BASE_URL=https://api.alpaca.markets (LIVE, not paper)
ALPACA_LIVE_TRADE=true
STARTING_CAPITAL=$980
```

### 2. **Start the Bot**

```bash
# Run on Railway via custom service in railway.json
python bot_2_crypto_scalper.py

# Or run locally for testing (in paper mode):
export ALPACA_BASE_URL=https://paper-api.alpaca.markets
python bot_2_crypto_scalper.py
```

### 3. **Monitor P&L in Real-Time**

The P&L tracker runs alongside the bot:
```bash
python bot_pl_tracker.py
```

Tracks:
- Portfolio value every 60 seconds
- Open positions with individual P&L
- Milestone achievements ($1k, $5k, $10k, etc.)
- Historical data in `bot_pl_history.json`

### 4. **Logs & State**

- **Trading Log:** `bot2_crypto.log` — all orders, signals, P&L
- **State File:** `bot2_state.json` — persists day count, win/loss ratio, peak portfolio
- **P&L History:** `bot_pl_history.json` — snapshots every minute

---

## Monitoring

### Live Dashboard

Check bot status in your logs:
```
=======================================================
⏱ BOT #2 CRYPTO | 🔴 LIVE | $1,245.67
  💪 Serious | Days to $100k: ~140
  Per-trade: $250.00 | W:12 L:3 (80% WR)
```

### Key Metrics

- **Win Rate:** Percentage of profitable trades
- **P&L:** Total profit/loss since starting
- **Days to Target:** Estimated days to reach $100k

### Alert Conditions

The bot stops trading if:
- Daily loss exceeds 3% (rests until next day)
- Daily trades reach 10 (waits for next 15-min cycle)
- Portfolio reaches $100k (target achieved!)

---

## Alpaca Account

- **Account ID:** APEX_589296
- **Account Type:** Live Trading (real money)
- **Starting Capital:** $980
- **Positions Allowed:** 4 concurrent
- **Leverage:** None (spot trading only)

### Key Features

- ✅ Crypto 24/7 trading (unlike stocks 9:30am-4pm)
- ✅ Minute-level resolution (15-min bars)
- ✅ No leverage/margin (safer)
- ✅ Historical data via Alpaca Data API

---

## Configuration

Edit `bot_2_crypto_scalper.py`:

```python
CONFIG = {
    "starting_capital": 980.0,        # Update if you add capital
    "target_portfolio": 100_000.0,    # Stop at this amount
    "daily_target_pct": 2.0,          # Daily profit target
    "max_daily_loss_pct": 3.0,        # Daily stop loss
    "stop_loss_pct": 0.8,             # Per-trade stop loss
    "take_profit_pct": 2.4,           # Per-trade take profit
    "max_positions": 4,               # Max open positions
    "max_trades_day": 10,             # Max trades per day
    "cycle_minutes": 15,              # Run every 15 minutes
}
```

### Crypto Pairs

```python
CRYPTOS = {
    "BTC/USD": {"tier": 1},    # Bitcoin
    "ETH/USD": {"tier": 1},    # Ethereum
    "SOL/USD": {"tier": 1},    # Solana
    "AVAX/USD": {"tier": 2},   # Avalanche
    "DOGE/USD": {"tier": 2},   # Dogecoin
    "LINK/USD": {"tier": 2},   # Chainlink
}
```

To add/remove cryptos, edit the dict and restart the bot.

---

## Performance History

Current P&L tracking started: 2026-07-18

View live P&L:
```bash
tail -f bot2_crypto.log
cat bot_pl_history.json | jq '.'
```

---

## Troubleshooting

### Bot not connecting
```
❌ Missing ALPACA_API_KEY in .env
❌ Cannot connect to Alpaca
```

**Fix:** Verify `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are set on Railway dashboard

### No orders placed
```
[BTC/USD] RSI:65 | HOLD | score=0/3
[ETH/USD] RSI:72 | HOLD | score=0/3
```

**Reason:** Waiting for oversold conditions (RSI < 45) or Bollinger Band signals

**Normal:** Bot enters trades opportunistically, not every cycle

### Portfolio value doesn't match Alpaca
- **Expected:** 1-2 minute delay due to API latency
- **Check:** Dashboard → https://app.alpaca.markets

---

## Next Steps

1. ✅ **Deployment:** Merge PR → Railway auto-redeploys
2. ✅ **P&L Tracking:** Monitor real-time performance
3. ⏳ **Live Trading:** Bot automatically trades $980 capital
4. 📊 **Milestones:** Track progress toward $100k target

---

## References

- **Alpaca API:** https://alpaca.markets/docs/api-references
- **Crypto Data:** https://data.alpaca.markets/v1beta3
- **Live Account:** https://app.alpaca.markets (Dashboard)

---

**Status:** 🟢 **Live Trading Enabled**  
**Capital:** $980 (Real Money)  
**Mode:** 🔴 LIVE  
**Target:** $100,000  
