"""
BOT RACE DASHBOARD — Live competitive tracking for Alpaca vs Coinbase trading bots.
Real-time leaderboard showing balance, ROI, trades, and performance metrics.
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from datetime import datetime, timedelta
from sqlalchemy import select, func, desc
from database import AsyncSessionLocal
from models import BotPosition, CryptoTradeLog, TradingBotState
import json
import logging
import os

log = logging.getLogger("bot_race")
router = APIRouter(prefix="/bot-race", tags=["Bot Race"])

# Live tracking state
BOT_STATE = {
    "alpaca": {
        "name": "Alpaca (Stocks)",
        "balance": 1000.80,
        "target": 2000.00,
        "daily_pnl": 0.0,
        "open_positions": 0,
        "trades_today": 0,
        "win_rate": 0.0,
        "last_update": datetime.utcnow().isoformat(),
    },
    "crypto": {
        "name": "Coinbase (Crypto)",
        "balance": 700.01,
        "target": 2000.00,
        "daily_pnl": 0.0,
        "open_positions": 0,
        "trades_today": 0,
        "win_rate": 0.0,
        "last_update": datetime.utcnow().isoformat(),
    }
}


async def get_alpaca_metrics():
    """Fetch Alpaca bot metrics from database."""
    try:
        async with AsyncSessionLocal() as session:
            # Get base capital from TradingBotState
            result = await session.execute(
                select(TradingBotState).where(TradingBotState.bot_name == "alpaca_prop")
            )
            state = result.scalar_one_or_none()

            # Get open positions count
            pos_result = await session.execute(
                select(func.count(BotPosition.id)).where(BotPosition.bot == "alpaca_prop")
            )
            open_pos = pos_result.scalar() or 0

            balance = state.current_capital if state else 1000.80

            return {
                "balance": balance,
                "open_positions": open_pos,
                "daily_pnl": state.daily_pnl if state else 0.0,
                "win_rate": 0.65 if state else 0.0,  # Placeholder
            }
    except Exception as e:
        log.debug(f"Error fetching Alpaca metrics: {e}")
        return {"balance": 1000.80, "open_positions": 0, "daily_pnl": 0.0, "win_rate": 0.0}


async def get_crypto_metrics():
    """Fetch Coinbase bot metrics from database."""
    try:
        async with AsyncSessionLocal() as session:
            # Get open positions
            pos_result = await session.execute(
                select(func.count(BotPosition.id)).where(BotPosition.bot == "crypto_coinbase")
            )
            open_pos = pos_result.scalar() or 0

            # Get today's trades
            today = datetime.utcnow().date()
            trades_result = await session.execute(
                select(func.count(CryptoTradeLog.id)).where(
                    func.date(CryptoTradeLog.entered_at) == today,
                    CryptoTradeLog.exit_at != None
                )
            )
            trades_today = trades_result.scalar() or 0

            # Calculate win rate from recent trades
            wins_result = await session.execute(
                select(func.count(CryptoTradeLog.id)).where(
                    CryptoTradeLog.realized_pnl > 0,
                    CryptoTradeLog.exit_at != None
                )
            )
            wins = wins_result.scalar() or 0

            return {
                "open_positions": open_pos,
                "trades_today": trades_today,
                "win_rate": (wins / max(trades_today, 1)) if trades_today > 0 else 0.0,
            }
    except Exception as e:
        log.debug(f"Error fetching Crypto metrics: {e}")
        return {"open_positions": 0, "trades_today": 0, "win_rate": 0.0}


@router.get("/race-data")
async def get_race_data():
    """Get real-time race data for both bots."""
    alpaca = await get_alpaca_metrics()
    crypto = await get_crypto_metrics()

    # Update state
    BOT_STATE["alpaca"].update(alpaca)
    BOT_STATE["alpaca"]["last_update"] = datetime.utcnow().isoformat()

    BOT_STATE["crypto"].update(crypto)
    BOT_STATE["crypto"]["last_update"] = datetime.utcnow().isoformat()

    # Calculate progress to target
    for bot_key in ["alpaca", "crypto"]:
        bot = BOT_STATE[bot_key]
        bot["progress_pct"] = (bot["balance"] / bot["target"]) * 100
        bot["gap_to_target"] = bot["target"] - bot["balance"]
        bot["daily_roi_pct"] = (bot["daily_pnl"] / bot["balance"] * 100) if bot["balance"] > 0 else 0.0

    # Determine leader
    alpaca_balance = BOT_STATE["alpaca"]["balance"]
    crypto_balance = BOT_STATE["crypto"]["balance"]

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "alpaca": BOT_STATE["alpaca"],
        "crypto": BOT_STATE["crypto"],
        "leader": "alpaca" if alpaca_balance > crypto_balance else "crypto",
        "spread": abs(alpaca_balance - crypto_balance),
        "combined_balance": alpaca_balance + crypto_balance,
    }


@router.get("/leaderboard")
async def get_leaderboard():
    """Get formatted leaderboard."""
    race = await get_race_data()

    bots = [
        ("Alpaca", race["alpaca"]),
        ("Coinbase", race["crypto"]),
    ]

    # Sort by balance (descending)
    bots.sort(key=lambda x: x[1]["balance"], reverse=True)

    leaderboard = []
    for rank, (name, data) in enumerate(bots, 1):
        leaderboard.append({
            "rank": rank,
            "name": name,
            "balance": f"${data['balance']:.2f}",
            "target": f"${data['target']:.2f}",
            "progress": f"{data['progress_pct']:.1f}%",
            "gap": f"${data['gap_to_target']:.2f}",
            "daily_roi": f"{data['daily_roi_pct']:.2f}%",
            "positions": data["open_positions"],
            "trades": data.get("trades_today", 0),
            "win_rate": f"{data['win_rate']*100:.1f}%",
        })

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "leaderboard": leaderboard,
        "combined_balance": race["combined_balance"],
    }


@router.get("/dashboard")
async def get_dashboard():
    """Serve the bot race dashboard HTML."""
    possible_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot_race_dashboard.html"),
        "/app/bot_race_dashboard.html",
        "bot_race_dashboard.html",
    ]

    for path in possible_paths:
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    return HTMLResponse(content=f.read())
            except Exception as e:
                log.debug(f"Failed to read dashboard from {path}: {e}")

    log.warning("bot_race_dashboard.html not found in any expected location")
    return HTMLResponse(content="""
    <html>
        <body style="font-family: sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; background: #f5f5f5;">
            <div style="text-align: center;">
                <h1>🏁 Bot Race Dashboard</h1>
                <p>Dashboard file not found. Please check deployment.</p>
                <p><a href="/api/bot-race/race-data">View Race Data API →</a></p>
            </div>
        </body>
    </html>
    """, status_code=200)
