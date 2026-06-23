"""
REVENUE BOT — 4-STREAM INCOME ENGINE
======================================
Streams: Prop Futures, Stock Grid, Crypto 24/7, Options Spreads
Every trade requires Triple AI confirmation (Claude + GPT-4o + Grok).
"""

import os
import time
import asyncio
import logging
import httpx

log = logging.getLogger("revenue_bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
BASE_URL      = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
LIVE          = os.getenv("ALPACA_LIVE_TRADE", "false").lower() == "true"
STOP          = os.getenv("STOP_TRADING", "false").lower() == "true"

EMPIRE_ACCOUNT_SIZE = float(os.getenv("EMPIRE_ACCOUNT_SIZE", 100000))
HARD_STOP           = float(os.getenv("EMPIRE_HARD_STOP", 80000))
REDUCE_AT           = float(os.getenv("EMPIRE_REDUCE_AT", 90000))

mode = "LIVE" if (LIVE and "paper" not in BASE_URL) else "PAPER"

# ── Stream configs ─────────────────────────────────────────────
STREAMS = {
    "Stock Grid":    {"symbols": ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"], "qty": 1},
    "Crypto 24/7":  {"symbols": ["BTC/USD", "ETH/USD"], "qty": 0.001},
    "Prop Futures":  {"symbols": ["SPY", "QQQ"], "qty": 1},
    "Options Spreads": {"symbols": ["SPY", "QQQ"], "qty": 1},
}


# ── Get account balance from Alpaca ───────────────────────────
async def get_account_balance() -> float:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{BASE_URL}/v2/account",
                headers={
                    "APCA-API-KEY-ID": ALPACA_KEY,
                    "APCA-API-SECRET-KEY": ALPACA_SECRET,
                }
            )
            data = r.json()
            balance = float(data.get("portfolio_value", EMPIRE_ACCOUNT_SIZE))
            log.info(f"Account balance: ${balance:,.2f}")
            return balance
    except Exception as e:
        log.error(f"Failed to get balance: {e}")
        return EMPIRE_ACCOUNT_SIZE


# ── Get market data for a symbol ──────────────────────────────
async def get_market_data(symbol: str) -> dict:
    try:
        # Clean symbol for crypto
        clean = symbol.replace("/", "")
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{BASE_URL}/v2/stocks/{clean}/bars",
                headers={
                    "APCA-API-KEY-ID": ALPACA_KEY,
                    "APCA-API-SECRET-KEY": ALPACA_SECRET,
                },
                params={"timeframe": "5Min", "limit": 20}
            )
            bars = r.json().get("bars", [])
            if not bars:
                return {}

            closes = [b["c"] for b in bars]
            price  = closes[-1]
            avg    = sum(closes) / len(closes)
            trend  = "bullish" if price > avg else "bearish"

            # Simple RSI
            gains  = [closes[i] - closes[i-1] for i in range(1, len(closes)) if closes[i] > closes[i-1]]
            losses = [closes[i-1] - closes[i] for i in range(1, len(closes)) if closes[i] < closes[i-1]]
            avg_gain = sum(gains) / max(len(gains), 1)
            avg_loss = sum(losses) / max(len(losses), 1)
            rs  = avg_gain / max(avg_loss, 0.001)
            rsi = 100 - (100 / (1 + rs))

            return {
                "symbol": symbol,
                "price": round(price, 4),
                "rsi": round(rsi, 1),
                "trend": trend,
                "volume": bars[-1].get("v", 0),
            }
    except Exception as e:
        log.error(f"Market data error for {symbol}: {e}")
        return {}


# ── Generate signal from market data ──────────────────────────
def generate_signal(data: dict, balance: float) -> dict | None:
    if not data:
        return None

    rsi   = data.get("rsi", 50)
    trend = data.get("trend", "neutral")
    price = data.get("price", 0)

    # BUY signal: RSI oversold (< 40) + bullish trend
    if rsi < 40 and trend == "bullish":
        return {
            "symbol": data["symbol"],
            "action": "BUY",
            "price": price,
            "rsi": rsi,
            "trend": trend,
            "volume": data.get("volume", 0),
            "balance": balance,
            "hard_stop": HARD_STOP,
        }

    # SELL signal: RSI overbought (> 65) + bearish trend
    if rsi > 65 and trend == "bearish":
        return {
            "symbol": data["symbol"],
            "action": "SELL",
            "price": price,
            "rsi": rsi,
            "trend": trend,
            "volume": data.get("volume", 0),
            "balance": balance,
            "hard_stop": HARD_STOP,
        }

    return None


