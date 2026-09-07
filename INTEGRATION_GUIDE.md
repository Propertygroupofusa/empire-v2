# Integration Guide: Wiring Your Trading System

## For Claude.ai Project Knowledge (Immediate)

**Upload these 5 files to your "Help Me With Building A Trading System" project:**

### 1. Copy MASTER_SYSTEM_PROMPT.md
```
From: /home/user/empire-v2/MASTER_SYSTEM_PROMPT.md
To: Claude.ai Project Knowledge
Purpose: System prompt reference + trading rulebook
```

### 2. Copy bot_learning_engine.py
```
From: /home/user/empire-v2/bot_learning_engine.py
To: Claude.ai Project Knowledge
Purpose: Memory engine implementation
```

### 3. Copy auto_optimize_grid_bot.py
```
From: /home/user/empire-v2/auto_optimize_grid_bot.py
To: Claude.ai Project Knowledge
Purpose: Weekly optimization logic
```

### 4. Copy bot_learnings.json structure
```
From: /home/user/empire-v2/bot_learnings.json (current)
To: Claude.ai Project Knowledge
Purpose: Shows memory format and what gets logged
```

### 5. Add PROJECT_README.md (provided in scratchpad)
```
File: /tmp/claude-0/-home-user-Delfina/8bc5b730-e02d-506c-9c98-0f6519adc72d/scratchpad/PROJECT_README.md
To: Claude.ai Project Knowledge
Purpose: Complete system overview and workflow
```

---

## Three Deployment Paths

### Path A: AI-Assisted Trading (Requires API Integration)
**Goal:** Wire bot_learning_engine into your trading API so AI makes trade decisions

```python
# Pseudocode: What needs to happen
from bot_learning_engine import BotLearningEngine
from trading_api import CoinbaseAdvancedTrade

engine = BotLearningEngine()

# Morning: Check memory
memory_check = engine.check_before_trade("BTC-USD", "grid 1% spacing")
if not memory_check["safe"]:
    print(f"Skip trade: {memory_check['reason']}")
    exit()

# Execute trade
trade = CoinbaseAdvancedTrade(coin="BTC-USD", size=100)
trade_id = trade.enter()
trade.monitor()

# Evening: Log result
engine.log_trade_result(
    trade_id=trade_id,
    coin="BTC-USD",
    entry=trade.entry_price,
    exit=trade.exit_price,
    profit=trade.pnl,
    status="closed_profit" if trade.pnl > 0 else "closed_loss"
)

# Report
print(engine.print_memory_summary())
```

**Requirements:**
- Trading API (Coinbase, Interactive Brokers, etc.)
- Python environment to run bot_learning_engine.py
- Daily scheduler (cron, Windows Task Scheduler, etc.)

---

### Path B: Manual Decision Framework (Using System Prompt)
**Goal:** Use MASTER_SYSTEM_PROMPT as your personal trading framework

```
Daily workflow:
1. Morning: Read bot_learnings.json summary
2. During day: Execute trades manually, follow discipline rules
3. Evening: Generate daily report using template from MASTER_SYSTEM_PROMPT
4. Log all closed trades to bot_learnings.json manually
5. Sunday: Run auto_optimize_grid_bot.py, get scaling recommendations
```

**Requirements:**
- Manual logging discipline (every trade logged)
- Weekly review ritual (Sunday optimization check)
- Access to bot_learnings.json file

---

### Path C: Hybrid (Recommended for Now)
**Goal:** Grid Bot runs on algorithm, you use system prompt for macro decisions

```
Current state (YOUR SETUP):
┌─────────────────────────────────────┐
│ Grid Bot (algorithmic, autonomous)  │
│ • Runs on exchange dashboard        │
│ • 7 branches (BTC, ETH, SOL, etc.)  │
│ • Auto-enters/exits based on grid   │
│ • Real capital: $1,080.60           │
└─────────────────────────────────────┘
                ↓
         Log trades to:
        bot_learnings.json
                ↓
        Weekly review using:
    MASTER_SYSTEM_PROMPT + 
   auto_optimize_grid_bot.py
                ↓
    Macro decisions only:
    • Scale capital? (if > 55% win rate)
    • Add new branches? (if > $50 profit)
    • Stop losers? (if < 50% win rate)
    • Rebalance? (if > 3% performance gap)
```

