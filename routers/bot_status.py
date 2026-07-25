"""
BOT STATUS ROUTER — bot_2_crypto_scalper.py status, in-process
=================================================================
Reads the same bot2_state.json / bot2_positions.json / bot2_trades.json
files bot_api.py reads. bot_2_crypto_scalper.py runs as a background
subprocess started by main.py (see lifespan), in the SAME container/
filesystem as this app - so there's no network hop needed and no
separate "bot-api" service to keep alive.

bot_api.py itself is left as-is (a standalone script useful if this
bot is ever split into its own deployed service later); this router
just duplicates its small read-only logic for the common case where
it isn't.
"""
import json
import os
from datetime import datetime

from fastapi import APIRouter

router = APIRouter()

STARTING_CAPITAL = 980.0
BOT_STATE_FILE = "bot2_state.json"
POSITIONS_FILE = "bot2_positions.json"
TRADES_FILE = "bot2_trades.json"


def _load_bot_state() -> dict:
    try:
        if os.path.exists(BOT_STATE_FILE):
            with open(BOT_STATE_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except (json.JSONDecodeError, IOError):
        pass
    return {
        "day": 1,
        "wins": 0,
        "losses": 0,
        "is_live": False,
        "start_pf": STARTING_CAPITAL,
        "peak_pf": STARTING_CAPITAL,
        "current_pf": STARTING_CAPITAL,
    }


def _load_positions() -> dict:
    try:
        if os.path.exists(POSITIONS_FILE):
            with open(POSITIONS_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except (json.JSONDecodeError, IOError):
        pass
    return {}


def _load_trades() -> list:
    try:
        if os.path.exists(TRADES_FILE):
            with open(TRADES_FILE, "r") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        pass
    return []


def _get_portfolio_value() -> float:
    state = _load_bot_state()
    return float(state.get("current_pf", state.get("peak_pf", STARTING_CAPITAL)))


@router.get("/status")
async def bot_status():
    state = _load_bot_state()
    positions = _load_positions()

    is_live = state.get("is_live", False)
    pf_value = _get_portfolio_value()
    starting = state.get("start_pf", STARTING_CAPITAL)

    profit = pf_value - starting
    profit_pct = (profit / starting * 100) if starting > 0 else 0

    total_trades = state.get("wins", 0) + state.get("losses", 0)
    win_rate = (state.get("wins", 0) / total_trades * 100) if total_trades > 0 else 0

    return {
        "mode": "🔴 LIVE" if is_live else "📄 PAPER",
        "is_live": is_live,
        "portfolio_value": round(pf_value, 2),
        "starting_capital": round(starting, 2),
        "profit": round(profit, 2),
        "profit_pct": round(profit_pct, 2),
        "active_positions": len(positions),
        "total_trades": total_trades,
        "wins": state.get("wins", 0),
        "losses": state.get("losses", 0),
        "win_rate": round(win_rate, 2),
        "day": state.get("day", 1),
        "peak_portfolio": round(state.get("peak_pf", STARTING_CAPITAL), 2),
        "timestamp": datetime.now().isoformat(),
    }


@router.get("/positions")
async def bot_positions():
    positions = _load_positions()

    result = []
    for symbol, pos_data in positions.items():
        if not isinstance(pos_data, dict):
            continue
        try:
            entry_px = float(pos_data.get("entry_price", 0))
            current_px = float(pos_data.get("current_price", entry_px))
            if entry_px > 0:
                pnl = ((current_px - entry_px) / entry_px) * 100
                pnl_dollar = current_px - entry_px
            else:
                pnl = 0
                pnl_dollar = 0
            result.append({
                "symbol": str(symbol),
                "entry_price": round(entry_px, 4),
                "current_price": round(current_px, 4),
                "pnl_pct": round(pnl, 2),
                "pnl_dollar": round(pnl_dollar, 2),
                "position_size": round(float(pos_data.get("position_size", 0)), 2),
            })
        except (ValueError, TypeError):
            continue

    return {"positions": result, "count": len(result)}


@router.get("/trades")
async def bot_trades(limit: int = 10):
    state = _load_bot_state()
    recent_trades = _load_trades()

    total = state.get("wins", 0) + state.get("losses", 0)
    return {
        "total_trades": total,
        "wins": state.get("wins", 0),
        "losses": state.get("losses", 0),
        "win_rate": round((state.get("wins", 0) / total * 100) if total > 0 else 0, 2),
        "recent_trades": recent_trades[-limit:] if recent_trades else [],
    }


@router.get("/health")
async def bot_health():
    state = _load_bot_state()
    pf = _get_portfolio_value()
    total_trades = state.get("wins", 0) + state.get("losses", 0)
    return {
        "status": "ok",
        "bot_running": total_trades > 0,
        "portfolio_value": round(pf, 2),
        "total_trades": total_trades,
        "last_update": datetime.now().isoformat(),
    }
