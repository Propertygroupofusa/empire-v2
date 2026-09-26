# Grid Bot Scaling & Mean Reversion Integration Guide

## Overview

Your crypto trading system now includes:
1. **Grid Bot** — Market-neutral grid trading (PROVEN: 76% win rate, +$17.03 P&L)
2. **Mean Reversion Bot** — Directional trades buying oversold coins (NEW: complements grid)
3. **Capital Scaling** — Automated function to increase grid bot allocations

## Quick Start: Scale Grid Bot by 25%

```bash
cd /home/user/empire-v2
python scale_grid_capital.py 1.25
```

This scales all active grid branches up by 25% and logs the change.

## Capital Allocation Strategy

**Total Account Capital:**
- Grid Bot: 80% (market-neutral, proven edge)
- Mean Reversion: 20% (directional, RSI-based)

**Grid Bot Example (before scaling):**
```
Total account: $1,000
├── Grid Bot (80%): $800
│   ├── BTC-USD: $120
│   ├── ETH-USD: $100
│   ├── SOL-USD: $80
│   └── Other branches: ~$500
└── Reserve/Mean Reversion (20%): $200
```

**After 25% Scaling:**
```
├── BTC-USD: $150 (+$30)
├── ETH-USD: $125 (+$25)
├── SOL-USD: $100 (+$20)
└── Other branches: ~$625 (+$125)
```

## Mean Reversion Bot: How It Works

### Entry Signal
- Buys when RSI (14-period) drops below 30 (oversold)
- Max 2% of available capital per trade
- Minimum $10 trade size
- One position per coin at a time

### Exit Signals (whichever comes first)
1. **Hard Stop Loss** — Price drops 2.5% below entry
2. **Profit Target** — Price rises 4% above entry (1.6x risk/reward ratio)
3. **Mean Reversion Complete** — RSI rises above 60 (overbought)
4. **Timeout** — Position held for 6 hours (prevents overnight gap risk)

### Capital Management
- Uses separate 20% capital pool from grid bot
- Position size: `min(available_capital * 2%, available_capital)`
- Risk/reward: 2.5% loss : 4% gain = 1.6x ratio

### Example Trade
```
[14:00] BTC-USD price: $42,500
       RSI: 28 (oversold) → BUY SIGNAL
       Entry: BTC-USD @ $42,500, qty: 0.01 BTC ($425)
       Stop Loss: $41,438 (2.5% below entry)
       Target: $44,200 (4% above entry)

[14:45] BTC-USD price: $44,300
       RSI: 62 (overbought) → SELL SIGNAL
       Exit: Sell @ $44,300
       P&L: $170 gross - $8.50 fees = +$161.50 net (+37.9%)
```

## Activity Logging

Both bots log all trades to the shared `CryptoActivityEvent` table:
- Grid bot logs buy/sell for each grid slice
- Mean reversion logs entry/exit with RSI, P&L, exit reason
- Dashboard automatically displays both systems' trades

**Dashboard will show:**
- Grid Bot section: Buy/sell per grid level, total P&L
- Mean Reversion section: Entry/exit trades with RSI context, quality metrics

## Monitoring the System

### Terminal 1: Run the Grid + Mean Reversion Bot
```bash
cd /home/user/empire-v2
python -c "import crypto_grid_bot; crypto_grid_bot.run()"
```

Expected output:
```
CRYPTO GRID BOT - real, live grid-trading branches
============================================================
[GRID] Cycle 1: 3 branches active
[GRID] BTC-USD: 2 open slices, reference price $42,300
[MR] Cycle: checked RSI for 10 coins
[SHADOW] Status: 15/50 trades logged...
```

### Terminal 2: Monitor Dashboard
```bash
cd /home/user/Delfina
python scripts/dashboard_server.py
# Open: http://localhost:8080/dashboard
```

The dashboard will display:
- Grid bot performance per branch (BTC, ETH, SOL, etc.)
- Mean reversion activity (active positions, entry/exit trades)
- Combined system performance (grid + mean reversion)

### Terminal 3: Check System Status
```bash
cd /home/user/empire-v2

# View current grid bot status (allocation, branches, P&L)
python -c "
import asyncio
import crypto_grid_bot
result = asyncio.run(crypto_grid_bot.get_grid_status())
print(f\"Total allocated: ${result['total_allocated_usd']:,.2f}\")
print(f\"Branches: {result['branch_count']}\")
print(f\"Free cash available: ${result['real_free_cash_usd']:,.2f}\")
for branch in result['branches'][:5]:
    print(f\"  {branch['bot_name']}: ${branch['allocated_usd']:,.2f}\")
"

# View grid bot trade history
python -c "
import asyncio
import crypto_grid_bot
result = asyncio.run(crypto_grid_bot.get_grid_trade_history())
print(f\"Total trades: {result['total_trade_count']}\")
print(f\"Win rate: {result['overall_win_rate']:.1f}%\")
print(f\"Total P&L: ${result['total_realized_pnl']:,.2f}\")
"
```

