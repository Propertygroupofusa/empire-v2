# Dynamic Capital Rotation Strategy

## Capital: $451.50 USD
## Deployment: Real-time rotation to highest-profit pairs

### Strategy Rules

**1. Entry Allocation**
- Fixed $250 per entry (position sizing from Fix #1)
- Allows 1-2 concurrent positions from $451.50 pool
- Exits recycle capital back to pool immediately

**2. Pair Performance Tracking**
- Monitor each of 28 pairs for:
  - Win rate (wins/total_exits)
  - Avg profit per win
  - Avg loss per loss
  - Risk-reward ratio
  - Sharpe ratio (profit consistency)

**3. Capital Rotation Rules**
- PRIORITY 1: Scan all 28 pairs for RSI recovery setup
- PRIORITY 2: Enter pairs with highest historical win rate first
- PRIORITY 3: Skip pairs with <40% win rate in last 10 trades
- PRIORITY 4: Scale position size on consecutive wins (up to 2x)
- PRIORITY 5: Exit immediately on RSI divergence (don't wait for target)

**4. Profit Locking**
- Tier 1 (3% profit): Close 50%, let rest run
- Tier 2 (5% profit): Close 75%, let 25% run
- Tier 3 (10% profit): Close 100%, lock in gains
- Trailing stop: 2 x ATR below entry (auto-exit on pullback)

**5. Loss Management**
- Hard stop: 2% loss (via ATR stop)
- Soft stop: Exit on RSI exit signal
- Never hold losers > 1 hour
- No revenge trading after 2 consecutive losses

**6. Capital Rebalancing**
- Every 10 exits: Analyze top 5 performing pairs
- Allocate 60% capital to top 2 pairs
- Allocate 40% to diversified rotation
- Rebalance weekly based on performance

### Expected Performance
- Base case: 55% win rate (from historical data)
- With rotation: 65%+ win rate (filter to high-conviction setups)
- Target ROI: +30-50% monthly on $451.50
- Compounding: Reinvest all profits (principal + gains)

### Monitoring
- Real-time P&L per pair
- Daily performance report
- Weekly capital rebalancing analysis
- Monthly backtest vs baseline

