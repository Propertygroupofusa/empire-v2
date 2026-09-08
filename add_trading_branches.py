#!/usr/bin/env python3
"""
Add new trading branches for BTC and AAVE to the grid bot system.

Usage:
  python add_trading_branches.py [--confirm]

Default behavior (NO --confirm flag):
  - Shows DRY RUN preview of what will be added
  - Does NOT add branches to database

With --confirm flag:
  - Adds BTC-USD and AAVE-USD branches to the system
  - Updates database with new allocations
  - Logs to activity feed
  - Grid bot picks up new branches on next cycle

Examples:
  python add_trading_branches.py              # Dry run (preview)
  python add_trading_branches.py --confirm    # Execute (add branches)

SAFETY:
  ✅ Existing branches remain ACTIVE and UNCHANGED
  ✅ Only adds new branches (no deletions)
  ✅ Grid bot continues trading throughout
  ✅ All changes atomic (all-or-nothing)
"""

import asyncio
import sys
import logging
from datetime import datetime
from database import AsyncSessionLocal
from models import CryptoGridBranch, CryptoActivityEvent
from sqlalchemy import select

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("add_branches")


async def main():
    confirm = "--confirm" in sys.argv

    log.info("=" * 70)
    log.info("Grid Bot: Add Trading Branches")
    log.info("=" * 70)
    log.info(f"Mode: {'EXECUTE' if confirm else 'DRY RUN (preview only)'}")
    log.info("=" * 70)

    # New branches to add
    new_branches = [
        {
            "bot_name": "crypto_grid_4",
            "product_id": "BTC-USD",
            "allocated_usd": 400.00,
            "active": True,
            "reason": "High liquidity, major trend follower"
        },
        {
            "bot_name": "crypto_grid_5",
            "product_id": "AAVE-USD",
            "allocated_usd": 300.00,
            "active": True,
            "reason": "DeFi protocol token, high volatility opportunity"
        }
    ]

    async with AsyncSessionLocal() as db:
        # Get current branches
        result = await db.execute(select(CryptoGridBranch).where(CryptoGridBranch.active == True))
        existing_branches = result.scalars().all()

        current_total = sum(b.allocated_usd for b in existing_branches)
        new_total = current_total + sum(b["allocated_usd"] for b in new_branches)

        log.info(f"\n📊 Current State:")
        log.info(f"  Active branches: {len(existing_branches)}")
        log.info(f"  Current total allocation: ${current_total:,.2f}")
        for branch in existing_branches:
            log.info(f"    • {branch.bot_name} ({branch.product_id}): ${branch.allocated_usd:,.2f}")

        log.info(f"\n➕ Branches to Add:")
        for new_branch in new_branches:
            log.info(f"  • {new_branch['bot_name']} ({new_branch['product_id']}): ${new_branch['allocated_usd']:,.2f}")
            log.info(f"    Reason: {new_branch['reason']}")

        log.info(f"\n📈 After Addition:")
        log.info(f"  Total allocation will be: ${new_total:,.2f}")
        log.info(f"  Capital increase: ${new_total - current_total:,.2f} ({100 * (new_total - current_total) / current_total:.1f}%)")

        if not confirm:
            log.info("\n" + "=" * 70)
            log.info("🔍 DRY RUN COMPLETE - Ready to add?")
            log.info("\nTo EXECUTE branch addition, run:")
            log.info(f"  python add_trading_branches.py --confirm")
            log.info("\nSAFETY CHECKS:")
            log.info("  ✅ Existing branches remain ACTIVE")
            log.info("  ✅ Grid bot will NOT be interrupted")
            log.info("  ✅ New branches available on next cycle")
            log.info("  ✅ Mean reversion bot already supports BTC & AAVE")
            log.info("=" * 70)
            sys.exit(0)

        # EXECUTE: Add branches to database
        log.info("\n" + "=" * 70)
        log.info("✅ EXECUTING BRANCH ADDITION...")
        log.info("=" * 70)

        try:
            for new_branch in new_branches:
                branch = CryptoGridBranch(
                    bot_name=new_branch["bot_name"],
                    product_id=new_branch["product_id"],
                    allocated_usd=new_branch["allocated_usd"],
                    active=True
                )
                db.add(branch)

            # Log activity event
            activity = CryptoActivityEvent(
                bot_name="crypto_grid_orchestrator",
                product_id="SYSTEM",
                event_type="SCALE",
                message=f"Added branches: BTC-USD (+${new_branches[0]['allocated_usd']:.2f}), AAVE-USD (+${new_branches[1]['allocated_usd']:.2f}). Total now ${new_total:,.2f}",
                created_at=datetime.utcnow()
            )
            db.add(activity)

            await db.commit()

            log.info("\n✅ BRANCHES ADDED SUCCESSFULLY")
            log.info(f"\nNew total allocation: ${new_total:,.2f}")
            log.info("Grid bot will trade BTC-USD and AAVE-USD on next cycle.")
            log.info("\nBranches now active:")
            for branch in existing_branches:
                log.info(f"  • {branch.bot_name} ({branch.product_id}): ${branch.allocated_usd:,.2f}")
            for new_branch in new_branches:
                log.info(f"  • {new_branch['bot_name']} ({new_branch['product_id']}): ${new_branch['allocated_usd']:,.2f} (NEW)")

            log.info("\n" + "=" * 70)
            log.info("Mean reversion bot will monitor RSI on BTC-USD and AAVE-USD.")
            log.info("Next deployment: Push to Railway to activate.")
            log.info("=" * 70)
            sys.exit(0)

        except Exception as e:
            log.error(f"\n❌ FAILED TO ADD BRANCHES")
            log.error(f"Error: {e}")
            log.error("\nNo changes were applied to database.")
            await db.rollback()
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
