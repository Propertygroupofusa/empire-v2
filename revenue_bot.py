"""
DEL'S TRADING EMPIRE — REVENUE BOT v3
=======================================
Fixed trade execution — signals now actually fire trades.
Streams: Stock Grid, Crypto 24/7, Options Spreads
Mode: Paper (flip ALPACA_LIVE_TRADE=true to go live)
"""

import os
import asyncio
import logging
import time
from datetime import datetime, timedelta
import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("revenue_bot")

# ── CONFIG ────────────────────────────────────────────────────────
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

# ── TRADING STREAMS ───────────────────────────────────────────────
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

# ── TRACK OPEN POSITIONS TO AVOID DUPLICATES ─────────────────────
open_positions = {}
daily_trades = 0
MAX_DAILY_TRADES = 10


# ── ALPACA API ────────────────────────────────────────────────────

async def get_account(session):
    try:
        async with session.get(f"{BASE_URL}/v2/account", headers=HEADERS) as r:
            if r.status == 200:
                data = await r.json()
                return float(data.get("portfolio_value", ACCOUNT_SIZE))
    except Exception as e:
        log.error(f"Account fetch error: {e}")
    return ACCOUNT_SIZE


async def get_price_and_rsi(session, symbol, asset_class):
    """Get current price and calculate RSI from bar data"""
    try:
        if asset_class == "crypto":
            sym_encoded = symbol.replace("/", "%2F")
            url = f"https://data.alpaca.markets/v1beta3/crypto/us/bars?symbols={sym_encoded}&timeframe=5Min&limit=20"
        else:
            url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=5Min&limit=20"

        async with session.get(url, headers=HEADERS) as r:
            if r.status != 200:
                log.warning(f"Price fetch {symbol}: HTTP {r.status}")
                return None

            data = await r.json()

            if asset_class == "crypto":
                bars = data.get("bars", {}).get(symbol, [])
            else:
                bars = data.get("bars", [])

            if not bars or len(bars) < 14:
                log.warning(f"{symbol}: Not enough bars ({len(bars) if bars else 0})")
                return None

            closes = [b["c"] for b in bars]
            price = closes[-1]

            # RSI calculation
            gains, losses = [], []
            for i in range(1, len(closes)):
                diff = closes[i] - closes[i-1]
                gains.append(max(diff, 0))
                losses.append(max(-diff, 0))

            avg_gain = sum(gains[-14:]) / 14
            avg_loss = sum(losses[-14:]) / 14
            rs = avg_gain / avg_loss if avg_loss > 0 else 100
            rsi = 100 - (100 / (1 + rs))

            # Trend
            sma5 = sum(closes[-5:]) / 5
            sma10 = sum(closes[-10:]) / 10
            trend = "bullish" if sma5 > sma10 else "bearish"

            return {"price": price, "rsi": round(rsi, 1), "trend": trend, "symbol": symbol}

    except Exception as e:
        log.error(f"Price/RSI error for {symbol}: {e}")
        return None


async def get_open_positions(session):
    """Get current open positions from Alpaca"""
    try:
        async with session.get(f"{BASE_URL}/v2/positions", headers=HEADERS) as r:
            if r.status == 200:
                positions = await r.json()
                return {p["symbol"]: p for p in positions}
    except Exception as e:
        log.error(f"Positions fetch error: {e}")
    return {}


async def execute_trade(session, symbol, action, qty, price, stream_name):
    """Execute a real trade via Alpaca"""
    global daily_trades

    if daily_trades >= MAX_DAILY_TRADES:
        log.warning(f"Daily trade limit ({MAX_DAILY_TRADES}) reached — skipping {symbol}")
        return False

    side = "buy" if action == "BUY" else "sell"

    order = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": "gtc" if "/" in symbol else "day",
    }

    mode = "LIVE" if LIVE_TRADE else "PAPER"

    try:
        async with session.post(f"{BASE_URL}/v2/orders", headers=HEADERS, json=order) as r:
            result = await r.json()

            if r.status in (200, 201):
                daily_trades += 1
                log.info(f"✅ TRADE EXECUTED | {mode} | {action} {qty} {symbol} @ ${price:.2f} | Order ID: {result.get('id', 'N/A')} | Stream: {stream_name}")
                open_positions[symbol] = {"action": action, "qty": qty, "entry": price, "stream": stream_name}
                return True
            else:
                log.error(f"❌ Order failed for {symbol}: {result.get('message', result)}")
                return False

    except Exception as e:
        log.error(f"Trade execution error for {symbol}: {e}")
        return False


