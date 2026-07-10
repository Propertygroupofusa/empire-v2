"""
DEL'S TRADING EMPIRE v2 — ORCHESTRATOR
========================================
Starts and manages all bots, handles crashes + restarts
- health_monitor (always on)
- prop_bot (APEX $25K futures trading)
- content_bot (AI video revenue system)
- video_revenue_api (API server)
"""

import os
import time
import logging
import subprocess
import signal
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("orchestrator")

# Configuration
STOP_TRADING = os.getenv("STOP_TRADING", "false").lower() == "true"
ENABLE_PROP_BOT = os.getenv("ENABLE_PROP_BOT", "true").lower() == "true"
ENABLE_CONTENT_BOT = os.getenv("ENABLE_CONTENT_BOT", "true").lower() == "true"
ENABLE_API = os.getenv("ENABLE_API", "true").lower() == "true"
RESTART_DELAY = int(os.getenv("RESTART_DELAY", 5))

# Track processes
processes = {}
shutdown_requested = False

def signal_handler(sig, frame):
    """Handle graceful shutdown"""
    global shutdown_requested
    log.warning("Shutdown signal received — stopping all bots...")
    shutdown_requested = True
    sys.exit(0)

def start_process(name, command):
    """Start a bot process and track it"""
    if shutdown_requested:
        return None

    log.info(f"Starting {name}...")
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )
        processes[name] = proc
        log.info(f"✅ {name} started (PID: {proc.pid})")
        return proc
    except Exception as e:
        log.error(f"❌ Failed to start {name}: {e}")
        return None

def monitor_processes():
    """Monitor all running processes and restart if they crash"""
    log.info("=" * 60)
    log.info("ORCHESTRATOR STARTED")
    log.info(f"STOP_TRADING: {STOP_TRADING}")
    log.info(f"PROP_BOT enabled: {ENABLE_PROP_BOT}")
    log.info(f"CONTENT_BOT enabled: {ENABLE_CONTENT_BOT}")
    log.info(f"API enabled: {ENABLE_API}")
    log.info("=" * 60)

    # Initial startup
    if ENABLE_PROP_BOT and not STOP_TRADING:
        start_process("PROP_BOT", "python prop_bot.py")

    if ENABLE_CONTENT_BOT:
        start_process("CONTENT_BOT", "python content_bot.py")

    if ENABLE_API:
        start_process("API", "uvicorn video_revenue_api:app --host 0.0.0.0 --port 8000")

    start_process("HEALTH_MONITOR", "python health_monitor.py")

    # Continuous monitoring
    while not shutdown_requested:
        time.sleep(10)

        for name, proc in list(processes.items()):
            if proc and proc.poll() is not None:  # Process has exited
                log.warning(f"⚠️  {name} crashed (exit code: {proc.returncode})")
                log.info(f"Restarting {name} in {RESTART_DELAY}s...")
                time.sleep(RESTART_DELAY)

                # Determine restart command
                if name == "PROP_BOT":
                    start_process(name, "python prop_bot.py")
                elif name == "CONTENT_BOT":
                    start_process(name, "python content_bot.py")
                elif name == "API":
                    start_process(name, "uvicorn video_revenue_api:app --host 0.0.0.0 --port 8000")
                elif name == "HEALTH_MONITOR":
                    start_process(name, "python health_monitor.py")

def cleanup():
    """Kill all child processes on exit"""
    log.info("Cleaning up processes...")
    for name, proc in processes.items():
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
                log.info(f"✅ {name} terminated")
            except:
                proc.kill()
                log.warning(f"⚠️  {name} force killed")

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        monitor_processes()
    except Exception as e:
        log.error(f"Orchestrator error: {e}")
    finally:
        cleanup()
