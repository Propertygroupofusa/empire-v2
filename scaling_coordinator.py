#!/usr/bin/env python3
"""
Scaling Coordinator — Automated bot instance management.
Monitors primary bot profit, triggers clones at thresholds, manages multi-instance fleet.
"""

import json
import os
import subprocess
from datetime import datetime
import time
import asyncio
import logging

log = logging.getLogger("scaling_coordinator")
log.setLevel(logging.INFO)

BASE_DIR = "/home/user/empire-v2"
PROFIT_THRESHOLDS = [50000, 100000, 150000, 200000]
INSTANCE_REGISTRY = f"{BASE_DIR}/instances/registry.json"

def get_or_create_registry():
    """Get or create instance registry"""
    if os.path.exists(INSTANCE_REGISTRY):
        with open(INSTANCE_REGISTRY, 'r') as f:
            return json.load(f)
    else:
        return {
            'instances': [],
            'total_capital_deployed': 0,
            'total_profit': 0,
            'clones_created': 0,
            'last_update': datetime.now().isoformat()
        }

def update_registry(registry):
    """Write registry"""
    os.makedirs(os.path.dirname(INSTANCE_REGISTRY), exist_ok=True)
    registry['last_update'] = datetime.now().isoformat()
    with open(INSTANCE_REGISTRY, 'w') as f:
        json.dump(registry, f, indent=2)

async def get_primary_bot_profit():
    """The primary bot's REAL realized profit, from the database.

    This used to read `{BASE_DIR}/bot_session.json` and return 0 when the
    file was missing. Three things were wrong with that at once, and
    together they made the figure incapable of ever being right:

      1. NOTHING IN THIS REPO WRITES bot_session.json. Three modules read
         it; zero write it. The grid bot records every closed round trip
         in CryptoGridTradeHistory, never in a JSON file.
      2. BASE_DIR was the hardcoded developer path "/home/user/empire-v2",
         which does not exist in the deployed container.
      3. The missing file fell through to `return 0`, and the endpoint
         logged that 0 as though it were a measurement.

    So the dashboard reported "Primary profit $0.00" permanently, while
    the account had really made +$19.55 over 82 closed round trips. A
    fallback presented as a reading is worse than no reading, which is why
    a real failure here now returns None and is rendered as "unavailable"
    rather than as zero.
    """
    try:
        import crypto_grid_bot
        history = await crypto_grid_bot.get_grid_trade_history(limit_recent=0)
        return history.get("total_realized_pnl")
    except Exception as e:
        log.error(f"Could not read real grid profit from the database: {e}")
        return None

def get_fleet_profit(registry, primary_profit):
    """Primary profit plus every cloned instance's own profit.

    Takes the primary figure rather than re-fetching it, so the two can
    never disagree. An unreadable primary makes the fleet total unreadable
    too - it is not silently treated as zero and added to.
    """
    if primary_profit is None:
        return None
    total_profit = primary_profit

    for instance in registry.get('instances', []):
        try:
            instance_session = f"{BASE_DIR}/instances/{instance['id']}/bot_session.json"
            if os.path.exists(instance_session):
                with open(instance_session, 'r') as f:
                    session = json.load(f)
                    total_profit += session.get('total_realized_pnl', 0)
        except Exception as e:
            log.warning(f"Error reading instance {instance['id']} profit: {e}")

    return total_profit

def should_clone(current_profit, registry):
    """Determine if clone should be created"""
    clones_created = registry.get('clones_created', 0)

    for threshold_level, threshold in enumerate(PROFIT_THRESHOLDS):
        if current_profit >= threshold and threshold_level >= clones_created:
            return True, threshold, threshold_level

    return False, None, None

