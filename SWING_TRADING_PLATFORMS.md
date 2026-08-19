# Swing Trading Platforms — Integration Analysis

## Overview
Swing trading focuses on capturing price swings over days/weeks (vs. scalping for seconds). We can integrate multiple platforms for diversified revenue.

---

## 1. TradingView (Charting + Signals)

### Capabilities
- Real-time charting & technical indicators
- Custom alerts via webhooks
- Alert → API trigger → auto-trade

### Best For
- Identifying swing setups visually
- Broadcasting signals to multiple brokers simultaneously
- Multi-timeframe confluence (4H + Daily + Weekly)

### Integration Path
```
TradingView Alert Webhook
    ↓
Empire API endpoint (/trading/signals/receive)
    ↓
Route to appropriate broker (Alpaca/Coinbase/Kraken)
    ↓
Auto-execute swing trade
```

### Cost
- **Free**: Charting only
- **Pro** ($15/mo): Alerts + webhooks
- **Premium** ($30/mo): Advanced alerts + multi-alerts

### Complexity: ⭐⭐ (Medium)
- Need to build webhook receiver
- Validate signals before execution
- Manage alert fatigue

---

## 2. Alpaca (Stocks/Options - Swing Trading)

### Capabilities Already Using
- Stock & options trading (commission-free)
- Paper trading for testing
- REST API + WebSocket

### Swing Trading Specific
- Hold positions 4-20 days (our prop_bot does 2-hour scalps)
- Extended hours trading (3:30am - 8pm ET)
- Options for premium collection ($covered calls)
- Margin available (up to 4:1)

### Integration Opportunity
**Swing Stock Strategy**: Buy oversold large-cap stocks (SPY, QQQ, IWM) on weekly RSI < 30, hold 5-10 days for mean reversion bounce

### Example Setup
```python
# Complementary to prop_bot (which scalps 2-hour RSI swings)
# This one: Buy weekly RSI < 30, sell at weekly RSI > 70 (5-10 day holds)

Entry: Weekly RSI < 25, Price > 200-day SMA
Exit:  Weekly RSI > 75 OR -5% stop loss
Hold:  5-10 days typical
```

### Cost
- Free API, no commissions

### Complexity: ⭐⭐ (Medium)
- Reuse existing Alpaca setup (already connected)
- Just add weekly-timeframe logic to prop_bot

---

## 3. Polygon.io (Market Data + Technical Indicators)

### Capabilities
- Real-time stock/crypto bars in any timeframe
- Technical indicators (RSI, MACD, BB, etc.)
- Earnings/dividend calendars
- Options chain data

### Best For
- Fetching high-quality OHLCV data
- Validating signals before entry
- Screening for swing setups

### Integration Path
```
Polygon.io Data Feed
    ↓
Calculate: RSI, MACD, Bollinger Bands, Volume Profile
    ↓
Generate signal scores (1-10)
    ↓
Only trade if score >= 7
```

### Cost
- **Free**: Up to 5 API calls/minute
- **Starter** ($99/mo): 1000 calls/minute + indicators
- **Professional** ($449/mo): Unlimited + all data types

### Complexity: ⭐ (Low)
- Drop-in replacement for current bar fetching
- Already getting bars from Alpaca data, Polygon is higher quality

---

## 4. Kraken (Crypto Swing Trading)

### Capabilities vs. Coinbase
- **More pairs**: 200+ crypto pairs (vs. Coinbase ~50)
- **Lower fees**: 0.16% maker / 0.26% taker (vs. Coinbase 0.5-0.6%)
- **Margin trading**: Up to 5:1 leverage
- **Staking**: Earn APY on holdings
- **Advanced orders**: Trailing stops, OCO (one-cancels-other)

### Swing Trading Example
```
Entry:  BTC RSI 4-hour < 35 + Volume confirmation
Exit:   BTC RSI 4-hour > 65 OR +5% profit target OR -2% stop
Hold:   12-48 hours typical
```

### Integration Path
Similar to Coinbase Advanced Trade API
- REST API (authenticated)
- WebSocket for real-time prices
- Same JWT-style auth flow

### Cost
- 0.16% maker / 0.26% taker (cheaper than Coinbase)
- No monthly API fee

### Complexity: ⭐⭐ (Medium)
- Similar API to Coinbase (already integrated)
- More pairs = more entry opportunities
- Trailing stops built-in (easier exits)

---

## 5. Interactive Brokers (Advanced Swing Trading)

### Capabilities
- **Widest asset class**: Stocks, Options, Futures, Forex, Crypto, Bonds
- **Lowest commissions**: $1 per trade (or $0 for certain instruments)
- **Paper trading**: Full-featured test account
- **Advanced orders**: Brackets, trailing stops, algorithmic orders
- **Margin**: Up to 10:1 (requires $2,000+ minimum)

