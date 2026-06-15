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

log = logging.getLogger("revenue_bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
LIVE = os.getenv("ALPACA_LIVE_TRADE", "false").lower() == "true"
STOP = os.getenv("STOP_TRADING", "false").lower() == "true"

EMPIRE_ACCOUNT_SIZE = float(os.getenv("EMPIRE_ACCOUNT_SIZE", 100000))
HARD_STOP = float(os.getenv("EMPIRE_HARD_STOP", 80000))
REDUCE_AT = float(os.getenv("EMPIRE_REDUCE_AT", 90000))

STREAMS = ["Prop Futures", "Stock Grid", "Crypto 24/7", "Options Spreads"]

mode = "LIVE" if (LIVE and "paper" not in BASE_URL) else "PAPER"


def check_account_safety(current_balance: float) -> str:
    """Returns: 'normal', 'reduce', or 'stop'"""
    if current_balance <= HARD_STOP:
        return "stop"
    if current_balance <= REDUCE_AT:
        return "reduce"
    return "normal"


async def evaluate_signal(stream: str, signal: dict) -> bool:
    """
    Calls Triple AI confirmation before allowing a trade.
    Returns True only if all 3 AIs approve.
    """
    try:
        from ai_signal_confirm import confirm_trade
        result = await confirm_trade(signal)
        return result["approved"]
    except ImportError:
        log.warning("ai_signal_confirm not available — skipping AI gate")
        return False


async def run_cycle():
    current_balance = EMPIRE_ACCOUNT_SIZE  # TODO: pull real balance from Alpaca
    safety = check_account_safety(current_balance)

    if safety == "stop":
        log.error(f"🛑 HARD STOP — balance ${current_balance} <= ${HARD_STOP}. All trading halted.")
        return
    elif safety == "reduce":
        log.warning(f"⚠️  REDUCE MODE — balance ${current_balance} <= ${REDUCE_AT}. Position sizes reduced.")

    for stream in STREAMS:
        log.info(f"[{stream}] Scanning signals...")
        # TODO: generate real signal dict per stream
        example_signal = {
            "symbol": "SPY",
            "action": "BUY",
            "price": 445.50,
            "rsi": 52,
            "trend": "bullish",
            "volume": "above average",
            "balance": current_balance,
            "hard_stop": HARD_STOP,
        }
        # approved = await evaluate_signal(stream, example_signal)
        # if approved:
        #     execute_trade(...)


def run():
    log.info(f"Revenue Bot started | Mode: {mode}")
    log.info(f"Account size: ${EMPIRE_ACCOUNT_SIZE} | Hard stop: ${HARD_STOP} | Reduce at: ${REDUCE_AT}")
    log.info(f"Streams: {', '.join(STREAMS)}")

    while True:
        if STOP:
            log.warning("STOP_TRADING=true — revenue bot paused")
            time.sleep(60)
            continue

        asyncio.run(run_cycle())
        time.sleep(30)


if __name__ == "__main__":
    run()
