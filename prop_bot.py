import os, time, logging
log = logging.getLogger("prop_bot")

ACCOUNT = os.getenv("TRADOVATE_USER", "APEX_589296")
MODE = os.getenv("TRADOVATE_MODE", "demo")
STOP = os.getenv("STOP_TRADING", "false").lower() == "true"

PROFIT_TARGET = float(os.getenv("PROP_PROFIT_TARGET", 1500))
DAILY_LOSS_LIMIT = float(os.getenv("PROP_DAILY_LOSS_LIMIT", 1000))

log.info(f"Prop Bot started | Account: {ACCOUNT} | Mode: {MODE}")

def run():
    while True:
        if STOP:
            log.warning("STOP_TRADING=true — prop bot paused")
            time.sleep(60)
            continue
        log.info(f"[{ACCOUNT}] Scanning futures markets...")
        time.sleep(30)

if __name__ == "__main__":
    run()
