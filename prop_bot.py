"""
PROP BOT — APEX $25K FUTURES TRADING
======================================
Account: APEX_589296
"""

import os
import time
import logging

log = logging.getLogger("prop_bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

ACCOUNT = os.getenv("TRADOVATE_USER", "APEX_589296")
MODE = os.getenv("TRADOVATE_MODE", "demo")
STOP = os.getenv("STOP_TRADING", "false").lower() == "true"

PROFIT_TARGET = float(os.getenv("PROP_PROFIT_TARGET", 1500))
DAILY_LOSS_LIMIT = float(os.getenv("PROP_DAILY_LOSS_LIMIT", 1000))

CONTRACT_TIERS = [
    (374, 1),
    (749, 2),
    (1124, 3),
    (float("inf"), 4),
]


def get_contract_size(profit: float) -> int:
    for cap, size in CONTRACT_TIERS:
        if profit <= cap:
            return size
    return 1


def run():
    log.info(f"Prop Bot started | Account: {ACCOUNT} | Mode: {MODE}")
    log.info(f"Profit target: ${PROFIT_TARGET} | Daily loss limit: ${DAILY_LOSS_LIMIT}")

    while True:
        if STOP:
            log.warning("STOP_TRADING=true — prop bot paused")
            time.sleep(60)
            continue

        log.info(f"[{ACCOUNT}] Scanning futures markets (MES, MNQ, MGC)...")
        # TODO: connect Tradovate API client here
        time.sleep(30)


if __name__ == "__main__":
    run()
