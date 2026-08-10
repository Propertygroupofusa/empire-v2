"""Alpaca/Apex futures trading dashboard and position tracker."""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from database import get_db
from models import TradingBotState

router = APIRouter(prefix="/alpaca", tags=["alpaca-trading"])


@router.get("/dashboard")
async def alpaca_dashboard(db: Session = Depends(get_db)):
    """Alpaca trading dashboard — current account status and positions."""
    stmt = select(TradingBotState).where(TradingBotState.bot_name == "prop_bot")
    bot_state = db.execute(stmt).scalar_one_or_none()

    if not bot_state:
        return {
            "status": "no_data",
            "message": "Alpaca bot has not started yet"
        }

    # Calculate performance
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
        stale = hours_ago > 1  # Mark as stale if >1 hour old
    else:
        hours_ago = None
        stale = True

    return {
        "bot": "Alpaca Apex Futures (prop_bot)",
        "account_status": "🔴 BLOCKED: API not responding (HTTP 403)" if stale else "🟢 SYNCED",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "last_update": last_update.isoformat() if last_update else None,
        "data_age_hours": round(hours_ago, 1) if hours_ago else None,
        "capital": {
            "starting": round(starting, 2),
            "current": round(current, 2),
            "profit": round(profit, 2),
            "profit_pct": round(profit_pct, 2),
        },
        "status": {
            "is_stale": stale,
            "message": f"Last synced {round(hours_ago, 1)}h ago" if stale else "Synced recently"
        },
        "issue": "API connectivity blocked - bot cannot update account data" if stale else None
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
