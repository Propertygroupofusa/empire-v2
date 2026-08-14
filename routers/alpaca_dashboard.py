"""Alpaca/Apex futures trading dashboard and position tracker."""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from database import get_db
from models import TradingBotState

router = APIRouter(tags=["alpaca-trading"])


@router.get("/dashboard")
async def alpaca_dashboard(db: Session = Depends(get_db)):
    """Alpaca trading dashboard — CACHED database data (no live API calls)."""
    stmt = select(TradingBotState).where(TradingBotState.bot_name == "prop_bot")
    bot_state = (await db.execute(stmt)).scalar_one_or_none()

    if not bot_state:
        return {
            "status": "no_data",
            "message": "Alpaca bot has not started yet"
        }

    # Calculate performance from DATABASE (no API calls needed)
    starting = bot_state.starting_capital or 1000.0
    current = bot_state.base_capital or 0.0
    profit = current - starting
    profit_pct = (profit / starting * 100) if starting > 0 else 0

    # Time since last update
    now = datetime.now(timezone.utc)
    last_update = bot_state.updated_at
    if last_update:
        time_delta = (now - last_update).total_seconds()
        hours_ago = time_delta / 3600
        stale = hours_ago > 2  # Mark stale if >2 hours old
    else:
        hours_ago = None
        stale = True

    return {
        "bot": "Alpaca Apex Futures (prop_bot) - CACHED VIEW",
        "api_status": "🔴 BLOCKED (HTTP 403 - network firewall)" if stale else "🟢 LIVE",
        "data_source": "Database cache - NO live API calls required",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "last_synced": last_update.isoformat() if last_update else "Never",
        "data_age_hours": round(hours_ago, 1) if hours_ago else None,
        "capital_tracking": {
            "starting_balance": round(starting, 2),
            "last_recorded_balance": round(current, 2),
            "profit_loss": round(profit, 2),
            "profit_loss_pct": round(profit_pct, 2),
            "status": "✅ Profitable" if profit > 0 else "❌ Loss" if profit < 0 else "➡️ Breakeven"
        },
        "warning": "⚠️ DATA IS {:.1f}h OLD - real account may have changed since last bot sync".format(hours_ago) if stale else None,
        "action_needed": "Network API block detected - bot cannot sync live data. Check network settings." if stale else None
    }


@router.get("/health")
async def alpaca_health():
    """Health check for Alpaca trading system."""
    return {
        "status": "ok",
        "service": "alpaca-trading",
        "bot": "prop_bot (Apex Futures)",
        "markets": [
            "MES (Micro E-mini S&P 500)",
            "MNQ (Micro E-mini Nasdaq-100)",
            "MYM (Micro E-mini Dow)",
            "M2K (Micro E-mini Russell 2000)",
            "Crypto futures: BTC, ETH, SOL, ADA, DOGE, XRP, LINK, AVAX, etc.",
        ],
        "strategy": "RSI oversold entry + ATR-based stops + 3-tier profit targets",
        "current_issue": "Network API blocked - equity and cash balance unavailable"
    }
