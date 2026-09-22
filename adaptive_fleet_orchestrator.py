#!/usr/bin/env python3
"""
Adaptive Capital Fleet Orchestrator — Manages 9 crypto coins with shared capital pool.
Unlocks coins at profit thresholds. Freezes underperformers. Dynamically allocates capital.
"""

import json
import os
import asyncio
import logging
from datetime import datetime
import time
from typing import Dict, List, Tuple

log = logging.getLogger("adaptive_fleet_orchestrator")
log.setLevel(logging.INFO)

BASE_DIR = "/home/user/empire-v2"
FLEET_REGISTRY = f"{BASE_DIR}/instances/fleet_registry.json"

# Nine coins: proven BTC first, then by established trading volume
NINE_COINS = [
    "BTC-USD",   # Proven ($13K history)
    "ETH-USD",   # Highest volume #2
    "SOL-USD",   # Volatile, grid-friendly
    "ADA-USD",   # Established
    "DOGE-USD",  # Volatile, liquid
    "XRP-USD",   # Established
    "LINK-USD",  # Established, volatile
    "AVAX-USD",  # Volatile, grid-friendly
    "DOT-USD",   # Established
]

# Scalping Grid Strategy Configuration
SCALPING_GRID_CONFIG = {
    "levels": 6,
    "spacing_percent": 0.5,
    "exit_target_percent": 0.75,
    "hold_time_seconds": "180-300",
    "stop_loss_percent": -0.5,
    "cycles_per_day": 10,
    "redeployment": "immediate"
}

# All coins now active (no unlock thresholds - scalping mode)
COIN_UNLOCK_THRESHOLDS = {
    "BTC-USD": 0,
    "ETH-USD": 0,
    "SOL-USD": 0,
    "ADA-USD": 0,
    "DOGE-USD": 0,
    "XRP-USD": 0,
    "LINK-USD": 0,
    "AVAX-USD": 0,
    "DOT-USD": 0,
}

# Performance thresholds for freezing underperformers
FREEZE_THRESHOLD_PF = 0.95  # Freeze if profit factor drops below 0.95
FREEZE_THRESHOLD_DAYS = 7   # After 7 days of underperformance


def get_or_create_fleet_registry() -> Dict:
    """Get or create fleet registry with coin state"""
    if os.path.exists(FLEET_REGISTRY):
        with open(FLEET_REGISTRY, 'r') as f:
            return json.load(f)
    else:
        return {
            'coins': {coin: {
                'active': coin == "BTC-USD",
                'locked': False,
                'enabled_at_profit': COIN_UNLOCK_THRESHOLDS[coin],
                'created_at': None,
                'capital_allocated': 0,
                'pnl': 0,
                'profit_factor': 1.0,
                'trade_count': 0,
                'frozen': False,
                'freeze_reason': None
            } for coin in NINE_COINS},
            'total_capital_deployed': 0,
            'total_fleet_profit': 0,
            'coins_active_count': 1,
            'coins_unlocked': 1,
            'last_update': datetime.now().isoformat()
        }


def update_fleet_registry(registry: Dict) -> None:
    """Write fleet registry to disk"""
    os.makedirs(os.path.dirname(FLEET_REGISTRY), exist_ok=True)
    registry['last_update'] = datetime.now().isoformat()
    with open(FLEET_REGISTRY, 'w') as f:
        json.dump(registry, f, indent=2)


def get_primary_bot_profit() -> float:
    """Get primary bot (BTC) current profit from bot session data"""
    try:
        session_file = f"{BASE_DIR}/bot_session.json"
        if os.path.exists(session_file):
            with open(session_file, 'r') as f:
                session = json.load(f)
            return session.get('total_realized_pnl', 0)
    except Exception as e:
        log.error(f"Error reading primary bot profit: {e}")
    return 0


def get_fleet_profit(registry: Dict) -> float:
    """Get total profit across all active coins"""
    total_profit = 0
    for coin in NINE_COINS:
        coin_data = registry['coins'][coin]
        if coin_data['active']:
            total_profit += coin_data['pnl']
    return total_profit


def get_coins_to_unlock(current_profit: float, registry: Dict) -> List[str]:
    """Determine which coins should be unlocked at current profit level"""
    coins_to_unlock = []

    for coin in NINE_COINS:
        coin_data = registry['coins'][coin]
        threshold = COIN_UNLOCK_THRESHOLDS[coin]

        # Check if coin should be unlocked
        if not coin_data['active'] and current_profit >= threshold:
            coins_to_unlock.append(coin)

    return coins_to_unlock


def unlock_coin(coin: str, current_profit: float, registry: Dict) -> Tuple[bool, str]:
    """Unlock and activate a new coin at profit threshold"""
    log.info(f"🔓 UNLOCKING COIN: {coin}")
    log.info(f"   Trigger: ${COIN_UNLOCK_THRESHOLDS[coin]:,.0f} profit reached")
    log.info(f"   Current Fleet Profit: ${current_profit:,.2f}")

    try:
        coin_data = registry['coins'][coin]
        coin_data['active'] = True
        coin_data['locked'] = False
        coin_data['created_at'] = datetime.now().isoformat()
        coin_data['capital_allocated'] = 0

        registry['coins_active_count'] = sum(
            1 for c in registry['coins'].values() if c['active']
        )
        registry['coins_unlocked'] = sum(
            1 for c in registry['coins'].values() if c['active'] or c['created_at']
        )

        update_fleet_registry(registry)
        log.info(f"✅ {coin} active. Fleet: {registry['coins_active_count']} coins")
        return True, coin
    except Exception as e:
        log.error(f"❌ Unlock failed for {coin}: {e}")
        return False, None