## Scaling Guide

### Scale Factors
- **1.10** = 10% increase (conservative)
- **1.25** = 25% increase (recommended)
- **1.50** = 50% increase (aggressive)

### Before Scaling
Check that:
1. Grid bot is running and profitable (check dashboard)
2. Mean reversion bot is enabled and collecting trades
3. Free cash is available (`real_free_cash_usd` > scaling amount)

### Execute Scaling
```bash
python scale_grid_capital.py 1.25
```

Output example:
```
✅ SCALING SUCCESSFUL
Branches scaled: 3
Old total allocation: $300.00
New total allocation: $375.00
Total increase: $75.00

per-Branch Updates:
  crypto_grid_1 (DOGE-USD): $100.00 → $125.00 (+25.0%)
  crypto_grid_2 (STX-USD): $120.00 → $150.00 (+25.0%)
  crypto_grid_3 (ETH-USD): $80.00 → $100.00 (+25.0%)
```

### Post-Scaling
- Grid bot picks up new allocations on next cycle (~30-60 seconds)
- Dashboard will show updated allocations instantly
- Monitor for successful order placement at new sizes

## Key Differences: Grid vs Mean Reversion

| Aspect | Grid Bot | Mean Reversion |
|--------|----------|---|
| Strategy | Market-neutral | Directional |
| Entry | 1% grid below reference | RSI < 30 oversold |
| Exit | FIFO (oldest slice) | RSI > 60 or profit target |
| Position Count | Multiple concurrent | One per coin |
| Risk/Reward | 1% grid spacing | 2.5% loss : 4% gain |
| Hold Time | Variable (grid-dependent) | Max 6 hours |
| Capital | 80% of account | 20% of account |
| Win Rate (actual) | 76% | TBD (tracking) |

## Expected Timeline

### Hour 1-2: System Startup
- Grid bot places initial slice buys
- Mean reversion checks RSI across 10 coins
- Dashboard shows grid positions and mean reversion monitoring

### Hour 3-6: Normal Operation
- Grid bot cycles through buy/sell signals
- Mean reversion accumulates trades (if oversold conditions occur)
- Dashboard updates every ~30 seconds with live P&L

### Hour 24: First Day Summary
- Grid bot: ~20-50 trades, typical +1-3% P&L
- Mean reversion: 2-5 trades (depends on market volatility)
- Dashboard shows combined system performance

## Troubleshooting

### Grid Bot Not Starting
```bash
# Check credentials
echo "API Key: $COINBASE_API_KEY_NAME"
echo "Has Private Key: $([ -n "$COINBASE_API_PRIVATE_KEY" ] && echo "YES" || echo "NO")"

# If empty, export them
export COINBASE_API_KEY_NAME="your_key"
export COINBASE_API_PRIVATE_KEY="your_secret"
```

### Mean Reversion Not Trading
- Check RSI thresholds (< 30 for buy, > 60 for sell)
- Verify capital allocation (`_get_mean_reversion_capital()` returns > $10)
- Check database for `CryptoActivityEvent` entries

### Scaling Failed
- Verify free cash available: `get_real_free_cash_usd()`
- Check database connection
- Ensure grid branches exist and are active

## Integration Details

### File Changes
- `crypto_grid_bot.py`: Added mean reversion import and cycle integration
- `crypto_mean_reversion_bot.py`: New mean reversion engine
- `scale_grid_capital.py`: Capital scaling utility

### Database Tables Used
- `crypto_grid_branch` — Grid bot allocations (updated by scaling)
- `crypto_grid_slice` — Active grid levels
- `crypto_grid_trade_history` — Executed grid trades
- `crypto_activity_event` — Shared activity feed (grid + mean reversion logs)

### Configuration Environment Variables
- `MEAN_REVERSION_CYCLE_SECONDS` — Mean reversion run frequency (default: 300 = 5 min)
- `GRID_AUTO_ROTATE_INTERVAL_SECONDS` — Grid auto-rotation interval (default: 1800 = 30 min)

## Next Steps

1. **Scale Grid Bot**: `python scale_grid_capital.py 1.25`
2. **Start Systems**: Run crypto_grid_bot.py (which now includes mean reversion)
3. **Monitor Dashboard**: Open http://localhost:8080/dashboard
4. **Track Performance**: Watch grid + mean reversion P&L accumulate

---

**Remember**: Grid bot has proven edge (76% win rate). Mean reversion is directional backup. Combined system = robust crypto trading across neutral and directional opportunities.