**Recommended actions NOW:**
1. Continue running Grid Bot (no changes)
2. Set up bot_learnings.json logging (manually or auto)
3. Review learnings every Sunday with auto_optimize_grid_bot.py
4. Make scaling decisions based on reports
5. Wait 1 week (Sept 14) to review 50+ trades before any changes

---

## Setup Steps (Immediate)

### Step 1: Add to Claude.ai Project Knowledge
1. Go to your Claude.ai project: "Help Me With Building A Trading System"
2. Click "Add Content" in Project Knowledge section
3. Upload these 5 files:
   - MASTER_SYSTEM_PROMPT.md
   - bot_learning_engine.py
   - auto_optimize_grid_bot.py
   - bot_learnings.json (current state)
   - PROJECT_README.md

This gives any Claude instance full context for your trading work.

### Step 2: Schedule 1-Week Check-in
- **Date:** Sept 14, 2026 (1 week from now)
- **Goal:** Review 50+ trades, confirm win rate trajectory
- **Action items:**
  - Count total trades closed
  - Calculate win rate (wins / total trades)
  - Compare to backtest target (59%)
  - Decide: Scale? Pause? Modify?

### Step 3: Manual Trade Logging (Daily)
If not auto-logged, manually add to bot_learnings.json:

```json
{
  "trade_id": "2026-09-07-btc-001",
  "coin": "BTC-USD",
  "entry_price": 100.50,
  "exit_price": 101.00,
  "profit": 50.00,
  "status": "closed_profit",
  "lesson": "BTC responds to 1% grid spacing - keep doing this"
}
```

### Step 4: Run Weekly Optimization (Sunday)
```bash
python auto_optimize_grid_bot.py
```

This generates:
- Win rate by strategy
- Profit factor
- Scaling recommendation
- Actions to take

---

## Decision Framework: When to Scale

Use this framework from MASTER_SYSTEM_PROMPT + auto_optimize_grid_bot.py:

```
IF win_rate >= 62% AND total_pnl > $50:
  → SCALE AGGRESSIVELY
  → Add 2-3 branches
  → Increase capital per branch by 50%

ELSE IF win_rate >= 58% AND total_pnl > $25:
  → SCALE MODERATELY
  → Add 1 new branch
  → Increase capital per branch by 25%

ELSE IF win_rate >= 55%:
  → SCALE SLOWLY
  → Add branches when 100 more trades complete
  → Hold capital steady

ELSE IF win_rate >= 52%:
  → INVESTIGATE
  → Wait for 200+ trades data
  → Do NOT scale

ELSE:
  → PAUSE SCALING
  → Fix issues first
  → Review what's broken
```

---

## File Locations

**Files to reference:**
- Master Prompt: `/home/user/empire-v2/MASTER_SYSTEM_PROMPT.md`
- Learning Engine: `/home/user/empire-v2/bot_learning_engine.py`
- Optimizer: `/home/user/empire-v2/auto_optimize_grid_bot.py`
- Memory Store: `/home/user/empire-v2/bot_learnings.json`
- Project README: `/tmp/claude-0/-home-user-Delfina/8bc5b730-e02d-506c-9c98-0f6519adc72d/scratchpad/PROJECT_README.md`

**Git tracking:**
- Code files (*.py, *.md): Commit to git (done Sept 7)
- Data files (*.json): Add to .gitignore (memory grows daily)

---

## Next Steps

**Before 1-week check-in (Sept 14):**
- [ ] Upload 5 files to Claude.ai project knowledge
- [ ] Continue running Grid Bot as-is (no changes)
- [ ] Manually log closed trades to bot_learnings.json (or set up auto-logging)
- [ ] Track daily: total trades, P&L, win rate

**At 1-week check-in (Sept 14):**
- [ ] Run: `python auto_optimize_grid_bot.py`
- [ ] Review: Win rate target is 59% (backtest proven)
- [ ] Decide: Scale? Add branches? Continue?

**Next scaling opportunity (Week 2):**
- IF win rate > 55%: Add ETH and SOL branches ($250 each)
- IF win rate < 55%: Hold steady, collect more data

---

**Version:** 1.0
**Status:** Ready to implement
**Last updated:** 2026-09-07
