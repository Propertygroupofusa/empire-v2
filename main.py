"""
DEL'S TRADING EMPIRE — MAIN ORCHESTRATOR
=========================================
Single entry point. Starts all bots as subprocesses, runs health server,
restarts crashed bots automatically.
"""

import os
import sys
import time
import subprocess
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("empire")

PORT = int(os.getenv("PORT", 10000))
STOP_TRADING = os.getenv("STOP_TRADING", "false").lower() == "true"
LIVE_TRADE = os.getenv("ALPACA_LIVE_TRADE", "false").lower() == "true"
LIVE_URL = "api.alpaca.markets" in os.getenv("ALPACA_BASE_URL", "")

BOTS = [
    "prop_bot.py",
    "revenue_bot.py",
    "health_monitor.py",
]

processes = {}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        import json
        body = json.dumps({
            "empire": "ONLINE",
            "live_trading": LIVE_TRADE and LIVE_URL,
            "stop_switch": STOP_TRADING,
            "bots": {n: ("running" if p.poll() is None else "stopped") for n, p in processes.items()}
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def start_health_server():
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()


def launch_bot(script):
    if not os.path.exists(script):
        log.warning(f"Skipping {script} — not found")
        return None
    log.info(f"Launching {script}")
    return subprocess.Popen([sys.executable, script], stdout=sys.stdout, stderr=sys.stderr)


def watchdog():
    backoff = {}
    while True:
        time.sleep(30)
        if STOP_TRADING:
            continue
        for script in BOTS:
            p = processes.get(script)
            if p and p.poll() is not None:
                wait = backoff.get(script, 5)
                log.warning(f"{script} crashed — restarting in {wait}s")
                time.sleep(wait)
                new_p = launch_bot(script)
                if new_p:
                    processes[script] = new_p
                    backoff[script] = min(wait * 2, 300)
            else:
                backoff[script] = 5


if __name__ == "__main__":
    log.info("=" * 50)
    log.info("DEL'S TRADING EMPIRE — STARTING")
    log.info(f"Mode: {'LIVE' if (LIVE_TRADE and LIVE_URL) else 'PAPER'}")
    log.info(f"Kill switch: {'ON' if STOP_TRADING else 'OFF'}")
    log.info("=" * 50)

    if STOP_TRADING:
        start_health_server()
        sys.exit(0)

    if LIVE_TRADE and not LIVE_URL:
        log.error("ALPACA_LIVE_TRADE=true but ALPACA_BASE_URL is still paper. Fix it.")
        sys.exit(1)

    threading.Thread(target=start_health_server, daemon=True).start()

    for script in BOTS:
        p = launch_bot(script)
        if p:
            processes[script] = p

    threading.Thread(target=watchdog, daemon=True).start()
    log.info(f"Empire online. {len(processes)} bots running.")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        for p in processes.values():
            p.terminate()
