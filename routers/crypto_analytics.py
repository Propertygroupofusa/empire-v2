"""Cryptocurrency bot analytics and trade instrumentation endpoints."""
from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
from database import get_db
from models import CryptoTradeLog

router = APIRouter(prefix="/crypto/analytics", tags=["crypto-analytics"])


@router.get("/trades/recent")
async def get_recent_trades(limit: int = 50, db: Session = Depends(get_db)):
    """Get recent trades from the log (newest first)."""
    stmt = select(CryptoTradeLog).order_by(CryptoTradeLog.entered_at.desc()).limit(limit)
    trades = db.execute(stmt).scalars().all()
    return {
        "count": len(trades),
        "trades": [t.to_dict() for t in trades],
    }


@router.get("/trades/by-symbol/{symbol}")
async def get_trades_by_symbol(symbol: str, limit: int = 50, db: Session = Depends(get_db)):
    """Get trades for a specific symbol."""
    stmt = (
        select(CryptoTradeLog)
        .where(CryptoTradeLog.symbol == symbol)
        .order_by(CryptoTradeLog.entered_at.desc())
        .limit(limit)
    )
    trades = db.execute(stmt).scalars().all()
    return {
        "symbol": symbol,
        "count": len(trades),
        "trades": [t.to_dict() for t in trades],
    }


@router.get("/metrics/summary")
async def get_metrics_summary(hours: int = 24, db: Session = Depends(get_db)):
    """Summary statistics for trades in the last N hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    stmt = select(CryptoTradeLog).where(CryptoTradeLog.entered_at >= cutoff)
    trades = db.execute(stmt).scalars().all()

    # Calculate metrics
    total_trades = len(trades)
    closed_trades = [t for t in trades if t.exit_at]
    open_trades = [t for t in trades if not t.exit_at]

    winning_trades = [t for t in closed_trades if t.realized_pnl and t.realized_pnl > 0]
    losing_trades = [t for t in closed_trades if t.realized_pnl and t.realized_pnl <= 0]

    total_pnl = sum(t.realized_pnl for t in closed_trades if t.realized_pnl)
    avg_pnl_pct = (sum(t.realized_pnl_pct for t in closed_trades if t.realized_pnl_pct) / len(closed_trades)) if closed_trades else 0

    # ARM → ENTER metrics
    arm_count = total_trades  # Every entry gets armed
    enter_count = len(closed_trades)  # Every closed trade was entered
    arm_enter_conversion = (enter_count / arm_count * 100) if arm_count > 0 else 0

    avg_rsi_at_arm = (sum(t.arm_rsi for t in trades if t.arm_rsi) / len([t for t in trades if t.arm_rsi])) if any(t.arm_rsi for t in trades) else 0
    avg_rsi_at_entry = (sum(t.entry_rsi for t in trades if t.entry_rsi) / len([t for t in trades if t.entry_rsi])) if any(t.entry_rsi for t in trades) else 0

    avg_volume_ratio = (sum(t.volume_ratio for t in trades if t.volume_ratio) / len([t for t in trades if t.volume_ratio])) if any(t.volume_ratio for t in trades) else 0

    avg_mae = (sum(t.max_adverse_excursion for t in closed_trades if t.max_adverse_excursion) / len([t for t in closed_trades if t.max_adverse_excursion])) if any(t.max_adverse_excursion for t in closed_trades) else 0
    avg_mfe = (sum(t.max_favorable_excursion for t in closed_trades if t.max_favorable_excursion) / len([t for t in closed_trades if t.max_favorable_excursion])) if any(t.max_favorable_excursion for t in closed_trades) else 0

    avg_time_held = (sum(t.time_held_minutes for t in closed_trades if t.time_held_minutes) / len([t for t in closed_trades if t.time_held_minutes])) if any(t.time_held_minutes for t in closed_trades) else 0

    return {
        "period_hours": hours,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trade_counts": {
            "total": total_trades,
            "closed": len(closed_trades),
            "open": len(open_trades),
            "winning": len(winning_trades),
            "losing": len(losing_trades),
        },
        "pnl": {
            "total_pnl": round(total_pnl, 2),
            "avg_pnl_pct": round(avg_pnl_pct * 100, 2),
            "win_rate": round(len(winning_trades) / len(closed_trades) * 100, 2) if closed_trades else 0,
        },
        "arm_enter_metrics": {
            "arm_count": arm_count,
            "enter_count": enter_count,
            "conversion_rate_pct": round(arm_enter_conversion, 2),
        },
        "entry_statistics": {
            "avg_rsi_at_arm": round(avg_rsi_at_arm, 1),
            "avg_rsi_at_entry": round(avg_rsi_at_entry, 1),
            "avg_volume_ratio": round(avg_volume_ratio, 2),
        },
        "risk_metrics": {
            "avg_mae": round(avg_mae, 4),
            "avg_mfe": round(avg_mfe, 4),
        },
        "exit_metrics": {
            "avg_time_held_minutes": round(avg_time_held, 0),
        },
    }


@router.get("/metrics/by-symbol")
async def get_metrics_by_symbol(hours: int = 24, db: Session = Depends(get_db)):
    """Per-symbol metrics for the last N hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    stmt = select(CryptoTradeLog).where(CryptoTradeLog.entered_at >= cutoff)
    trades = db.execute(stmt).scalars().all()

    # Group by symbol
    by_symbol = {}
    for trade in trades:
        if trade.symbol not in by_symbol:
            by_symbol[trade.symbol] = []
        by_symbol[trade.symbol].append(trade)

    # Calculate per-symbol metrics
    results = {}
    for symbol, symbol_trades in by_symbol.items():
        closed = [t for t in symbol_trades if t.exit_at]
        winning = [t for t in closed if t.realized_pnl and t.realized_pnl > 0]

        total_pnl = sum(t.realized_pnl for t in closed if t.realized_pnl)

        results[symbol] = {
            "trades": len(symbol_trades),
            "closed": len(closed),
            "winning": len(winning),
            "pnl": round(total_pnl, 2),
            "win_rate": round(len(winning) / len(closed) * 100, 2) if closed else 0,
        }

    return {
        "period_hours": hours,
        "symbols": results,
    }


