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

def get_primary_bot_profit():
    """Get primary bot current profit from Grid Bot"""
    try:
        # Read from bot session data
        session_file = f"{BASE_DIR}/bot_session.json"
        if os.path.exists(session_file):
            with open(session_file, 'r') as f:
                session = json.load(f)
            # Sum up realized P&L from all grid branches
            return session.get('total_realized_pnl', 0)
    except Exception as e:
        log.error(f"Error reading primary bot profit: {e}")
    return 0

def get_fleet_profit(registry):
    """Get total profit across all instances"""
    total_profit = get_primary_bot_profit()

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
            current_profit = get_primary_bot_profit()
            fleet_profit = get_fleet_profit(registry)

            if iteration % 6 == 1:  # Log every 3 minutes (check every 30s)
                log.info(f"\n[{datetime.now().strftime('%H:%M:%S')}] FLEET STATUS")
                log.info(f"  Primary Bot Profit: ${current_profit:,.2f}")
                log.info(f"  Fleet Total Profit: ${fleet_profit:,.2f}")
                log.info(f"  Instances Active: {len(registry.get('instances', [])) + 1}")
                log.info(f"  Clones Created: {registry.get('clones_created', 0)}")

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
    """Get current fleet status for API endpoint"""
    registry = get_or_create_registry()
    primary_profit = get_primary_bot_profit()
    fleet_profit = get_fleet_profit(registry)

    return {
        "primary_bot_profit": round(primary_profit, 2),
        "fleet_total_profit": round(fleet_profit, 2),
        "active_instances": len(registry.get('instances', [])) + 1,
        "clones_created": registry.get('clones_created', 0),
        "total_capital_deployed": registry.get('total_capital_deployed', 0),
        "profit_thresholds": PROFIT_THRESHOLDS,
        "next_threshold": next((t for t in PROFIT_THRESHOLDS if t > primary_profit), None),
        "registry": registry
    }

if __name__ == "__main__":
    try:
        monitor_fleet()
    except KeyboardInterrupt:
        log.info("\n\n⛔ Scaling coordinator stopped by user")
