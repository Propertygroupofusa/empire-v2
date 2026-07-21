#!/usr/bin/env python3
"""
BOT DATA API — Real-time trading data from bot_2_crypto_scalper.py

Serves bot state, portfolio value, trades, P&L via REST API
Used by trading_dashboard.html to display REAL trading data
"""
import json
import os
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import logging

log = logging.getLogger("bot_api")

app = FastAPI(title="Bot Data API", version="1.0.0")

# Enable CORS for dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Bot constants
STARTING_CAPITAL = 980.0
BOT_STATE_FILE = "bot2_state.json"
POSITIONS_FILE = "bot2_positions.json"  # Will be created by bot
TRADES_FILE = "bot2_trades.json"


def load_bot_state() -> dict:
    """Load current bot state from file"""
    try:
        if os.path.exists(BOT_STATE_FILE):
            with open(BOT_STATE_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        log.error(f"Error loading bot state: {e}")

    return {
        "day": 1,
        "wins": 0,
        "losses": 0,
        "is_live": False,
        "start_pf": STARTING_CAPITAL,
        "peak_pf": STARTING_CAPITAL,
    }


def load_positions() -> dict:
    """Load current positions from bot"""
    try:
        if os.path.exists(POSITIONS_FILE):
            with open(POSITIONS_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        log.debug(f"Error loading positions: {e}")

    return {}


def load_trades() -> list:
    """Load trade history from bot"""
    try:
        if os.path.exists(TRADES_FILE):
            with open(TRADES_FILE, "r") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
    except Exception as e:
        log.debug(f"Error loading trades: {e}")

    return []


def get_portfolio_value() -> float:
    """Get current portfolio value (sum of cash + position values)"""
    state = load_bot_state()
    return state.get("peak_pf", STARTING_CAPITAL)


@app.get("/api/bot/status")
async def bot_status():
    """Get bot trading status"""
    state = load_bot_state()
    positions = load_positions()

    is_live = state.get("is_live", False)
    pf_value = get_portfolio_value()
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
        "cash_available": round(starting, 2),
        "active_positions": len(positions),
        "total_trades": total_trades,
        "wins": state.get("wins", 0),
        "losses": state.get("losses", 0),
        "win_rate": round(win_rate, 2),
        "day": state.get("day", 1),
        "peak_portfolio": round(state.get("peak_pf", STARTING_CAPITAL), 2),
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/bot/positions")
async def bot_positions():
    """Get current open positions"""
    positions = load_positions()

    result = []
    for symbol, pos_data in positions.items():
        entry_px = pos_data.get("entry_price", 0)
        current_px = pos_data.get("current_price", entry_px)

        if entry_px > 0:
            pnl = ((current_px - entry_px) / entry_px) * 100
            pnl_dollar = current_px - entry_px
        else:
            pnl = 0
            pnl_dollar = 0

        result.append({
            "symbol": symbol,
            "entry_price": round(entry_px, 4),
            "current_price": round(current_px, 4),
            "pnl_pct": round(pnl, 2),
            "pnl_dollar": round(pnl_dollar, 2),
            "position_size": round(pos_data.get("position_size", 0), 2),
        })

    return {"positions": result, "count": len(result)}


@app.get("/api/bot/trades")
async def bot_trades(limit: int = 10):
    """Get recent closed trades"""
    state = load_bot_state()
    recent_trades = load_trades()

    total = state.get("wins", 0) + state.get("losses", 0)
    return {
        "total_trades": total,
        "wins": state.get("wins", 0),
        "losses": state.get("losses", 0),
        "win_rate": round((state.get("wins", 0) / total * 100) if total > 0 else 0, 2),
        "recent_trades": recent_trades[-limit:] if recent_trades else []
    }


@app.get("/api/bot/health")
async def bot_health():
    """Health check for bot API"""
    try:
        state = load_bot_state()
        pf = get_portfolio_value()

        return {
            "status": "ok",
            "bot_running": state.get("wins", 0) + state.get("losses", 0) > 0,
            "portfolio_value": round(pf, 2),
            "last_update": datetime.now().isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
async def root():
    """API documentation"""
    return {
        "name": "Bot Data API",
        "version": "1.0.0",
        "description": "Real-time trading data from crypto scalper bot",
        "endpoints": {
            "/api/bot/status": "Current bot status, portfolio, P&L",
            "/api/bot/positions": "Open positions",
            "/api/bot/trades": "Trade history and statistics",
            "/api/bot/health": "API health check",
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