# ── Execute trade via Alpaca ───────────────────────────────────
async def execute_trade(signal: dict, qty: float):
    try:
        symbol = signal["symbol"].replace("/", "")
        action = signal["action"]
        side   = "buy" if action == "BUY" else "sell"

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{BASE_URL}/v2/orders",
                headers={
                    "APCA-API-KEY-ID": ALPACA_KEY,
                    "APCA-API-SECRET-KEY": ALPACA_SECRET,
                    "Content-Type": "application/json",
                },
                json={
                    "symbol": symbol,
                    "qty": str(qty),
                    "side": side,
                    "type": "market",
                    "time_in_force": "gtc",
                }
            )
            order = r.json()
            if "id" in order:
                log.info(f"✅ TRADE EXECUTED | {mode} | {action} {qty} {symbol} @ ${signal['price']} | Order ID: {order['id']}")
                return True
            else:
                log.error(f"Trade failed: {order}")
                return False
    except Exception as e:
        log.error(f"Execute trade error: {e}")
        return False


# ── Safety check ──────────────────────────────────────────────
def check_account_safety(balance: float) -> str:
    if balance <= HARD_STOP:
        return "stop"
    if balance <= REDUCE_AT:
        return "reduce"
    return "normal"


# ── Main cycle ────────────────────────────────────────────────
async def run_cycle():
    balance = await get_account_balance()
    safety  = check_account_safety(balance)

    if safety == "stop":
        log.error(f"🛑 HARD STOP — balance ${balance:,.2f} <= ${HARD_STOP}. All trading halted.")
        return

    if safety == "reduce":
        log.warning(f"⚠️  REDUCE MODE — balance ${balance:,.2f} <= ${REDUCE_AT}")

    qty_multiplier = 0.5 if safety == "reduce" else 1.0

    for stream_name, config in STREAMS.items():
        for symbol in config["symbols"]:
            log.info(f"[{stream_name}] Scanning {symbol}...")

            # Get real market data
            data = await get_market_data(symbol)
            if not data:
                continue

            log.info(f"[{stream_name}] {symbol} — Price: ${data.get('price')} | RSI: {data.get('rsi')} | Trend: {data.get('trend')}")

            # Generate signal
            signal = generate_signal(data, balance)
            if not signal:
                continue

            log.info(f"[{stream_name}] 📡 SIGNAL: {signal['action']} {symbol} | RSI: {signal['rsi']} | Sending to Triple AI...")

            # Triple AI confirmation
            try:
                from ai_signal_confirm import confirm_trade
                result = await confirm_trade(signal)

                if result["approved"]:
                    qty = config["qty"] * qty_multiplier
                    await execute_trade(signal, qty)
                else:
                    log.info(f"[{stream_name}] 🚫 Signal rejected by AI | Avg confidence: {result.get('avg_confidence')}%")
            except Exception as e:
                log.error(f"AI confirmation error: {e}")


def run():
    log.info("=" * 60)
    log.info("DEL'S TRADING EMPIRE — REVENUE BOT")
    log.info(f"Mode: {mode}")
    log.info(f"Account: ${EMPIRE_ACCOUNT_SIZE:,.0f} | Hard stop: ${HARD_STOP:,.0f} | Reduce at: ${REDUCE_AT:,.0f}")
    log.info(f"Streams: {', '.join(STREAMS.keys())}")
    log.info("=" * 60)

    while True:
        if STOP:
            log.warning("STOP_TRADING=true — revenue bot paused")
            time.sleep(60)
            continue
        try:
            asyncio.run(run_cycle())
        except Exception as e:
            log.error(f"Cycle error: {e}")
        time.sleep(30)


if __name__ == "__main__":
    run()