def clone_new_instance(threshold_level, primary_profit):
    """Create new bot clone"""
    instance_id = f"bot_clone_{threshold_level + 1}"
    capital_pct = 50.0  # Allocate 50% of primary capital to each clone

    log.info(f"🚀 CREATING CLONE: {instance_id}")
    log.info(f"   Trigger: ${PROFIT_THRESHOLDS[threshold_level]:,.0f} profit reached")

    try:
        # Create instance directory
        instance_dir = f"{BASE_DIR}/instances/{instance_id}"
        os.makedirs(instance_dir, exist_ok=True)

        # Copy primary bot config to instance
        import shutil
        if os.path.exists(f"{BASE_DIR}/crypto_grid_bot.py"):
            shutil.copy(f"{BASE_DIR}/crypto_grid_bot.py", f"{instance_dir}/crypto_grid_bot.py")

        log.info(f"✅ Clone {instance_id} directory created")
        return True, instance_id
    except Exception as e:
        log.error(f"❌ Clone creation failed: {e}")
        return False, None

def monitor_fleet():
    """Main monitoring loop"""
    log.info("\n" + "="*70)
    log.info("🤖 SCALING COORDINATOR STARTED")
    log.info("="*70)

    registry = get_or_create_registry()

    iteration = 0
    while True:
        iteration += 1

        try:
            current_profit = asyncio.run(get_primary_bot_profit())
            fleet_profit = get_fleet_profit(registry, current_profit)

            if iteration % 6 == 1:  # Log every 3 minutes (check every 30s)
                def _money(v):
                    return "unavailable" if v is None else f"${v:,.2f}"
                log.info(f"\n[{datetime.now().strftime('%H:%M:%S')}] FLEET STATUS")
                log.info(f"  Primary Bot Profit: {_money(current_profit)}")
                log.info(f"  Fleet Total Profit: {_money(fleet_profit)}")
                log.info(f"  Instances Active: {len(registry.get('instances', [])) + 1}")
                log.info(f"  Clones Created: {registry.get('clones_created', 0)}")

            # An unreadable profit must never be treated as "below every
            # threshold" and silently skipped, nor as a reason to clone.
            if current_profit is None:
                time.sleep(30)
                continue

            # Check if clone should be created
            should_scale, threshold, level = should_clone(current_profit, registry)

            if should_scale:
                success, instance_id = clone_new_instance(level, current_profit)

                if success:
                    # Update registry
                    registry['instances'].append({
                        'id': instance_id,
                        'created_at': datetime.now().isoformat(),
                        'created_at_profit': current_profit,
                        'capital_allocated': 0  # Will be updated per instance
                    })
                    registry['clones_created'] = level + 1
                    update_registry(registry)

                    log.info(f"✅ Registry updated. Clones: {registry['clones_created']}\n")

        except Exception as e:
            log.error(f"[Monitor Error] {e}")

        time.sleep(30)  # Check every 30 seconds

async def get_fleet_status():
    """Get current fleet status for API endpoint.

    Every money figure is either a real number from the database or None.
    None means "could not read", which the dashboard must render as
    unavailable - never as $0.00. That distinction is the whole point of
    this rewrite: the previous version could only ever return zero.
    """
    registry = get_or_create_registry()
    primary_profit = await get_primary_bot_profit()
    fleet_profit = get_fleet_profit(registry, primary_profit)

    return {
        "primary_bot_profit": (None if primary_profit is None else round(primary_profit, 2)),
        "fleet_total_profit": (None if fleet_profit is None else round(fleet_profit, 2)),
        "profit_source": ("CryptoGridTradeHistory - every closed round trip"
                          if primary_profit is not None else
                          "unavailable - the database could not be read"),
        "active_instances": len(registry.get('instances', [])) + 1,
        "clones_created": registry.get('clones_created', 0),
        "total_capital_deployed": registry.get('total_capital_deployed', 0),
        "profit_thresholds": PROFIT_THRESHOLDS,
        "next_threshold": (None if primary_profit is None else
                           next((t for t in PROFIT_THRESHOLDS if t > primary_profit), None)),
        "registry": registry
    }

if __name__ == "__main__":
    try:
        monitor_fleet()
    except KeyboardInterrupt:
        log.info("\n\n⛔ Scaling coordinator stopped by user")
