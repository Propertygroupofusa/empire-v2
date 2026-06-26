"""
DEL'S TRADING EMPIRE — REVENUE BOT v4
=======================================
FIXED: Correct Alpaca data API format for stocks and crypto
FIXED: Added feed=iex parameter
FIXED: Correct response parsing (bars is a dict keyed by symbol)
"""

import os
import asyncio
import logging
import time
from datetime import datetime
import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("revenue_bot")

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
BASE_URL      = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
LIVE_TRADE    = os.getenv("ALPACA_LIVE_TRADE", "false").lower() == "true"
STOP          = os.getenv("STOP_TRADING", "false").lower() == "true"
HARD_STOP     = float(os.getenv("EMPIRE_HARD_STOP", "80000"))
REDUCE_AT     = float(os.getenv("EMPIRE_REDUCE_AT", "90000"))
ACCOUNT_SIZE  = float(os.getenv("EMPIRE_ACCOUNT_SIZE", "100000"))

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type": "application/json"
}

DATA_HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}

STREAMS = {
    "Stock Grid": {
        "symbols": ["SPY", "QQQ", "NVDA", "AAPL", "MSFT"],
        "qty": 1,
        "asset_class": "us_equity",
        "rsi_buy": 35,
        "rsi_sell": 65,
    },
    "Crypto 24/7": {
        "symbols": ["BTC/USD", "ETH/USD"],
        "qty": 0.001,
        "asset_class": "crypto",
        "rsi_buy": 32,
        "rsi_sell": 68,
    },
}

open_positions = {}
daily_trades = 0
MAX_DAILY_TRADES = 10


async def get_account(session):
    try:
        async with session.get(
            f"{BASE_URL}/v2/account", headers=HEADERS
        ) as r:
            data = await r.json()
            if r.status == 200:
                bal = float(data.get("portfolio_value", ACCOUNT_SIZE))
                log.info(f"💰 Account: ${bal:,.2f}")
                return bal
            log.error(f"Account error {r.status}: {data}")
    except Exception as e:
        log.error(f"Account error: {e}")
    return ACCOUNT_SIZE


async def get_price_and_rsi(session, symbol, asset_class):
    try:
        if asset_class == "crypto":
            sym_encoded = symbol.replace("/", "%2F")
            url = (
                f"https://data.alpaca.markets/v1beta3/crypto/us/bars"
                f"?symbols={sym_encoded}&timeframe=5Min&limit=20"
            )
        else:
            # FIXED: use /v2/stocks/bars (plural) with symbols param + feed=iex
            url = (
                f"https://data.alpaca.markets/v2/stocks/bars"
                f"?symbols={symbol}&timeframe=5Min&limit=20&feed=iex"
            )

        async with session.get(url, headers=DATA_HEADERS) as r:
            raw = await r.text()
            if r.status != 200:
                log.warning(f"[{symbol}] Data error {r.status}: {raw[:300]}")
                return None

            data = await r.json()

            # FIXED: response is always {"bars": {"SYMBOL": [...]}}
            bars_dict = data.get("bars", {})
            if isinstance(bars_dict, dict):
                bars = bars_dict.get(symbol, [])
            else:
                bars = bars_dict  # fallback

            if not bars or len(bars) < 14:
                log.warning(f"[{symbol}] Only {len(bars) if bars else 0} bars returned — need 14")
                return None

            closes = [b["c"] for b in bars]
            price  = closes[-1]

            # RSI
            gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
            losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
            ag = sum(gains[-14:])  / 14
            al = sum(losses[-14:]) / 14
            rs  = ag / al if al > 0 else 100
            rsi = round(100 - (100 / (1 + rs)), 1)

            # Trend
            sma5  = sum(closes[-5:])  / 5
            sma10 = sum(closes[-10:]) / 10
            trend = "bullish" if sma5 > sma10 else "bearish"

            log.info(f"[{symbol}] ✅ ${price:.2f} | RSI:{rsi} | {trend}")
            return {"price": price, "rsi": rsi, "trend": trend}

    except Exception as e:
        log.error(f"[{symbol}] Exception: {e}")
        return None


async def get_open_positions(session):
    try:
        async with session.get(f"{BASE_URL}/v2/positions", headers=HEADERS) as r:
            if r.status == 200:
                positions = await r.json()
                return {p["symbol"]: p for p in positions}
    except Exception as e:
        log.error(f"Positions error: {e}")
    return {}


