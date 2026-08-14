# Bot Mandate Integration Plan

This plan bridges the mandate framework (bot_mandates.py) with the actual trading code.

---

## Phase 1: Enforcement (Required Before Bot Runs)

### 1.1 Universe Enforcement
**File: prop_bot.py**
```python
# At start of run_prop_cycle():
from bot_mandates import APEX_MANDATE

ALLOWED_SYMBOLS = (
    APEX_MANDATE["universe"]["futures"] +
    APEX_MANDATE["universe"]["crypto"] +
    APEX_MANDATE["universe"]["commodities"]
)

# Before attempting any trade:
if symbol not in ALLOWED_SYMBOLS:
    log.warning(f"[MANDATE] {symbol} not in approved universe - skipping")
    continue
```

### 1.2 Entry Mandate Enforcement
**File: prop_bot.py, in try_open() function**
```python
from bot_mandates import validate_entry

is_valid, reason = validate_entry(
    bot_name="prop_bot",
    symbol=contract,
    rsi=rsi,
    volume_ratio=get_volume_ratio(...),
    buying_power=buying_power,
    open_positions=len(open_prop_positions),
    total_notional=sum(p["qty"]*p["entry"] for p in open_prop_positions.values()),
    equity=equity
)

if not is_valid:
    log.info(f"[MANDATE] Entry blocked: {reason}")
    return False

# Only if all mandate checks pass, proceed to open_position()
```

### 1.3 Capital Limit Enforcement
**File: prop_bot.py, in size_position() and check_margin_safety()**
```python
# Already done, but verify against mandate:
from bot_mandates import APEX_MANDATE

capital = APEX_MANDATE["capital"]
assert MIN_BUYING_POWER_BUFFER == capital["locked_reserve"]
assert MIN_POSITION_NOTIONAL == capital["min_position_size"]
assert BASE_MAX_POSITIONS == capital["max_open_positions"]
```

### 1.4 Kill Condition Monitoring
**File: prop_bot.py, new function**
```python
def check_kill_conditions():
    """Halt trading if any kill condition is triggered"""
    from bot_mandates import APEX_MANDATE
    
    # Check each kill condition
    if daily_pnl < -APEX_MANDATE["capital"]["max_daily_loss"]:
        log.critical(f"[KILL] Daily loss limit hit: ${daily_pnl:.2f}")
        halt_all_trading()
        return True
    
    if buying_power < APEX_MANDATE["capital"]["critical_buying_power"]:
        log.critical(f"[KILL] Buying power critically low: ${buying_power:.2f}")
        halt_all_trading()
        return True
    
    # ... check remaining 6 conditions
    return False

# Call at top of run_prop_cycle():
if check_kill_conditions():
    return
```

### 1.5 Exit Mandate Enforcement
**File: prop_bot.py, in close_position() logic**
```python
# Already implemented with tiered exits, but add mandate check:
from bot_mandates import APEX_MANDATE

exit_config = APEX_MANDATE["exit"]

# Stop loss
stop_price = entry * (1 - exit_config["stop_loss_pct"])
if current_price <= stop_price:
    await close_position(..., reason="STOP_LOSS")
    return

# Profit tiers
profit_target = ...
tiers = exit_config["profit_tiers"]
for tier_level, tier_pct in zip(tiers, [1/3, 1/3, 1/3]):
    if unrealized_pnl >= profit_target * tier_level:
        exit_qty = position["qty"] * tier_pct
        await close_position(..., qty=exit_qty, reason=f"TIER_{tier_level}")
```

---

## Phase 2: Logging & Audit (Track Mandate Compliance)

### 2.1 Decision Logging
**File: prop_bot.py, new logging function**
```python
def log_decision(bot_name, symbol, decision, reason, mandate_check=None):
    """Log every trading decision against mandate"""
    from datetime import datetime
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "bot": bot_name,
        "symbol": symbol,
        "decision": decision,  # "ENTER" / "EXIT" / "SKIP" / "HALT"
        "reason": reason,  # Specific reason
        "mandate_check": mandate_check,  # Which mandate rule applied
        "position": open_prop_positions.get(symbol),
        "metrics": {
            "rsi": rsi,
            "equity": equity,
            "buying_power": buying_power,
            "open_positions": len(open_prop_positions),
        }
    }
    # Log to database + stdout
    log.info(f"[DECISION] {decision} {symbol}: {reason}")
```

### 2.2 Compliance Tracking
**File: routers/monitoring.py (new endpoint)**
```python
@router.get("/mandates/compliance/{bot_name}")
async def mandate_compliance(bot_name: str, db: Session = Depends(get_db)):
    """Show bot mandate compliance metrics"""
    mandate = get_bot_mandate(bot_name)
    closed_trades = db.query(ClosedTrade).filter(
        ClosedTrade.bot == bot_name,
        ClosedTrade.closed_at >= datetime.utcnow() - timedelta(days=1)
    ).all()
    
    return {
        "bot": bot_name,
        "mandate": mandate["description"],
        "compliance": {
            "trades_in_universe": sum(1 for t in closed_trades if t.symbol in mandate_universe),
            "trades_violating_entry": sum(1 for t in closed_trades if t.entry_violated),
            "trades_obeying_exit": sum(1 for t in closed_trades if t.exit_obeyed),
            "capital_respects_limits": check_capital_compliance(closed_trades, mandate),
            "no_kill_conditions": check_kill_conditions_clean(),
        }
    }
```

