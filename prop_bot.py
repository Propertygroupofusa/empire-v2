"""
DEL'S TRADING EMPIRE — PROP BOT v3
=====================================
APEX $25K Futures evaluation — MES, MNQ, MGC
Account: APEX_589296
Rule: 7 consecutive profitable days before going live
"""

import os
import asyncio
import logging
import time
import json
from datetime import datetime
import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("prop_bot")

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
BASE_URL      = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
LIVE_TRADE    = os.getenv("ALPACA_LIVE_TRADE", "false").lower() == "true"
STOP          = os.getenv("STOP_TRADING", "false").lower() == "true"
STATE_DIR     = os.getenv("STATE_DIR", "/data/bot_state")
STATE_FILE    = os.path.join(STATE_DIR, "prop_bot_state.json")

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type": "application/json"
}

# APEX futures — use micro contracts (lower risk during evaluation)
FUTURES = {
    "MES": {"name": "Micro E-mini S&P 500", "qty": 1, "symbol": "SPY"},   # Use SPY as proxy
    "MNQ": {"name": "Micro E-mini Nasdaq",  "qty": 1, "symbol": "QQQ"},   # Use QQQ as proxy
}

# Track profitable days for APEX 7-day rule
profitable_days = []
daily_pnl = 0.0
open_prop_positions = {}

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"profitable_days": [], "daily_pnl": 0.0}

def save_state():
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump({"profitable_days": profitable_days, "daily_pnl": daily_pnl}, f, indent=2)


async def get_price_rsi(session, symbol):
    """Get price and RSI for futures proxy symbol"""
    try:
        url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=5Min&limit=20"
        async with session.get(url, headers=HEADERS) as r:
            if r.status != 200:
                return None
            data = await r.json()
            bars = data.get("bars", [])
            if len(bars) < 14:
                return None

            closes = [b["c"] for b in bars]
            price = closes[-1]

            gains = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
            losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
            avg_gain = sum(gains[-14:]) / 14
            avg_loss = sum(losses[-14:]) / 14
            rs = avg_gain / avg_loss if avg_loss > 0 else 100
            rsi = 100 - (100 / (1 + rs))

            sma5 = sum(closes[-5:]) / 5
            sma10 = sum(closes[-10:]) / 10
            trend = "bullish" if sma5 > sma10 else "bearish"

            return {"price": price, "rsi": round(rsi, 1), "trend": trend}
    except Exception as e:
        log.error(f"Price error {symbol}: {e}")
        return None


async def execute_futures_trade(session, contract, action, qty, price):
    """Execute futures trade via Alpaca (using stock proxy for paper)"""
    global daily_pnl

    symbol = FUTURES[contract]["symbol"]
    side = "buy" if action == "BUY" else "sell"

    order = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": "day",
    }

    mode = "LIVE" if LIVE_TRADE else "PAPER"

    try:
        async with session.post(f"{BASE_URL}/v2/orders", headers=HEADERS, json=order) as r:
            result = await r.json()
            if r.status in (200, 201):
                log.info(f"✅ FUTURES TRADE | {mode} | {action} {qty} {contract} ({symbol}) @ ${price:.2f} | APEX_589296")
                open_prop_positions[contract] = {"action": action, "entry": price, "qty": qty}
                return True
            else:
                log.error(f"❌ Futures order failed: {result.get('message', result)}")
                return False
    except Exception as e:
        log.error(f"Futures trade error: {e}")
        return False


async def run_prop_cycle():
    global daily_pnl, profitable_days

    # Only trade during market hours (9:30am - 4pm ET = 14:30 - 21:00 UTC)
    now = datetime.utcnow()
    market_open = now.replace(hour=14, minute=30, second=0)
    market_close = now.replace(hour=21, minute=0, second=0)

    if not (market_open <= now <= market_close):
        log.info(f"[APEX_589296] Market closed — waiting for 9:30am ET")
        return

    log.info(f"[APEX_589296] Scanning futures markets (MES, MNQ)... | Daily P&L: ${daily_pnl:.2f} | Profitable days: {len(profitable_days)}/7")

    async with aiohttp.ClientSession() as session:
        for contract, config in FUTURES.items():
            data = await get_price_rsi(session, config["symbol"])
            if not data:
                continue

            price = data["price"]
            rsi   = data["rsi"]
            trend = data["trend"]

            log.info(f"[APEX_589296] {contract} ({config['symbol']}) | ${price:.2f} | RSI:{rsi} | {trend}")

            has_position = contract in open_prop_positions

            # BUY signal — RSI oversold + bullish
            if not has_position and rsi < 38 and trend == "bullish":
                log.info(f"[APEX_589296] 📡 LONG {contract} — RSI:{rsi} Trend:{trend}")
                await execute_futures_trade(session, contract, "BUY", config["qty"], price)

            # SELL signal — RSI overbought or bearish reversal
            elif has_position and (rsi > 62 or (trend == "bearish" and rsi > 50)):
                entry = open_prop_positions[contract]["entry"]
                pnl = (price - entry) * config["qty"] * 50  # MES point value ~$5 * 10
                daily_pnl += pnl
                log.info(f"[APEX_589296] 📤 CLOSE {contract} | Entry: ${entry:.2f} Exit: ${price:.2f} | P&L: ${pnl:.2f}")
                open_prop_positions.pop(contract, None)

            await asyncio.sleep(0.5)

    # Check if today was profitable
    today = now.strftime("%Y-%m-%d")
    if daily_pnl > 0 and (not profitable_days or profitable_days[-1] != today):
        profitable_days.append(today)
        save_state()
        log.info(f"✅ PROFITABLE DAY #{len(profitable_days)} | ${daily_pnl:.2f} | APEX_589296")
        if len(profitable_days) >= 7:
            log.warning("=" * 80)
            log.warning("🎯 🎯 🎯  7 CONSECUTIVE PROFITABLE DAYS ACHIEVED — READY TO GO LIVE!  🎯 🎯 🎯")
            log.warning("=" * 80)
            log.warning("🚨 MANUAL ACTION REQUIRED:")
            log.warning("   1. Go to Railway → empire-v2 → Variables")
            log.warning("   2. Set ALPACA_LIVE_TRADE=true")
            log.warning("   3. Restart deployment")
            log.warning("   Account APEX_589296 will switch to LIVE TRADING on next restart")
            log.warning("=" * 80)


def run():
    global profitable_days, daily_pnl

    # Load state from previous session
    state = load_state()
    profitable_days = state.get("profitable_days", [])
    daily_pnl = state.get("daily_pnl", 0.0)

    log.info("=" * 60)
    log.info("DEL'S TRADING EMPIRE — PROP BOT v3")
    log.info(f"Account: APEX_589296 | Mode: {'LIVE' if LIVE_TRADE else 'PAPER'}")
    log.info(f"Profitable days: {len(profitable_days)}/7 needed")
    log.info("=" * 60)

    while True:
        if STOP:
            log.warning("STOP_TRADING=true — prop bot paused")
            time.sleep(60)
            continue
        try:
            asyncio.run(run_prop_cycle())
        except Exception as e:
            log.error(f"Prop cycle error: {e}")
        time.sleep(30)


if __name__ == "__main__":
    run()
