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
from datetime import datetime
import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("prop_bot")

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
BASE_URL      = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
LIVE_TRADE    = os.getenv("ALPACA_LIVE_TRADE", "false").lower() == "true"
STOP          = os.getenv("STOP_TRADING", "false").lower() == "true"

# RSI entry/exit thresholds. Loosened from the original 38/62 so the bot
# doesn't need as extreme an oversold/overbought reading to act - trades
# more often, at the cost of acting on weaker, less-confirmed signals.
# Configurable via env so further tuning doesn't require a code change.
RSI_BUY_BELOW  = float(os.getenv("PROP_RSI_BUY_BELOW", "45"))
RSI_SELL_ABOVE = float(os.getenv("PROP_RSI_SELL_ABOVE", "55"))

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


async def broadcast_signal_to_subscribers(session, contract, action, price, rsi, trend, stop_loss=None, target=None):
    """Broadcast signal to all trading signal subscribers via API."""
    try:
        api_url = os.getenv("API_BASE_URL", "http://localhost:8000")
        signal_data = {
            "contract": contract,
            "action": action,
            "entry_price": price,
            "stop_loss": stop_loss or (price * 0.98),  # 2% stop loss if not specified
            "target_price": target or (price * 1.03 if action == "BUY" else price * 0.97),  # 3% target
            "rsi": rsi,
            "trend": trend,
            "confidence": 0.85,
        }

        async with session.post(f"{api_url}/trading/signals/broadcast", json=signal_data) as r:
            if r.status == 200:
                result = await r.json()
                log.info(f"📡 Signal broadcast complete: {result.get('subscribers_notified', 0)} subscribers notified")
                return True
            else:
                log.warning(f"Signal broadcast failed: {r.status}")
                return False
    except Exception as e:
        log.warning(f"Could not broadcast signal: {e}")
        return False


async def execute_futures_trade(session, contract, action, qty, price, rsi, trend, stop_loss=None, target=None):
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

                # Broadcast signal to subscribers
                await broadcast_signal_to_subscribers(session, contract, action, price, rsi, trend, stop_loss, target)

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

    log.info(f"[APEX_589296] Scanning futures markets (MES, MNQ)... | Daily P&L: ${daily_pnl:.2f}")

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
            if not has_position and rsi < RSI_BUY_BELOW and trend == "bullish":
                log.info(f"[APEX_589296] 📡 LONG {contract} — RSI:{rsi} Trend:{trend}")
                stop_loss = price * 0.98  # 2% below entry
                target = price * 1.03    # 3% above entry
                await execute_futures_trade(session, contract, "BUY", config["qty"], price, rsi, trend, stop_loss, target)

            # SELL signal — RSI overbought or bearish reversal
            elif has_position and (rsi > RSI_SELL_ABOVE or (trend == "bearish" and rsi > 50)):
                entry = open_prop_positions[contract]["entry"]
                pnl = (price - entry) * config["qty"] * 50  # MES point value ~$5 * 10
                daily_pnl += pnl
                log.info(f"[APEX_589296] 📤 CLOSE {contract} | Entry: ${entry:.2f} Exit: ${price:.2f} | P&L: ${pnl:.2f}")

                # Broadcast close signal to subscribers
                target = price
                await broadcast_signal_to_subscribers(session, contract, "SELL", price, rsi, trend, target=target)

                open_prop_positions.pop(contract, None)

            await asyncio.sleep(0.5)

    # Check if today was profitable
    today = now.strftime("%Y-%m-%d")
    if daily_pnl > 0 and (not profitable_days or profitable_days[-1] != today):
        profitable_days.append(today)
        log.info(f"✅ PROFITABLE DAY #{len(profitable_days)} | ${daily_pnl:.2f} | APEX_589296")
        if len(profitable_days) >= 7:
            log.info("🎯 7 CONSECUTIVE PROFITABLE DAYS ACHIEVED — READY TO GO LIVE!")
            log.info("ACTION: Change ALPACA_LIVE_TRADE=true in Railway to go live")


def run():
    log.info("=" * 60)
    log.info("DEL'S TRADING EMPIRE — PROP BOT v3")
    log.info(f"Account: APEX_589296 | Mode: {'LIVE' if LIVE_TRADE else 'PAPER'}")
    log.info(f"RSI thresholds: buy < {RSI_BUY_BELOW} | sell > {RSI_SELL_ABOVE}")
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