async def close_position(session, symbol, qty, price, stream_name, reason):
    """Close an existing position"""
    try:
        async with session.delete(f"{BASE_URL}/v2/positions/{symbol}", headers=HEADERS) as r:
            if r.status in (200, 204):
                log.info(f"📤 POSITION CLOSED | {symbol} | Reason: {reason} | Price: ${price:.2f}")
                open_positions.pop(symbol, None)
                return True
    except Exception as e:
        log.error(f"Close position error for {symbol}: {e}")
    return False


# ── MAIN TRADING CYCLE ────────────────────────────────────────────

async def run_cycle():
    global daily_trades

    # Reset daily trades at market open
    if datetime.utcnow().hour == 13 and datetime.utcnow().minute < 2:  # 9am ET
        daily_trades = 0
        log.info("🔄 Daily trade counter reset")

    async with aiohttp.ClientSession() as session:
        # Check account balance
        balance = await get_account(session)
        log.info(f"💰 Portfolio: ${balance:,.2f} | Daily trades: {daily_trades}/{MAX_DAILY_TRADES}")

        # Safety checks
        if balance <= HARD_STOP:
            log.error(f"🛑 HARD STOP — Balance ${balance:,.2f} <= ${HARD_STOP:,.2f} — ALL TRADING HALTED")
            return

        qty_mult = 0.5 if balance <= REDUCE_AT else 1.0
        if qty_mult < 1.0:
            log.warning(f"⚠️ REDUCE MODE — Balance ${balance:,.2f} <= ${REDUCE_AT:,.2f}")

        # Get current positions
        current_positions = await get_open_positions(session)

        # Run each stream
        for stream_name, config in STREAMS.items():
            for symbol in config["symbols"]:
                log.info(f"[{stream_name}] Scanning {symbol}...")

                data = await get_price_and_rsi(session, symbol, config["asset_class"])
                if not data:
                    continue

                price = data["price"]
                rsi   = data["rsi"]
                trend = data["trend"]

                log.info(f"[{stream_name}] {symbol} | Price: ${price:.2f} | RSI: {rsi} | Trend: {trend}")

                # Check if we already have this position open
                has_position = symbol in current_positions or symbol in open_positions

                # ── BUY SIGNAL ──────────────────────────────────────
                if not has_position and rsi < config["rsi_buy"] and trend == "bullish":
                    qty = round(config["qty"] * qty_mult, 6)
                    log.info(f"[{stream_name}] 📡 BUY SIGNAL — {symbol} RSI:{rsi} Trend:{trend} — Executing...")
                    await execute_trade(session, symbol, "BUY", qty, price, stream_name)

                # ── SELL SIGNAL (close long) ────────────────────────
                elif has_position and rsi > config["rsi_sell"]:
                    log.info(f"[{stream_name}] 📡 SELL SIGNAL — {symbol} RSI:{rsi} — Closing position...")
                    await close_position(session, symbol, config["qty"], price, stream_name, f"RSI overbought ({rsi})")

                # ── SHORT SIGNAL (bearish + overbought) ────────────
                elif not has_position and rsi > config["rsi_sell"] and trend == "bearish":
                    qty = round(config["qty"] * qty_mult, 6)
                    log.info(f"[{stream_name}] 📡 SHORT SIGNAL — {symbol} RSI:{rsi} Trend:{trend} — Executing...")
                    await execute_trade(session, symbol, "SELL", qty, price, stream_name)

                else:
                    log.info(f"[{stream_name}] {symbol} — No signal (RSI:{rsi} not in range, trend:{trend})")

                await asyncio.sleep(0.5)  # Rate limit between symbols


def run():
    mode = "LIVE 🔴" if LIVE_TRADE else "PAPER 📄"
    log.info("=" * 60)
    log.info("DEL'S TRADING EMPIRE — REVENUE BOT v3")
    log.info(f"Mode: {mode}")
    log.info(f"Streams: {', '.join(STREAMS.keys())}")
    log.info(f"Account size: ${ACCOUNT_SIZE:,.0f}")
    log.info(f"Hard stop: ${HARD_STOP:,.0f} | Reduce at: ${REDUCE_AT:,.0f}")
    log.info("=" * 60)

    while True:
        if STOP:
            log.warning("STOP_TRADING=true — bot paused")
            time.sleep(60)
            continue
        try:
            asyncio.run(run_cycle())
        except Exception as e:
            log.error(f"Cycle error: {e}")
        log.info("⏳ Waiting 30 seconds before next scan...")
        time.sleep(30)


if __name__ == "__main__":
    run()
