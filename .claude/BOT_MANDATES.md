# Trading Empire Bot Mandates

Each bot has a **distinct job**. This document defines success/failure for each.

---

## Bot 1: APEX Prop Bot (Alpaca)

**Primary Mandate:** Pass funded-account evaluation ($1,500 target, $1,000 max drawdown)

### What It Should Do
- Aggressively pursue $1,500 profit target
- Enter only when RSI oversold + trend confirmation agree
- Use tiered exits: lock profits at 50%, 100%, 150% of target
- Respect drawdown limit absolutely (evaluation constraint)
- Log every trade for post-mortem analysis

### What It Should NOT Do
- Hold positions indefinitely ("hope" is not a strategy)
- Tie up most of account in 1-2 stagnant positions
- Treat evaluation capital like normal trading capital (it's constrained)
- Enter just because buying power is available
- Continue trading after daily loss limit hit

### Main KPI
**Probability of passing evaluation without violating drawdown**
- Target: $1,500 profit (hard target)
- Constraint: $1,000 max trailing drawdown (hard limit)
- Secondary: Win rate, expectancy, hold time

### Universe
```
Futures: MES, MNQ, MYM, M2K (micro contracts, lower risk during evaluation)
Crypto: BTC/USD, ETH/USD, SOL/USD, ADA/USD, DOGE/USD (via Alpaca)
Commodities: GLD, USO, SLV (precious metals, energy)
```

### Entry Mandate
All of:
1. RSI(14) < 30 (oversold)
2. SMA5 trend confirmed (bullish for long entries)
3. Buying power >= $150 buffer
4. Position size respects $50 minimum
5. Open positions < 2 (1.0x scale)
6. Total notional < 50% of equity
7. No stale market data (bars updated within 2 minutes)

### Exit Mandate
**Take Profit:**
- Tier 1: Exit 1/3 at 50% of profit target
- Tier 2: Exit 1/3 at 100% of profit target
- Tier 3: Exit 1/3 at 150% of profit target

**Cut Loss:**
- Stop loss: 0.3% (baseline), 0.2% (at 1.5x+ scale)
- Aggressive exit: 0.5% loss triggers immediate full exit
- Daily loss limit: $10 per day (stops all new entries)

**Time Out:**
- Position held > 4 hours → evaluate for exit
- Stale signal: RSI/trend changes → reassess

**Abandon:**
- No fill after 30 seconds → cancel and skip
- API error 3+ times → log and halt symbol

### Capital Mandate
```
Total account: ~$980
Locked reserve: $150 (hard buffer)
Deployable: ~$830
Max per position: $240 (2 positions @ $120 each)
Max total notional: 50% of $980 = $490
Max positions: 2 (at 1.0x scale)
Max loss per day: $10
Emergency brake: At $100 BP, halt all new entries
```

### Kill Conditions
Stop trading entirely if:
1. ❌ Daily loss limit hit ($10 in real losses)
2. ❌ Buying power below $100 (emergency threshold)
3. ❌ Equity drops below $800 (evaluation failure imminent)
4. ❌ API connectivity lost (can't verify positions/BP)
5. ❌ Database error (can't log trades)
6. ❌ Market data stale >5 minutes (can't trust RSI/trend)
7. ❌ Unexpected position appears (reconciliation failed)
8. ❌ Order rejection 5+ times (systematic problem)

### Success Definition
✅ Reaches $1,500 profit
✅ Never violates $1,000 drawdown
✅ Win rate >= 40% (trading quality, not just luck)
✅ Expectancy > 0 (winners bigger than losers)
✅ Avg hold time < 2 hours (capital available for next opportunity)

---

## Bot 2: Coinbase Crypto Bot

**Primary Mandate:** Short-term crypto opportunity capture

### What It Should Do
- Scan all approved crypto pairs continuously
- Enter high-confluence RSI + volume setups
- Take profits quickly (tight targets: 1-3% per trade)
- Cut losers fast (tight stops: 0.5-1.0%)
- Account for fees ($0.05-0.10 per trade) in P&L
- Repeat 3-5 times per day if opportunities available

### What It Should NOT Do
- Hold indefinitely ("hodl" is for long-term investors, not this bot)
- Average down into losing positions
- Enter on low volume (easy prey for whipsaws)
- Ignore fees (they matter on $50 positions)
- Trade just because cash is available

### Main KPI
**Net P&L + Expectancy**
- Target: Consistent small wins (0.5-1% per trade)
- Secondary: Win rate, # of trades, avg hold time
- Constraint: Max 5% loss per day

### Universe
```
Approved pairs: BTC/USD, ETH/USD, SOL/USD, ADA/USD, DOGE/USD, XRP/USD, LINK/USD, AVAX/USD
Additional: NEAR/USD, MATIC/USD, ARB/USD, OP/USD, APT/USD, SEI/USD, SUI/USD
Restriction: Skip if volume < 10M (24h), bid-ask spread > 0.2%
```

### Entry Mandate
All of:
1. RSI(14) < 35 (oversold, not just 30)
2. Volume ratio > 1.5x (higher than 5-bar average)
3. Buying power >= $150 buffer
4. Position size >= $50, <= $240
5. Open positions < 2
6. Fee impact acceptable (fee won't exceed 50% of expected win)
7. Bid-ask spread < 0.1% (liquidity check)

### Exit Mandate
**Take Profit:**
- Target: +1% to +3% (depends on volatility)
- Scale out: Exit 50% at +1%, remainder at +2-3%

**Cut Loss:**
- Stop loss: 0.5% (tight for crypto volatility)
- Aggressive exit: Any loss > 1% → full exit
- Daily loss limit: 5% of session capital

**Time Out:**
- Position held > 30 minutes → evaluate
- No movement in 15 min → exit (capital available elsewhere)

**Abandon:**
- No fill in 15 seconds → cancel
- Spread widens > 0.15% after entry → exit immediately (slippage risk)

### Capital Mandate
```
Total account: Separate Coinbase account
Max per position: $240
Max positions: 2
Max total notional: 50% of account
Min position: $50
Emergency brake: At $100 free cash, halt new entries
Daily loss limit: 5% of session capital
```

### Kill Conditions
Stop trading entirely if:
1. ❌ Daily loss limit hit (5% in real losses)
2. ❌ Free cash below $100
3. ❌ Volume dries up (< 5M on core pair)
4. ❌ Spread widens permanently (> 0.2%)
5. ❌ API down or rate limited
6. ❌ Database error (can't log trades)
7. ❌ Stale price data (> 2 minutes old)
8. ❌ Order rejection 3+ times

### Success Definition
✅ Win rate >= 50% (crypto is noisy, this is good)
✅ Expectancy > 0 (average trade is profitable)
✅ Avg hold time < 20 minutes (quick capital turnover)
✅ Gross P&L > fees (basic profitability)
✅ No single loss > 2% of account

---

## Bot 3: Alpaca Stock/ETF Bot

**Primary Mandate:** Short-term stock/ETF trading growth

### What It Should Do
- Scan approved liquid stocks/ETFs
- Enter when price + volume + trend align
- Target 2-5% wins per trade
- Maintain capital availability (don't lock up in stagnant positions)
- Build consistent small profits

### What It Should NOT Do
- Hold indefinitely ("position investing" is different strategy)
- Enter on low volume (illiquid = slippage)
- Ignore commission impact
- Treat like a long-term account

### Main KPI
**Capital Turnover + Net P&L**
- Target: 3-5 trades per day (if opportunities exist)
- Target: 1-3% win per trade
- Secondary: Win rate, avg hold time

### Universe
```
Approved: SPY, QQQ, IWM, VTI, VOO, VGT, XLK, XLE, XLF
Excluded: TSLA, GME, highly volatile single stocks (too risky for small positions)
Restriction: Only trade if spread < 0.05%, volume > 1M shares
```

### Entry Mandate
All of:
1. RSI(14) 25-35 OR 65-75 (oversold long, overbought short)
2. Volume > 1.5x 5-bar average
3. Price break of recent support/resistance
4. Buying power >= $100
5. Position size >= $30, <= $200
6. Max 3 open positions

### Exit Mandate
**Take Profit:** +2% to +5%
**Cut Loss:** -1.5%
**Time Out:** > 2 hours held → exit
**Avg hold time target:** 45 minutes

### Capital Mandate
```
Max per position: $200
Max positions: 3
Max total notional: 60% (slightly higher than crypto, lower volatility)
Min position: $30
```

### Kill Conditions
1. ❌ Daily loss limit hit (3%)
2. ❌ Free cash below $50
3. ❌ Market closes (can't trade after hours)
4. ❌ API down

---

## Bot 4: Analytics & Monitoring (Non-Trading)

**Primary Mandate:** Independent oversight of all three trading bots

### What It Should Do
- Monitor closed trades in real-time
- Alert on mandate violations
- Track P&L, win rate, expectancy
- Flag stale data (no trades > 2 hours)
- Detect API failures
- Report position reconciliation errors
- Calculate rolling metrics (hourly, daily, weekly)

### What It Should NOT Do
- Place orders (read-only)
- Adjust bot parameters
- Exit positions (that's the bot's job)
- Make trading decisions

### Metrics to Track
```
Per-bot:
- Trades today/week/month
- Win rate (%, count)
- Gross P&L, Net P&L
- Avg winner / avg loser
- Expectancy (per trade)
- Avg hold time
- Capital deployed
- Capital available
- Risk exposure (%)
- Daily loss YTD
- Mandate compliance (%)

System-wide:
- Total P&L
- Total trades
- Combined win rate
- Total capital deployed
- API health status
- Database status
- Data freshness
```

### Kill Conditions for Monitoring
Alert (don't auto-stop) if:
1. ⚠️ No trades in 2 hours (signal stale?)
2. ⚠️ P&L negative > 5% daily (evaluate)
3. ⚠️ API error rate > 5%
4. ⚠️ Database query time > 5 seconds
5. ⚠️ Position count mismatch (bot says 2, broker says 1)
6. ⚠️ Buying power discrepancy > $50

---

## Dashboard Should Show

For each bot:
```
BOT NAME: [Mandate]
─────────────────────────
Status: ACTIVE | HALTED | ERROR
Days active: X

CAPITAL
  Deployed: $X (X%)
  Available: $X
  Max allowed: $X (Y%)

PERFORMANCE
  Trades today: X (W wins, L losses)
  Win rate: X%
  Avg winner: $X
  Avg loser: $X
  Expectancy: $X
  Gross P&L: $X
  Fees: $X
  Net P&L: $X

COMPLIANCE
  Mandate compliance: X%
  Daily loss limit: $X / $X
  Max drawdown: $X / $X
  Risk level: SAFE | WARNING | CRITICAL

SIGNALS
  Last signal: X min ago
  Last trade: X min ago
  Data freshness: LIVE | STALE | ERROR
```

---

## Implementation Checklist

- [ ] Define mandate for each bot in code comments
- [ ] Hard-code universe limits (no trading outside approved list)
- [ ] Enforce entry conditions (don't override on "just because")
- [ ] Enforce capital limits (buffer, position size, max notional)
- [ ] Implement kill conditions (halt bot if triggered)
- [ ] Log every decision: why entry? why exit? why skip?
- [ ] Dashboard shows mandate + compliance, not just P&L
- [ ] Weekly review: Did bot stay in its lane?
- [ ] Alert system for mandate violations
- [ ] Post-mortem on any kill condition trigger

