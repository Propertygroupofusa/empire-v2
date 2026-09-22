#!/usr/bin/env python3
"""
Fleet Scalping Grid Dashboard - Real-time monitoring of 9-coin scalping grid
"""
import json
import logging
from datetime import datetime
from typing import Dict

log = logging.getLogger("fleet_scalping_dashboard")

def get_fleet_dashboard_data() -> Dict:
    """Generate dashboard data for scalping grid fleet"""
    try:
        with open('instances/fleet_registry.json', 'r') as f:
            registry = json.load(f)
    except:
        return {"status": "error", "message": "Fleet registry not found"}

    coins_data = []
    total_capital = registry.get('total_capital_deployed', 5000)
    total_profit = 0

    for coin, data in registry['coins'].items():
        coin_profit = data.get('pnl', 0)
        total_profit += coin_profit

        capital_allocated = data.get('capital_allocated', total_capital/9)
        # Profit calc: (capital/6 levels) × 0.75% exit × 6 levels × 10 cycles/day
        pos_per_level = capital_allocated / 6
        profit_per_level = pos_per_level * 0.0075
        daily_target = profit_per_level * 6 * 10

        coins_data.append({
            "coin": coin,
            "status": "✓ ACTIVE" if data['active'] else "LOCKED",
            "capital": round(capital_allocated, 2),
            "daily_target": round(daily_target, 2),
            "current_pnl": round(coin_profit, 2),
            "trades": data.get('trade_count', 0),
            "pf": round(data.get('profit_factor', 1.0), 2)
        })

    # Fleet-wide daily target
    pos_per_level = total_capital / 9 / 6
    profit_per_level = pos_per_level * 0.0075
    daily_per_coin = profit_per_level * 6 * 10

    dashboard = {
        "timestamp": datetime.now().isoformat(),
        "strategy": "Scalping Grid 6-Level (0.5% spacing)",
        "fleet_status": "🚀 ACTIVE",
        "total_capital": round(total_capital, 2),
        "capital_per_coin": round(total_capital / 9, 2),
        "daily_profit_target": round(daily_per_coin * 9, 2),
        "monthly_profit_target": round(daily_per_coin * 9 * 30, 2),
        "total_fleet_pnl": round(total_profit, 2),
        "coins_active": sum(1 for c in registry['coins'].values() if c['active']),
        "coins_data": coins_data
    }
    
    return dashboard

if __name__ == "__main__":
    data = get_fleet_dashboard_data()
    print(json.dumps(data, indent=2))
