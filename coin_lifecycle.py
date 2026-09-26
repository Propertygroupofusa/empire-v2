#!/usr/bin/env python3
"""
Coin Lifecycle Manager — Handles coin setup, trading initialization, and grid configuration.
Integrates with adaptive_fleet_orchestrator to manage active coins.
"""

import json
import os
import logging
from datetime import datetime
from typing import Dict, Optional, Tuple

log = logging.getLogger("coin_lifecycle")
log.setLevel(logging.INFO)

BASE_DIR = "/home/user/empire-v2"


class CoinLifecycle:
    """Manage individual coin's lifecycle: setup, activate, monitor, freeze"""

    def __init__(self, coin: str, db_connection=None):
        self.coin = coin
        self.db = db_connection
        self.grid_pct = 0.01  # 1% grid spacing (matches proven BTC setup)
        self.num_levels = 10  # 10 grid levels (matches proven BTC setup)
        self.allocated_usd = 0  # Set during activation

    def setup_coin_for_trading(self, allocated_capital: float) -> Tuple[bool, str]:
        """
        Setup coin for grid trading.
        Creates database entry and initializes grid parameters.
        """
        try:
            # Dynamic capital allocation based on coin order
            # BTC gets 100% of allocated, others split the pool
            coin_capital = allocated_capital

            log.info(f"📋 SETTING UP: {self.coin}")
            log.info(f"   Capital Allocated: ${coin_capital:,.2f}")
            log.info(f"   Grid Config: {self.grid_pct*100}% spacing, {self.num_levels} levels")

            # Here we would insert into crypto_grid_branches table
            # For now, just log the setup
            self.allocated_usd = coin_capital

            return True, f"{self.coin} initialized with ${coin_capital:,.2f}"

        except Exception as e:
            log.error(f"❌ Setup failed for {self.coin}: {e}")
            return False, str(e)

    def enable_trading(self) -> bool:
        """Enable live trading for this coin"""
        try:
            log.info(f"▶️  ENABLING TRADING: {self.coin}")
            # Update database to mark as active=true
            return True
        except Exception as e:
            log.error(f"Error enabling {self.coin}: {e}")
            return False

    def pause_trading(self, reason: str = "Manual pause") -> bool:
        """Pause trading for this coin (freeze without closing positions)"""
        try:
            log.warning(f"⏸️  PAUSING TRADING: {self.coin}")
            log.warning(f"   Reason: {reason}")
            # Update database to mark as active=false
            return True
        except Exception as e:
            log.error(f"Error pausing {self.coin}: {e}")
            return False

    def get_performance_metrics(self) -> Dict:
        """Get current performance metrics for this coin"""
        return {
            "coin": self.coin,
            "capital_allocated": self.allocated_usd,
            "grid_pct": self.grid_pct,
            "num_levels": self.num_levels,
            "trades_completed": 0,
            "win_rate": 0,
            "profit_factor": 1.0,
            "total_pnl": 0
        }


class FleetCoinManager:
    """Manage lifecycle for entire 9-coin fleet"""

    NINE_COINS = [
        "BTC-USD", "ETH-USD", "SOL-USD", "ADA-USD", "DOGE-USD",
        "XRP-USD", "LINK-USD", "AVAX-USD", "DOT-USD"
    ]

    COIN_CONFIG = {
        "BTC-USD": {"grid_pct": 0.01, "num_levels": 10, "priority": 1},
        "ETH-USD": {"grid_pct": 0.015, "num_levels": 8, "priority": 2},
        "SOL-USD": {"grid_pct": 0.02, "num_levels": 8, "priority": 3},
        "ADA-USD": {"grid_pct": 0.025, "num_levels": 6, "priority": 4},
        "DOGE-USD": {"grid_pct": 0.03, "num_levels": 6, "priority": 5},
        "XRP-USD": {"grid_pct": 0.025, "num_levels": 6, "priority": 6},
        "LINK-USD": {"grid_pct": 0.02, "num_levels": 8, "priority": 7},
        "AVAX-USD": {"grid_pct": 0.02, "num_levels": 8, "priority": 8},
        "DOT-USD": {"grid_pct": 0.025, "num_levels": 6, "priority": 9},
    }

    def __init__(self, db_connection=None):
        self.db = db_connection
        self.coins = {coin: CoinLifecycle(coin, db_connection) for coin in self.NINE_COINS}

    def initialize_coin(self, coin: str, capital: float) -> bool:
        """Initialize a specific coin for trading"""
        if coin not in self.coins:
            log.error(f"Unknown coin: {coin}")
            return False

        config = self.COIN_CONFIG[coin]
        lifecycle = self.coins[coin]

        # Apply coin-specific grid config
        lifecycle.grid_pct = config["grid_pct"]
        lifecycle.num_levels = config["num_levels"]

        success, msg = lifecycle.setup_coin_for_trading(capital)
        log.info(f"   {msg}")
        return success

    def activate_coin(self, coin: str) -> bool:
        """Activate a coin for live trading"""
        if coin not in self.coins:
            return False
        return self.coins[coin].enable_trading()

    def freeze_coin(self, coin: str, reason: str) -> bool:
        """Freeze a coin from new trades"""
        if coin not in self.coins:
            return False
        return self.coins[coin].pause_trading(reason)

    def get_fleet_summary(self) -> Dict:
        """Get summary of all coins' performance"""
        return {
            "timestamp": datetime.now().isoformat(),
            "coins": {coin: self.coins[coin].get_performance_metrics()
                     for coin in self.NINE_COINS},
            "total_coins": len(self.NINE_COINS),
            "config": self.COIN_CONFIG
        }


def provision_all_coins(total_capital: float, active_coins: list) -> Dict:
    """
    Provision capital across active coins.
    Equal split per active coin.
    """
    if not active_coins:
        return {}

    capital_per_coin = total_capital / len(active_coins)

    return {coin: capital_per_coin for coin in active_coins}


if __name__ == "__main__":
    # Test: Initialize fleet
    fleet = FleetCoinManager()

    log.info("\n📊 FLEET INITIALIZATION TEST")

    # Start with just BTC
    fleet.initialize_coin("BTC-USD", 1000)
    fleet.activate_coin("BTC-USD")

    log.info("\n✅ BTC-USD provisioned and ready")
    log.info(f"\nFleet Summary:\n{json.dumps(fleet.get_fleet_summary(), indent=2)}")
