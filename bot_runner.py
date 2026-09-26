#!/usr/bin/env python3
"""
TRADING BOT RUNNER - Entry point for Railway deployment

This script owns the crypto grid fleet when CRYPTO_STRATEGY_MODE=grid_fleet.
It orchestrates:
- Grid trading bot (multiple branches: DOGE, STX, ETH, BTC, AAVE)
- Mean reversion bot (integrated)
- Shadow learning integration (Delfina learning engine)

All bots run continuously with automatic recovery on errors.

CRITICAL: Event loop must be created and set BEFORE importing modules that use asyncio.
This prevents "Semaphore is bound to a different event loop" errors.
"""

import asyncio
import logging
import os
import time

from crypto_strategy_config import UNCONFIGURED, get_crypto_strategy_mode

# Setup logging for Railway
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)
log = logging.getLogger(__name__)

def run_with_supervision(run_bot, sleep=time.sleep, max_backoff_seconds=60):
    backoff_seconds = 5

    while True:
        try:
            run_bot()
            log.error("Grid Fleet stopped unexpectedly; restarting in %s seconds", backoff_seconds)
        except Exception:
            log.exception("Grid Fleet crashed; restarting in %s seconds", backoff_seconds)

        sleep(backoff_seconds)
        backoff_seconds = min(backoff_seconds * 2, max_backoff_seconds)


def _load_and_run_grid_bot():
    import crypto_grid_bot

    log.info("✓ Crypto grid bot module loaded")
    crypto_grid_bot.run()


def main():
    log.info("=" * 70)
    log.info("TRADING BOT RUNNER - Dedicated crypto strategy service")
    log.info("=" * 70)

    strategy_mode = get_crypto_strategy_mode()
    if strategy_mode == UNCONFIGURED:
        # NOT "owned by the web service" - owned by nobody. Saying otherwise
        # sends the reader to check a service that is also doing nothing,
        # which is exactly how the 'delfina_scalping' typo stayed alive for
        # days while both services quietly deferred to each other.
        log.error(
            "CRYPTO_STRATEGY_MODE is missing or not a known strategy, so NO crypto "
            "loop is running anywhere - not here, and not on the web service. Set "
            "CRYPTO_STRATEGY_MODE=grid_fleet and SERVICE_ROLE=crypto-trading on THIS "
            "service, and leave CRYPTO_STRATEGY_MODE UNSET on the web service - "
            "every mode spends the same Coinbase balance, so a second strategy "
            "there would trade the money this fleet is already using."
        )
        return
    if strategy_mode != "grid_fleet":
        log.info(
            "CRYPTO_STRATEGY_MODE=%r is owned by the web service; dedicated runner exiting",
            strategy_mode,
        )
        return

    if os.getenv("STOP_TRADING", "false").lower() == "true":
        log.warning("STOP_TRADING=true; Grid Fleet will remain stopped")
        return

    if not os.getenv("COINBASE_API_KEY_NAME") or not os.getenv("COINBASE_API_PRIVATE_KEY"):
        log.error("Coinbase API credentials are not set; Grid Fleet will remain stopped")
        return

    try:
        # CRITICAL FIX: Create and set event loop BEFORE importing modules
        # This ensures all asyncio objects created during imports are bound
        # to the correct event loop, preventing "bound to a different event loop" errors
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        log.info("✓ Event loop initialized (before module imports)")

        log.info("✓ Coinbase credentials verified")
        log.info("Starting supervised Grid Fleet loop...")

        run_with_supervision(_load_and_run_grid_bot)

    except KeyboardInterrupt:
        log.info("\nBot stopped by user")
    except Exception as e:
        log.error(f"✗ Fatal error: {e}", exc_info=True)

if __name__ == "__main__":
    main()
