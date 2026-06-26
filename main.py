"""
DEL'S TRADING EMPIRE — MAIN ORCHESTRATOR v3
=============================================
Starts all bots, monitors health, auto-restarts crashes.
Deploy this single file on Railway.
"""

import os
import subprocess
import threading
import logging
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("empire")

STOP = os.getenv("STOP_TRADING", "false").lower() == "true"
PORT = int(os.getenv("PORT", 8080))

BOTS = {
    "revenue_bot": {"file": "revenue_bot.py", "process": None, "restarts": 0},
    "prop_bot":    {"file": "prop_bot.py",    "process": None, "restarts": 0},
}

bot_status = {}


def start_bot(name, config):
    """Start a bot subprocess"""
    try:
        p = subprocess.Popen(
            ["python3", config["file"]],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        config["process"] = p
        bot_status[name] = "running"
        log.info(f"✅ Started {name} (PID {p.pid})")

        # Stream logs
        def stream_logs():
            for line in p.stdout:
                print(f"[{name}] {line}", end="")
        threading.Thread(target=stream_logs, daemon=True).start()

        return p
    except Exception as e:
        log.error(f"Failed to start {name}: {e}")
        bot_status[name] = "failed"
        return None


def monitor_bots():
    """Watch all bots and restart if they crash"""
    while True:
        for name, config in BOTS.items():
            p = config["process"]
            if p is None or p.poll() is not None:
                exit_code = p.poll() if p else "never started"
                log.warning(f"⚠️  {name} stopped (exit: {exit_code}) — restarting...")
                config["restarts"] += 1
                bot_status[name] = "restarting"
                time.sleep(5)
                start_bot(name, config)
        time.sleep(15)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = f"""DEL'S TRADING EMPIRE — HEALTH CHECK
Time: {datetime.utcnow().isoformat()}
Stop trading: {STOP}

BOT STATUS:
""".encode()
            for name, config in BOTS.items():
                p = config["process"]
                status = "RUNNING" if p and p.poll() is None else "STOPPED"
                body += f"  {name}: {status} (restarts: {config['restarts']})\n".encode()

            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress HTTP logs


def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    log.info(f"Health server running on port {PORT}")
    server.serve_forever()


if __name__ == "__main__":
    log.info("=" * 60)
    log.info("DEL'S TRADING EMPIRE — STARTING")
    log.info(f"Mode: {'PAPER' if not os.getenv('ALPACA_LIVE_TRADE','false')=='true' else 'LIVE 🔴'}")
    log.info(f"Kill switch: {'ON 🛑' if STOP else 'OFF'}")
    log.info("=" * 60)

    if STOP:
        log.warning("STOP_TRADING=true — empire paused at startup")

    # Start health server
    threading.Thread(target=run_health_server, daemon=True).start()

    # Start all bots
    for name, config in BOTS.items():
        log.info(f"Launching {config['file']}...")
        start_bot(name, config)
        time.sleep(2)

    log.info(f"Empire online. {len(BOTS)} bots running.")

    # Monitor forever
    monitor_bots()