def check_underperformers(registry: Dict) -> List[str]:
    """Identify coins to freeze due to underperformance"""
    freeze_candidates = []

    for coin in NINE_COINS:
        coin_data = registry['coins'][coin]

        # Skip BTC (always protected) and already frozen coins
        if coin == "BTC-USD" or coin_data['frozen']:
            continue

        # Check profit factor threshold
        if coin_data['trade_count'] > 10 and coin_data['profit_factor'] < FREEZE_THRESHOLD_PF:
            freeze_candidates.append((coin, f"PF {coin_data['profit_factor']:.2f} < {FREEZE_THRESHOLD_PF}"))

    return freeze_candidates


def freeze_coin(coin: str, reason: str, registry: Dict) -> bool:
    """Freeze a coin from new trades (but don't close existing positions)"""
    log.warning(f"❄️  FREEZING COIN: {coin}")
    log.warning(f"   Reason: {reason}")

    try:
        coin_data = registry['coins'][coin]
        coin_data['frozen'] = True
        coin_data['freeze_reason'] = reason

        update_fleet_registry(registry)
        log.warning(f"   {coin} frozen from new entries")
        return True
    except Exception as e:
        log.error(f"Error freezing {coin}: {e}")
        return False


def allocate_capital(registry: Dict, total_available: float) -> Dict:
    """Dynamically allocate capital across active coins"""
    active_coins = [c for c in NINE_COINS if registry['coins'][c]['active']]

    if not active_coins:
        return {}

    # Simple allocation: equal split across active coins
    allocation_per_coin = total_available / len(active_coins)

    allocation = {}
    for coin in active_coins:
        allocation[coin] = allocation_per_coin

    return allocation


async def get_fleet_status() -> Dict:
    """Get current fleet status for API endpoint"""
    registry = get_or_create_fleet_registry()
    primary_profit = get_primary_bot_profit()
    fleet_profit = get_fleet_profit(registry)

    active_coins = [c for c in NINE_COINS if registry['coins'][c]['active']]
    frozen_coins = [c for c in NINE_COINS if registry['coins'][c]['frozen']]

    # Scalping Grid Metrics
    total_daily_target = 0
    for coin in active_coins:
        coin_data = registry['coins'][coin]
        daily_target = coin_data.get('daily_target_profit', 0)
        total_daily_target += daily_target

    return {
        "fleet_type": "Scalping Grid Fleet (9-Coin)",
        "strategy_mode": "scalping_grid_6level",
        "primary_coin": "BTC-USD",
        "primary_profit": round(primary_profit, 2),
        "fleet_total_profit": round(fleet_profit, 2),
        "active_coins": active_coins,
        "active_coins_count": len(active_coins),
        "frozen_coins": frozen_coins,
        "frozen_coins_count": len(frozen_coins),
        "total_capital_deployed": registry['total_capital_deployed'],
        "capital_per_coin": round(registry['total_capital_deployed'] / 9, 2),
        "grid_config": {
            "levels": 6,
            "spacing_percent": 0.5,
            "exit_target_percent": 0.75,
            "hold_time_seconds": "180-300",
            "cycles_per_day": 10
        },
        "expected_daily_profit": round(total_daily_target, 2),
        "expected_monthly_profit": round(total_daily_target * 30, 2),
        "coins_status": registry['coins'],
        "registry": registry
    }


def monitor_fleet() -> None:
    """Main monitoring loop — 30s cycle, scalping grid execution"""
    log.info("\n" + "="*70)
    log.info("🚀 SCALPING GRID FLEET ORCHESTRATOR STARTED")
    log.info("="*70)
    log.info("   Strategy: 6-Level Scalping Grid (0.5% spacing)")
    log.info("   9 Coins Active: BTC → ETH → SOL → ADA → DOGE → XRP → LINK → AVAX → DOT")
    log.info("   Capital: $5,000 ($555.56/coin)")
    log.info("   Target: $375/day ($41.67/coin)")
    log.info("   Monthly Target: $11,250")
    log.info("="*70)

    registry = get_or_create_fleet_registry()
    iteration = 0

    while True:
        iteration += 1

        try:
            primary_profit = get_primary_bot_profit()
            fleet_profit = get_fleet_profit(registry)

            # Log every 6 iterations (3 minutes)
            if iteration % 6 == 1:
                active_coins = [c for c in NINE_COINS if registry['coins'][c]['active']]
                log.info(f"\n[{datetime.now().strftime('%H:%M:%S')}] FLEET STATUS")
                log.info(f"  Primary (BTC) Profit: ${primary_profit:,.2f}")
                log.info(f"  Fleet Total Profit: ${fleet_profit:,.2f}")
                log.info(f"  Active Coins: {len(active_coins)} / 9")
                log.info(f"  Active: {', '.join(active_coins)}")

            # Check which coins should be unlocked
            coins_to_unlock = get_coins_to_unlock(primary_profit, registry)
            if coins_to_unlock:
                for coin in coins_to_unlock:
                    success, unlocked_coin = unlock_coin(coin, primary_profit, registry)
                    if success:
                        log.info(f"✅ Fleet unlocked {unlocked_coin}\n")

            # Check for underperformers
            freeze_candidates = check_underperformers(registry)
            if freeze_candidates:
                for coin, reason in freeze_candidates:
                    freeze_coin(coin, reason, registry)
                    log.info(f"⚠️  Froze {coin}: {reason}\n")

        except Exception as e:
            log.error(f"[Monitor Error] {e}")

        time.sleep(30)  # Check every 30 seconds


if __name__ == "__main__":
    try:
        monitor_fleet()
    except KeyboardInterrupt:
        log.info("\n\n⛔ Adaptive Fleet Orchestrator stopped by user")
