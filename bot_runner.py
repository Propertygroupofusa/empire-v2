#!/usr/bin/env python3
"""
TRADING BOT RUNNER - Entry point for Railway deployment

This script starts the crypto grid bot which orchestrates:
- Grid trading bot (multiple branches: DOGE, STX, ETH, BTC, AAVE)
- Mean reversion bot (integrated)
- Shadow learning integration (Delfina learning engine)

All bots run continuously with automatic recovery on errors.
"""

import logging
import sys

# Setup logging for Railway
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)
log = logging.getLogger(__name__)

def main():
    log.info("=" * 70)
    log.info("TRADING BOT RUNNER - Starting crypto grid + mean reversion bots")
    log.info("=" * 70)
    
    try:
        import crypto_grid_bot
        log.info("✓ Crypto grid bot module loaded")
        
        # Verify Coinbase credentials are set
        import crypto_btc_compound_bot as engine
        if not engine.COINBASE_API_KEY_NAME or not engine.COINBASE_API_PRIVATE_KEY:
            log.error("✗ Coinbase API credentials not set in environment")
            log.error("  Required: COINBASE_API_KEY_NAME and COINBASE_API_PRIVATE_KEY")
            sys.exit(1)
        
        log.info("✓ Coinbase credentials verified")
        log.info("Starting bot loop...")
        
        # Start the grid bot (includes mean reversion engine)
        crypto_grid_bot.run()
        
    except KeyboardInterrupt:
        log.info("\nBot stopped by user")
        sys.exit(0)
    except Exception as e:
        log.error(f"✗ Fatal error: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
