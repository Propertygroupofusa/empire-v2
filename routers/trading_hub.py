"""
Trading Hub - Real-Time Bot Activity Dashboard
Shows live positions, trade history, bot status, and P&L metrics
Plus: Measurements Dashboard for statistical edge validation
"""

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import logging
import os

from database import get_db
from models import (
    CryptoTradeLog, BotPosition, TradingBotState
)

# Measurement system: Statistical edge validation (lazy-loaded on first use to avoid startup delay)

ET = ZoneInfo("America/New_York")
router = APIRouter(tags=["trading"])
log = logging.getLogger("trading_hub")

# ============================================================================
# API ENDPOINTS
# ============================================================================

@router.get("/api/positions")
async def get_live_positions(db: AsyncSession = Depends(get_db)):
    """Get all active positions across all trading modes"""
    try:
        result = await db.execute(
            select(BotPosition)
        )
        positions = result.scalars().all()

        return {
            "count": len(positions),
            "positions": [
                {
                    "id": p.id,
                    "symbol": p.symbol,
                    "side": p.side,
                    "quantity": float(p.qty) if p.qty is not None else 0.0,
                    "entry_price": float(p.entry_price),
                    # Current model stores open positions and entry context only.
                    "current_price": None,
                    "pnl": 0.0,
                    "pnl_pct": 0.0,
                    "entered_at": p.opened_at.isoformat() if p.opened_at else None,
                    "mode": p.bot,  # e.g. "crypto_coinbase" / "prop_apex"
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
            .where(CryptoTradeLog.event_type == "EXIT")
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
                    "quantity": float(t.position_size) if t.position_size else None,
                    "pnl": float(t.net_pnl) if t.net_pnl else 0.0,
                    "pnl_pct": float(t.net_pnl_pct) if t.net_pnl_pct else 0.0,
                    "side": "long",
                    "status": "closed",
                    "timestamp": (t.exit_at or t.timestamp).isoformat() if (t.exit_at or t.timestamp) else None,
                    "strategy": t.strategy_version,
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
        # Get bot state (bucket model: one row per bot bucket)
        result = await db.execute(select(TradingBotState))
        bot_states = result.scalars().all()

        # Get open positions count
        pos_result = await db.execute(
            select(func.count(BotPosition.id))
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

        # Calculate daily P&L from EXIT events
        pnl_result = await db.execute(
            select(func.sum(CryptoTradeLog.net_pnl)).where(
                (func.date(CryptoTradeLog.timestamp) == today) &
                (CryptoTradeLog.event_type == "EXIT")
            )
        )
        daily_pnl = pnl_result.scalar() or 0

        equity = sum(float(s.base_capital or 0) for s in bot_states)
        last_update = max((s.updated_at for s in bot_states if s.updated_at), default=None)
        status = "active" if open_positions > 0 else "idle"

        return {
            "status": status,
            "mode": "multi_bot",
            "last_update": last_update.isoformat() if last_update else None,
            "open_positions": open_positions,
            "trades_today": trades_today,
            "daily_pnl": float(daily_pnl),
            "equity": round(equity, 2),
            "cash": 0.0,
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
            select(BotPosition)
        )
        positions = pos_result.scalars().all()
        total_position_value = sum(float((p.qty or 0) * (p.entry_price or 0)) for p in positions)
        total_pnl = 0.0

        # Today's stats
        today = datetime.now(ET).date()
        trades_result = await db.execute(
            select(CryptoTradeLog).where(
                (func.date(CryptoTradeLog.timestamp) == today) &
                (CryptoTradeLog.event_type == "EXIT")
            )
        )
        today_trades = trades_result.scalars().all()

        wins = len([t for t in today_trades if t.net_pnl and float(t.net_pnl) > 0])
        losses = len([t for t in today_trades if t.net_pnl and float(t.net_pnl) < 0])
        daily_pnl = sum(float(t.net_pnl) for t in today_trades if t.net_pnl)

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
# MEASUREMENTS ENDPOINTS (Statistical Edge Validation)
# ============================================================================

@router.get("/api/measurements/strategies")
async def get_strategies_evidence():
    """Get statistical evidence for all strategies"""
    try:
        from measurement_system import trade_logger, StatisticalAnalyzer
    except ImportError:
        return []

    strategies = [
        {"name": "Blitzkrieg (10x Day Trading)", "key": "blitzkrieg"},
        {"name": "Crypto Coinbase (RSI Recovery)", "key": "crypto_coinbase_rsi_recovery"},
        {"name": "APEX Futures (Mean Reversion)", "key": "apex_mean_reversion"},
    ]

    results = []
    for strategy in strategies:
        trades = trade_logger.get_strategy_trades(strategy["key"])
        if trades:
            evidence = StatisticalAnalyzer.calculate_summary(trades)
            results.append({
                "name": strategy["name"],
                "key": strategy["key"],
                "evidence": evidence,
            })

    return results


@router.get("/api/measurements/trades/recent")
async def get_recent_measurements_trades(limit: int = 20):
    """Get recent trades from measurement system with full context"""
    try:
        from measurement_system import trade_logger
    except ImportError:
        return {"trades": []}

    # Get all logged trades (from all strategies combined)
    all_trades = []

    # Collect trades from all strategies
    strategies = ["blitzkrieg", "crypto_coinbase_rsi_recovery", "apex_mean_reversion"]
    for strategy_key in strategies:
        trades = trade_logger.get_strategy_trades(strategy_key)
        for trade in trades[-limit:]:  # Get last N trades per strategy
            all_trades.append({
                "strategy": strategy_key,
                "symbol": trade.symbol,
                "entry_price": float(trade.entry_price) if trade.entry_price else None,
                "exit_price": float(trade.exit_price) if trade.exit_price else None,
                "quantity": float(trade.entry_quantity) if trade.entry_quantity else 0,
                "entry_time": trade.entry_time.isoformat() if trade.entry_time else None,
                "exit_time": trade.exit_time.isoformat() if trade.exit_time else None,
                "exit_reason": trade.exit_reason,
                "actual_pnl": float(trade.actual_pnl) if trade.actual_pnl else 0,
                "actual_pnl_pct": float(trade.actual_pnl_pct) if trade.actual_pnl_pct else 0,
                "expected_value": float(trade.expected_value) if trade.expected_value else None,
                "pnl": float(trade.actual_pnl) if trade.actual_pnl else 0,
            })

    # Sort by exit time, most recent first
    all_trades.sort(
        key=lambda t: t["exit_time"] or t["entry_time"] or "",
        reverse=True
    )

    return {
        "count": len(all_trades),
        "trades": all_trades[:limit],
    }


@router.get("/api/measurements/summary")
async def get_measurements_summary():
    """Get overall measurements summary across all strategies"""
    try:
        from measurement_system import trade_logger, StatisticalAnalyzer
    except ImportError:
        return {
            "total_trades": 0,
            "strategies": [],
            "overall_win_rate": 0,
            "overall_profit_factor": 0,
        }

    strategies = [
        {"name": "Blitzkrieg", "key": "blitzkrieg"},
        {"name": "Crypto Coinbase", "key": "crypto_coinbase_rsi_recovery"},
        {"name": "APEX Futures", "key": "apex_mean_reversion"},
    ]

    total_trades = 0
    overall_evidence = None
    strategies_data = []

    for strategy in strategies:
        trades = trade_logger.get_strategy_trades(strategy["key"])
        total_trades += len(trades)

        if trades:
            evidence = StatisticalAnalyzer.calculate_summary(trades)
            strategies_data.append({
                "name": strategy["name"],
                "trades": len(trades),
                "win_rate": evidence.get("win_rate", 0),
                "profit_factor": evidence.get("profit_factor", 0),
                "p_value": evidence.get("p_value", 1.0),
            })

            # Accumulate for overall stats
            if overall_evidence is None:
                overall_evidence = evidence
            else:
                # Merge - this is a simple approach, could be more sophisticated
                overall_evidence["trade_count"] += evidence.get("trade_count", 0)
                # ... other aggregations could go here

    return {
        "total_trades": total_trades,
        "strategies": strategies_data,
        "overall_win_rate": overall_evidence.get("win_rate", 0) if overall_evidence else 0,
        "overall_profit_factor": overall_evidence.get("profit_factor", 0) if overall_evidence else 0,
        "timestamp": datetime.now(ZoneInfo("America/New_York")).isoformat(),
    }


# ============================================================================
# HTML PAGES
# ============================================================================

@router.get("/measurements")
async def measurements_dashboard():
    """Serve measurements dashboard HTML"""
    possible_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "measurements_dashboard.html"),
        "/app/measurements_dashboard.html",
        "measurements_dashboard.html",
    ]

    for path in possible_paths:
        if os.path.exists(path):
            return FileResponse(path, media_type="text/html")

    return HTMLResponse("<h1>Measurements Dashboard</h1><p>measurements_dashboard.html not found</p>")


@router.get("/")
async def trading_hub_page():
    """Serve trading hub dashboard HTML"""
    return {
        "message": "Trading Hub API - Use /api/positions, /api/trades/recent, /api/bot-status, /api/summary, or /api/measurements/* for edge validation"
    }
