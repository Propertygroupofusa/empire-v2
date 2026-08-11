"""
Trading Hub - Real-Time Bot Activity Dashboard
Shows live positions, trade history, bot status, and P&L metrics
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import logging

from database import get_db
from models import (
    CryptoTradeLog, BotPosition, TradingBotState
)

ET = ZoneInfo("America/New_York")
router = APIRouter(prefix="/trading-hub", tags=["trading"])
log = logging.getLogger("trading_hub")

# ============================================================================
# API ENDPOINTS
# ============================================================================

@router.get("/api/positions")
async def get_live_positions(db: AsyncSession = Depends(get_db)):
    """Get all active positions across all trading modes"""
    try:
        result = await db.execute(
            select(BotPosition).where(BotPosition.status == "open")
        )
        positions = result.scalars().all()

        return {
            "count": len(positions),
            "positions": [
                {
                    "id": p.id,
                    "symbol": p.symbol,
                    "side": p.side,
                    "quantity": float(p.quantity),
                    "entry_price": float(p.entry_price),
                    "current_price": float(p.current_price) if p.current_price else None,
                    "pnl": float(p.pnl) if p.pnl else 0,
                    "pnl_pct": float(p.pnl_pct) if p.pnl_pct else 0,
                    "entered_at": p.entered_at.isoformat() if p.entered_at else None,
                    "mode": p.mode,  # "crypto", "futures", "options"
                }
                for p in positions
            ]
        }
    except Exception as e:
        log.error(f"Error fetching positions: {e}")
        return {"count": 0, "positions": [], "error": str(e)}

@router.get("/api/trades/recent")
async def get_recent_trades(limit: int = 10, db: AsyncSession = Depends(get_db)):
    """Get recent closed trades"""
    try:
        result = await db.execute(
            select(CryptoTradeLog)
            .order_by(desc(CryptoTradeLog.timestamp))
            .limit(limit)
        )
        trades = result.scalars().all()

        return {
            "count": len(trades),
            "trades": [
                {
                    "id": t.id,
                    "symbol": t.symbol,
                    "entry_price": float(t.entry_price) if t.entry_price else None,
                    "exit_price": float(t.exit_price) if t.exit_price else None,
                    "quantity": float(t.quantity) if t.quantity else None,
                    "pnl": float(t.pnl) if t.pnl else 0,
                    "pnl_pct": float(t.pnl_pct) if t.pnl_pct else 0,
                    "side": t.side,
                    "status": t.status,
                    "timestamp": t.timestamp.isoformat() if t.timestamp else None,
                    "strategy": t.strategy,
                }
                for t in trades
            ]
        }
    except Exception as e:
        log.error(f"Error fetching trades: {e}")
        return {"count": 0, "trades": [], "error": str(e)}

@router.get("/api/bot-status")
async def get_bot_status(db: AsyncSession = Depends(get_db)):
    """Get current bot status and metrics"""
    try:
        # Get bot state
        result = await db.execute(select(TradingBotState).limit(1))
        bot_state = result.scalar_one_or_none()

        # Get open positions count
        pos_result = await db.execute(
            select(func.count(BotPosition.id)).where(BotPosition.status == "open")
        )
        open_positions = pos_result.scalar() or 0

        # Get today's trades
        today = datetime.now(ET).date()
        trades_result = await db.execute(
            select(func.count(CryptoTradeLog.id)).where(
                func.date(CryptoTradeLog.timestamp) == today
            )
        )
        trades_today = trades_result.scalar() or 0

        # Calculate daily P&L
        pnl_result = await db.execute(
            select(func.sum(CryptoTradeLog.pnl)).where(
                func.date(CryptoTradeLog.timestamp) == today
            )
        )
        daily_pnl = pnl_result.scalar() or 0

        return {
            "status": bot_state.status if bot_state else "unknown",
            "mode": bot_state.mode if bot_state else "idle",
            "last_update": bot_state.last_update.isoformat() if bot_state and bot_state.last_update else None,
            "open_positions": open_positions,
            "trades_today": trades_today,
            "daily_pnl": float(daily_pnl),
            "equity": float(bot_state.equity) if bot_state and bot_state.equity else 0,
            "cash": float(bot_state.cash) if bot_state and bot_state.cash else 0,
        }
    except Exception as e:
        log.error(f"Error fetching bot status: {e}")
        return {
            "status": "error",
            "mode": "unknown",
            "error": str(e),
            "open_positions": 0,
            "trades_today": 0,
            "daily_pnl": 0,
        }

@router.get("/api/summary")
async def get_trading_summary(db: AsyncSession = Depends(get_db)):
    """Get complete trading summary"""
    try:
        # Positions
        pos_result = await db.execute(
            select(BotPosition).where(BotPosition.status == "open")
        )
        positions = pos_result.scalars().all()
        total_position_value = sum(float(p.quantity * p.entry_price) for p in positions if p.quantity and p.entry_price)
        total_pnl = sum(float(p.pnl) for p in positions if p.pnl)

        # Today's stats
        today = datetime.now(ET).date()
        trades_result = await db.execute(
            select(CryptoTradeLog).where(
                func.date(CryptoTradeLog.timestamp) == today
            )
        )
        today_trades = trades_result.scalars().all()

        wins = len([t for t in today_trades if t.pnl and float(t.pnl) > 0])
        losses = len([t for t in today_trades if t.pnl and float(t.pnl) < 0])
        daily_pnl = sum(float(t.pnl) for t in today_trades if t.pnl)

        return {
            "positions": {
                "count": len(positions),
                "total_value": round(total_position_value, 2),
                "unrealized_pnl": round(total_pnl, 2),
            },
            "today": {
                "trades": len(today_trades),
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / len(today_trades) * 100, 1) if today_trades else 0,
                "pnl": round(daily_pnl, 2),
            },
            "timestamp": datetime.now(ET).isoformat(),
        }
    except Exception as e:
        log.error(f"Error fetching summary: {e}")
        return {
            "positions": {"count": 0, "total_value": 0, "unrealized_pnl": 0},
            "today": {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "pnl": 0},
            "error": str(e),
        }

# ============================================================================
# HTML PAGE
# ============================================================================

@router.get("/")
async def trading_hub_page():
    """Serve trading hub dashboard HTML"""
    return {
        "message": "Trading Hub API - Use /api/positions, /api/trades/recent, /api/bot-status, /api/summary"
    }