async def execute_trade(session, symbol, action, qty, price, stream_name):
    global daily_trades
    if daily_trades >= MAX_DAILY_TRADES:
        log.warning(f"Daily limit {MAX_DAILY_TRADES} reached")
        return False

    side  = "buy" if action == "BUY" else "sell"
    tif   = "gtc" if "/" in symbol else "day"
    order = {"symbol": symbol, "qty": str(qty), "side": side,
             "type": "market", "time_in_force": tif}
    mode  = "LIVE 🔴" if LIVE_TRADE else "PAPER 📄"

    try:
        async with session.post(
            f"{BASE_URL}/v2/orders", headers=HEADERS, json=order
        ) as r:
            result = await r.json()
            if r.status in (200, 201):
                daily_trades += 1
                log.info(
                    f"✅ TRADE #{daily_trades} | {mode} | {action} {qty} "
                    f"{symbol} @ ${price:.2f} | {stream_name}"
                )
                open_positions[symbol] = {
                    "action": action, "qty": qty,
                    "entry": price, "stream": stream_name
                }
                return True
            log.error(f"❌ Order failed {symbol}: {result.get('message', result)}")
    except Exception as e:
        log.error(f"Trade error {symbol}: {e}")
    return False


async def close_position(session, symbol, price, reason):
    try:
        async with session.delete(
            f"{BASE_URL}/v2/positions/{symbol}", headers=HEADERS
        ) as r:
            if r.status in (200, 204):
                log.info(f"📤 CLOSED {symbol} @ ${price:.2f} | {reason}")
                open_positions.pop(symbol, None)
                return True
    except Exception as e:
        log.error(f"Close error {symbol}: {e}")
    return False


async def run_cycle():
    global daily_trades

    if datetime.utcnow().hour == 13 and datetime.utcnow().minute < 2:
        daily_trades = 0
        log.info("🔄 Daily counter reset")

    async with aiohttp.ClientSession() as session:
        balance = await get_account(session)

        if balance <= HARD_STOP:
            log.error(f"🛑 HARD STOP ${balance:,.2f}")
            return

        qty_mult = 0.5 if balance <= REDUCE_AT else 1.0
        current  = await get_open_positions(session)
        log.info(f"Open positions: {list(current.keys()) or 'None'}")

        for stream_name, config in STREAMS.items():
            for symbol in config["symbols"]:
                log.info(f"[{stream_name}] Scanning {symbol}...")

                data = await get_price_and_rsi(
                    session, symbol, config["asset_class"]
                )
                if not data:
                    continue

                price = data["price"]
                rsi   = data["rsi"]
                trend = data["trend"]

                has_pos = symbol in current or symbol in open_positions

                if not has_pos and rsi < config["rsi_buy"] and trend == "bullish":
                    qty = round(config["qty"] * qty_mult, 6)
                    log.info(f"[{stream_name}] 📡 BUY {symbol} RSI:{rsi}")
                    await execute_trade(session, symbol, "BUY", qty, price, stream_name)

                elif has_pos and rsi > config["rsi_sell"]:
                    log.info(f"[{stream_name}] 📡 SELL {symbol} RSI:{rsi}")
                    await close_position(session, symbol, price, f"RSI:{rsi}")

                elif not has_pos and rsi > config["rsi_sell"] and trend == "bearish":
                    qty = round(config["qty"] * qty_mult, 6)
                    log.info(f"[{stream_name}] 📡 SHORT {symbol} RSI:{rsi}")
                    await execute_trade(session, symbol, "SELL", qty, price, stream_name)

                else:
                    log.info(
                        f"[{stream_name}] {symbol} — No signal | "
                        f"RSI:{rsi} needs <{config['rsi_buy']} or >{config['rsi_sell']} | {trend}"
                    )

                await asyncio.sleep(0.5)


def run():
    log.info("=" * 60)
    log.info("DEL'S TRADING EMPIRE — REVENUE BOT v4")
    log.info(f"Mode: {'LIVE 🔴' if LIVE_TRADE else 'PAPER 📄'}")
    log.info(f"Key present: {bool(ALPACA_KEY)} | URL: {BASE_URL}")
    log.info("=" * 60)

    cycle = 0
    while True:
        if STOP:
            log.warning("STOP_TRADING=true — paused")
            time.sleep(60)
            continue
        cycle += 1
        log.info(f"--- CYCLE {cycle} ---")
        try:
            asyncio.run(run_cycle())
        except Exception as e:
            log.error(f"Cycle {cycle} error: {e}")
        log.info("⏳ Next scan in 30s...")
        time.sleep(30)


if __name__ == "__main__":
    run()