---

## Phase 3: Dashboard Upgrade

### 3.1 Mandate-Based Dashboard
**File: routers/bot_status.py (new endpoint)**
```python
@router.get("/bots/dashboard/{bot_name}")
async def bot_mandate_dashboard(bot_name: str, db: Session = Depends(get_db)):
    """Show bot status against its mandate"""
    mandate = get_bot_mandate(bot_name)
    trades_today = db.query(ClosedTrade).filter(
        ClosedTrade.bot == bot_name,
        ClosedTrade.closed_at >= datetime.utcnow() - timedelta(hours=24)
    ).all()
    
    # Calculate metrics
    win_count = sum(1 for t in trades_today if t.pnl > 0)
    win_rate = win_count / len(trades_today) if trades_today else 0
    
    return {
        "bot_name": bot_name,
        "mandate": {
            "role": mandate["role"].value,
            "description": mandate["description"],
            "primary_kpi": mandate["kpi"]["primary"],
        },
        "capital": {
            "deployed": calculate_deployed(),
            "deployed_pct": calculate_deployed() / mandate["capital"]["total_account"] * 100,
            "available": calculate_available(),
            "max_allowed": mandate["capital"]["total_account"],
        },
        "performance": {
            "trades_today": len(trades_today),
            "wins": win_count,
            "losses": len(trades_today) - win_count,
            "win_rate": f"{win_rate*100:.1f}%",
            "avg_winner": calculate_avg_winner(trades_today),
            "avg_loser": calculate_avg_loser(trades_today),
            "expectancy": calculate_expectancy(trades_today),
            "gross_pnl": sum(t.pnl for t in trades_today),
            "fees": calculate_fees(trades_today),
            "net_pnl": sum(t.pnl for t in trades_today) - calculate_fees(trades_today),
        },
        "compliance": {
            "mandate_adherence": calculate_mandate_score(),
            "daily_loss_used": calculate_daily_loss(),
            "daily_loss_limit": mandate["capital"]["max_daily_loss"],
            "risk_level": "SAFE" if calculate_daily_loss() < mandate["capital"]["max_daily_loss"] * 0.5 else "WARNING",
            "mandate_status": "COMPLIANT" if calculate_mandate_score() >= 95 else "DEVIATION",
        },
        "signals": {
            "last_signal_age_min": calculate_signal_age(),
            "last_trade_age_min": calculate_trade_age(),
            "data_freshness": "LIVE" if check_data_fresh() else "STALE",
        }
    }
```

---

## Phase 4: Alerts & Violations

### 4.1 Mandate Violation Alerts
**File: routers/alerts.py (new endpoint)**
```python
@router.get("/mandates/violations")
async def mandate_violations(db: Session = Depends(get_db)):
    """List any mandate violations detected"""
    violations = []
    
    for bot_name, mandate in ALL_MANDATES.items():
        # Check universe violations
        off_universe = db.query(ClosedTrade).filter(
            ClosedTrade.bot == bot_name,
            ~ClosedTrade.symbol.in_(mandate["universe"])
        ).all()
        if off_universe:
            violations.append({
                "bot": bot_name,
                "type": "UNIVERSE_VIOLATION",
                "count": len(off_universe),
                "trades": [t.symbol for t in off_universe],
            })
        
        # Check kill condition violations
        # ... similar checks for other conditions
    
    return {"violations": violations}
```

---

## Checklist: Before Bots Trade Real Money

- [ ] **Universe enforcement** - Bots reject symbols outside approved list
- [ ] **Entry validation** - All 5+ entry conditions must pass
- [ ] **Capital limits** - Position size, max notional enforced
- [ ] **Kill conditions** - 7+ hard stops implemented and logged
- [ ] **Exit rules** - Tiered exits, stops, timeouts working
- [ ] **Decision logging** - Every decision logged with mandate reference
- [ ] **Compliance dashboard** - Shows mandate adherence, not just P&L
- [ ] **Violation detection** - Alerts on any mandate breach
- [ ] **Database audit trail** - Closed trades have all required fields
- [ ] **Manual review process** - Weekly: Are bots staying in their lane?

---

## Timeline

**Immediate (before first trade):**
- Implement universe enforcement
- Add entry mandate validation
- Activate kill conditions

**Within 24 hours:**
- Deploy dashboard showing mandate compliance
- Set up violation alerts
- Begin collecting decision logs

**Within 1 week:**
- Full audit trail review
- Calculate mandate compliance score
- Weekly mandate review meeting

---

## Success Criteria

✅ Each bot trades ONLY symbols in its universe
✅ Entry decisions logged with mandate reference
✅ No position violates capital limits
✅ Kill conditions activate before catastrophic loss
✅ Dashboard shows mandate compliance >= 95%
✅ No violations in first week of trading
✅ Weekly review shows each bot staying in its lane
✅ Decision logs tell us WHY each trade happened (not just that it happened)

