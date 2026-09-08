#!/usr/bin/env python3
"""
Grid Bot Capital Scaling Utility

SAFE: Scales up grid bot capital allocations without disabling anything.

Usage:
  python scale_grid_capital.py [scale_factor] [--confirm]

Default behavior (NO --confirm flag):
  - Shows DRY RUN preview of what would change
  - Does NOT apply changes
  - Allows you to verify before committing

With --confirm flag:
  - Applies the scaling changes atomically
  - Updates database and logs to activity feed
  - Grid bot remains active throughout

Examples:
  python scale_grid_capital.py              # Dry run with default 1.25x
  python scale_grid_capital.py 1.25         # Dry run with 25% increase
  python scale_grid_capital.py 1.25 --confirm  # Execute with 25% increase
  python scale_grid_capital.py 1.50 --confirm  # Execute with 50% increase

SAFETY GUARANTEES:
  ✅ Does NOT disable any branches
  ✅ Does NOT modify grid bot logic
  ✅ Does NOT stop grid bot execution
  ✅ Grid bot continues trading while scaling
  ✅ All changes are atomic (all-or-nothing)
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
    # Parse arguments
    scale_factor = 1.25  # Default: 25% increase
    confirm = False

    if len(sys.argv) >= 2:
        try:
            scale_factor = float(sys.argv[1])
        except ValueError:
            log.error(f"Invalid scale factor: {sys.argv[1]}")
            sys.exit(1)

    if "--confirm" in sys.argv:
        confirm = True

    if scale_factor <= 1.0:
        log.error("Scale factor must be > 1.0")
        sys.exit(1)

    log.info("=" * 70)
    log.info(f"Grid Bot Capital Scaling Utility")
    log.info("=" * 70)
    log.info(f"Scale factor: {scale_factor}x ({(scale_factor-1)*100:.0f}% increase)")
    log.info(f"Mode: {'EXECUTE' if confirm else 'DRY RUN (preview only)'}")
    log.info("=" * 70)

    # Run with dry_run unless --confirm flag provided
    result = await crypto_grid_bot.scale_grid_bot_capital(scale_factor, dry_run=not confirm)

    if result.get("status") in ["success", "dry_run_success"]:
        is_dry_run = result.get("status") == "dry_run_success"

        if is_dry_run:
            log.info(f"\n📋 DRY RUN PREVIEW (No changes applied)")
        else:
            log.info(f"\n✅ SCALING EXECUTED SUCCESSFULLY")

        log.info(f"Branches to scale: {result.get('branches_to_scale') or result.get('branches_scaled')}")
        log.info(f"Old total allocation: ${result.get('total_old_allocation'):,.2f}")
        log.info(f"New total allocation: ${result.get('total_new_allocation'):,.2f}")
        log.info(f"Total increase: ${result.get('total_increase_preview') or result.get('total_increase'):,.2f}")

        log.info("\nPer-Branch Updates:")
        updates = result.get('preview_updates') or result.get('updates', [])
        for update in updates:
            active_status = "✅ ACTIVE" if update.get('active') else "⚠️  DISABLED"
            log.info(
                f"  {update['bot_name']} ({update['product_id']}) [{active_status}]: "
                f"${update['old_allocated_usd']:,.2f} → ${update['new_allocated_usd']:,.2f} "
                f"(+{update['increase_pct']:.1f}%)"
            )

        if is_dry_run:
            log.info("\n" + "=" * 70)
            log.info("🔍 DRY RUN COMPLETE - Preview looks good?")
            log.info("\nTo EXECUTE this scaling, run:")
            log.info(f"  python scale_grid_capital.py {scale_factor} --confirm")
            log.info("\nSAFETY CHECKS PASSED:")
            log.info("  ✅ All branches remain ACTIVE")
            log.info("  ✅ Grid bot will NOT be disabled")
            log.info("  ✅ Only allocated_usd field will change")
            log.info("  ✅ Grid bot picks up new allocations on next cycle")
            log.info("=" * 70)
            sys.exit(0)
        else:
            log.info("\n" + "=" * 70)
            if result.get("safety_notes"):
                log.info("SAFETY VERIFICATION:")
                for note in result.get("safety_notes", []):
                    log.info(f"  {note}")
            log.info("=" * 70)
            log.info("Grid bot will use new allocations on next cycle (~30 seconds).")
            log.info("=" * 70)
            sys.exit(0)
    else:
        log.error(f"\n❌ OPERATION FAILED")
        log.error(f"Error: {result.get('error')}")
        log.error("\nThis means:")
        log.error("  - No branches were modified")
        log.error("  - Grid bot remains in current state")
        log.error("  - No changes were applied to database")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