### Best For
- Multi-asset swing strategies
- Sophisticated order types
- Options swing strategies ($covered calls, spreads)
- Forex pairs (GBP/USD, EUR/USD, etc.)

### Integration Complexity
- Requires TWS (Trading Workstation) running or gateway
- More complex API (IBAPI)
- Steeper learning curve than Alpaca/Coinbase

### Cost
- **Account minimum**: $2,000 USD
- **Commissions**: $1 per stock trade, cheaper for forex/futures
- **Data fees**: $10/mo for real-time market data

### Complexity: ⭐⭐⭐⭐ (Hard)
- Not recommended as first additional integration
- Save for after perfecting Alpaca/Coinbase strategies

---

## 6. Binance (Crypto - Highest Volume)

### Capabilities
- **Largest trading volume**: 1000+ crypto pairs
- **Lowest fees**: 0.1% maker / 0.1% taker
- **Leverage**: Up to 20x (risky)
- **Futures**: Perpetual + dated contracts
- **Grid trading**: Automated buy/sell grid strategies

### Swing Trading Example
```
BTC/USDT Entry:  4-hour RSI < 40 + bullish divergence
BTC/USDT Exit:   4-hour RSI > 60 OR +8% profit target
Altcoin Entry:   Weekly oversold, 4-hour bounce confirmed
Hold:            3-7 days typical
```

### Integration Path
REST API (similar signature to Coinbase)

### Cost
- **Free tier**: Sufficient for our needs
- **VIP**: 0.075% maker / 0.075% taker (volume-based)

### Complexity: ⭐⭐ (Medium)
- API similar to Kraken/Coinbase
- More pairs = more scanning needed

---

## Strategy Comparison Table

| Platform | Asset Class | Hold Time | Entry Points/Day | Fee | Complexity | Priority |
|----------|------------|-----------|------------------|-----|-----------|----------|
| **prop_bot** (Alpaca) | Stocks/Futures | 2-hour | 10-20 | $0 | ⭐ | ✅ Running |
| **btc_profit_lock_bot** (Coinbase) | Crypto | Minutes-Hours | Variable | 0.5% | ⭐ | ✅ New (3x speed) |
| **Alpaca Swing** (Weekly RSI) | Stocks | 5-10 days | 2-5 | $0 | ⭐⭐ | 🟡 Next |
| **TradingView Signals** | Any broker | Any | Alert-based | $15/mo | ⭐⭐ | 🟡 Next |
| **Kraken Swing** | Crypto | 12-48h | 5-10 | 0.16% | ⭐⭐ | 🟡 After Alpaca |
| **Binance Futures** | Crypto | 1-7 days | 10+ | 0.1% | ⭐⭐⭐ | 🔴 Later |
| **Interactive Brokers** | All | Variable | 10-50 | $1 | ⭐⭐⭐⭐ | 🔴 Advanced |

---

## My Recommendation: Phased Integration

### Phase 1 (THIS WEEK)
- ✅ **prop_bot** (Alpaca 2-hour scalping) — LIVE
- ✅ **btc_profit_lock_bot** (Coinbase 10% cycling) — LIVE at 3x speed
- Add **Alpaca Swing** strategy (weekly RSI oversold entries)

### Phase 2 (NEXT WEEK)
- Add **Kraken** integration (cheaper fees, more pairs, built-in trailing stops)
- Add **TradingView** webhook receiver (broadcast signals to all brokers)

### Phase 3 (LATER)
- Binance Futures if volume increases
- Interactive Brokers for options/spreads

---

## Quick Start: Alpaca Swing Strategy

Since we already have Alpaca connected, adding a swing component is fastest:

```python
# In prop_bot.py, add parallel strategy:

# SCALP STRATEGY (existing): 5-min RSI, 2-hour holds
# SWING STRATEGY (new): Weekly RSI, 5-10 day holds

Entry: Weekly RSI < 30 + price > 200-day SMA
Target: Weekly RSI > 70 (sell 50%) or +5% profit
Stop: -3% or weekly RSI oversold reversal fails
Hold: 5-10 days typical
```

This would:
- Use same Alpaca account
- Trade same symbols (SPY, QQQ, IWM)
- Add 2-5 additional trades/week
- Capture larger swings (less fees, higher win rate)

---

## Decision Point

Which would you prefer to implement first?

1. **Alpaca Swing Strategy** — Leverages existing setup, quick win
2. **Kraken Integration** — Lower fees, 200+ crypto pairs, new revenue stream
3. **TradingView Signals** — Broadcast engine, works with any broker
4. **Binance Futures** — Highest volume, highest risk

Recommendation: **Alpaca Swing** (lowest friction, highest probability) + **Kraken** (diversify from Coinbase)
