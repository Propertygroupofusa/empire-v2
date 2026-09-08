#!/usr/bin/env python3
"""
Grid Bot Capital Scaling Utility

Scales up grid bot capital allocations by a specified factor.
Usage: python scale_grid_capital.py [scale_factor]

Example:
  python scale_grid_capital.py 1.25  # Scale up by 25%
  python scale_grid_capital.py 1.50  # Scale up by 50%
"""

import asyncio
import sys
import logging
from database import AsyncSessionLocal
from models import CryptoGridBranch
from sqlalchemy import select
import crypto_grid_bot

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("scale_grid_capital")


async def main():
    if len(sys.argv) < 2:
        scale_factor = 1.25  # Default: 25% increase
        log.info(f"No scale factor provided, using default: {scale_factor}")
    else:
        try:
            scale_factor = float(sys.argv[1])
        except ValueError:
            log.error(f"Invalid scale factor: {sys.argv[1]}")
            sys.exit(1)

    if scale_factor <= 1.0:
        log.error("Scale factor must be > 1.0")
        sys.exit(1)

    log.info("=" * 60)
    log.info(f"Grid Bot Capital Scaling: {scale_factor}x ({(scale_factor-1)*100:.0f}% increase)")
    log.info("=" * 60)

    result = await crypto_grid_bot.scale_grid_bot_capital(scale_factor)

    if result.get("status") == "success":
        log.info(f"\n✅ SCALING SUCCESSFUL")
        log.info(f"Branches scaled: {result['branches_scaled']}")
        log.info(f"Old total allocation: ${result['total_old_allocation']:,.2f}")
        log.info(f"New total allocation: ${result['total_new_allocation']:,.2f}")
        log.info(f"Total increase: ${result['total_increase']:,.2f}")
        log.info("\nper-Branch Updates:")
        for update in result["updates"]:
            log.info(
                f"  {update['bot_name']} ({update['product_id']}): "
                f"${update['old_allocated_usd']:,.2f} → ${update['new_allocated_usd']:,.2f} "
                f"(+{update['increase_pct']:.1f}%)"
            )
        log.info("\n" + "=" * 60)
        log.info("Grid bot will use new allocations on next cycle.")
        log.info("=" * 60)
        sys.exit(0)
    else:
        log.error(f"❌ SCALING FAILED: {result.get('error')}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