@router.get("/trading-summary")
async def get_trading_summary(db: Session = Depends(get_db)):
    """One-page summary: All coins traded, open positions, P&L status."""
    stmt = select(CryptoTradeLog).order_by(CryptoTradeLog.entered_at.desc())
    all_trades = db.execute(stmt).scalars().all()

    # Get unique symbols
    symbols = sorted(set(t.symbol for t in all_trades))

    # Build summary by symbol
    summary = {}
    for symbol in symbols:
        symbol_trades = [t for t in all_trades if t.symbol == symbol]
        open_trades = [t for t in symbol_trades if not t.exit_at]
        closed_trades = [t for t in symbol_trades if t.exit_at]

        total_pnl = sum(t.net_pnl for t in closed_trades if t.net_pnl) or 0
        win_count = len([t for t in closed_trades if t.net_pnl and t.net_pnl > 0])

        summary[symbol] = {
            "total_trades": len(symbol_trades),
            "open_positions": len(open_trades),
            "closed_trades": len(closed_trades),
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(win_count / len(closed_trades) * 100, 1) if closed_trades else 0,
            "last_trade": symbol_trades[0].entered_at.isoformat() if symbol_trades else None,
        }

    # Overall stats
    total_trades_all = len(all_trades)
    open_all = [t for t in all_trades if not t.exit_at]
    closed_all = [t for t in all_trades if t.exit_at]
    total_pnl_all = sum(t.net_pnl for t in closed_all if t.net_pnl) or 0

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "overall": {
            "coins_traded": len(symbols),
            "total_trades": total_trades_all,
            "open_positions": len(open_all),
            "closed_trades": len(closed_all),
            "total_pnl": round(total_pnl_all, 2),
        },
        "by_coin": summary,
    }


@router.get("/health")
async def analytics_health():
    """Health check for analytics system."""
    return {
        "status": "ok",
        "service": "crypto-analytics",
        "instrumentation": [
            "CryptoTradeLog: full trade lifecycle tracking",
            "Entry: ARM RSI, volume ratio, candle position",
            "Exit: reason, P&L, time held, MAE/MFE",
            "Endpoints: /trades/recent, /trades/by-symbol, /metrics/summary, /metrics/by-symbol, /trading-summary",
        ],
    }
