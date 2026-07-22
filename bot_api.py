#!/usr/bin/env python3
"""REST API for trading bot status, positions, and trades."""
import json
import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Bot API", version="1.0")

# Enable CORS for dashboard access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATE_FILE = "bot2_state.json"
POSITIONS_FILE = "bot2_positions.json"
TRADES_FILE = "bot2_trades.json"


def load_state():
    """Load bot state from JSON file."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {
        "start_pf": 980.0,
        "day_start_pf": 980.0,
        "wins": 0,
        "losses": 0,
        "trades_today": 0,
        "is_live": False,
    }


def load_positions():
    """Load open positions from JSON file."""
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {}


def load_trades():
    """Load closed trades from JSON file."""
    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return []


@app.get("/health")
async def health():
    """Health check."""
    return {"status": "ok"}


@app.get("/api/bot/status")
async def bot_status():
    """Get current bot status: portfolio value, P&L, win rate, positions."""
    state = load_state()
    positions = load_positions()

    start_pf = state.get("start_pf", 980.0)
    current_pf = state.get("current_pf", start_pf)  # In production, would query Alpaca

    pnl_amount = current_pf - start_pf
    pnl_pct = ((pnl_amount / start_pf) * 100) if start_pf > 0 else 0

    wins = state.get("wins", 0)
    losses = state.get("losses", 0)
    total = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0

    return {
        "portfolio_value": round(current_pf, 2),
        "pnl_amount": round(pnl_amount, 2),
        "pnl_percent": round(pnl_pct, 2),
        "win_rate": round(win_rate, 1),
        "wins": wins,
        "losses": losses,
        "total_trades": total,
        "open_positions": len(positions),
        "mode": "LIVE" if state.get("is_live") else "PAPER",
    }


@app.get("/api/bot/positions")
async def bot_positions():
    """Get open positions with entry/current prices and P&L."""
    positions = load_positions()
    result = []

    for symbol, pos in positions.items():
        entry = pos.get("entry_price", 0)
        current = pos.get("current_price", entry)  # Would fetch from market data
        pnl = ((current - entry) / entry * 100) if entry > 0 else 0

        result.append({
            "symbol": symbol,
            "entry_price": round(entry, 4),
            "current_price": round(current, 4),
            "pnl_percent": round(pnl, 2),
            "peak_price": pos.get("peak_price", entry),
        })

    return {"positions": result, "count": len(result)}


@app.get("/api/bot/trades")
async def bot_trades():
    """Get recent closed trades."""
    trades = load_trades()

    # Calculate stats
    total = len(trades)
    wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
    losses = sum(1 for t in trades if t.get("pnl", 0) < 0)
    avg_pnl = sum(t.get("pnl", 0) for t in trades) / total if total > 0 else 0

    return {
        "trades": trades[-50:],  # Last 50 trades
        "stats": {
            "total_closed": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
            "avg_pnl": round(avg_pnl, 4),
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
