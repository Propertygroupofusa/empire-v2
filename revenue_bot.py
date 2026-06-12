import os, time, logging
log = logging.getLogger("revenue_bot")

ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
LIVE = os.getenv("ALPACA_LIVE_TRADE", "false").lower() == "true"
STOP = os.getenv("STOP_TRADING", "false").lower() == "true"
HARD_STOP = float(os.getenv("EMPIRE_HARD_STOP", 80000))

mode = "LIVE" if (LIVE and "paper" not in BASE_URL) else "PAPER"
log.info(f"Revenue Bot started | Mode: {mode}")

STREAMS = ["Prop Futures", "Stock Grid", "Crypto 24/7", "Options Spreads"]

def run():
    while True:
        if STOP:
            log.warning("STOP_TRADING=true — revenue bot paused")
            time.sleep(60)
            continue
        for stream in STREAMS:
            log.info(f"[{stream}] Scanning signals...")
        time.sleep(30)

if __name__ == "__main__":
    run()
