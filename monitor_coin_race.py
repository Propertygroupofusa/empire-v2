#!/usr/bin/env python3
"""
Monitor 9-coin scalping fleet race to $15 profit per coin
Tracks placement order: 1st coin to $15, 2nd, 3rd, etc.
"""
import json
import os
from datetime import datetime
from pathlib import Path

FLEET_REGISTRY = "/home/user/empire-v2/instances/fleet_registry.json"
RACE_TRACKER = "/home/user/empire-v2/instances/coin_race_tracker.json"

def get_fleet_status():
    """Read current fleet PNL status"""
    try:
        with open(FLEET_REGISTRY, 'r') as f:
            registry = json.load(f)
        return registry.get('coins', {})
    except Exception as e:
        print(f"Error reading fleet registry: {e}")
        return {}

def get_race_tracker():
    """Load race tracker or create new"""
    if os.path.exists(RACE_TRACKER):
        try:
            with open(RACE_TRACKER, 'r') as f:
                return json.load(f)
        except:
            pass
    return {"finishers": [], "started_at": datetime.now().isoformat()}

def save_race_tracker(tracker):
    """Save race tracker"""
    os.makedirs(os.path.dirname(RACE_TRACKER), exist_ok=True)
    with open(RACE_TRACKER, 'w') as f:
        json.dump(tracker, f, indent=2)

def check_race():
    """Check if any coins have reached $15 profit"""
    coins = get_fleet_status()
    tracker = get_race_tracker()
    finishers = {f['coin'] for f in tracker['finishers']}

    # Check which coins hit $15
    for coin, data in sorted(coins.items()):
        pnl = data.get('pnl', 0)
        if pnl >= 15.0 and coin not in finishers:
            placement = len(tracker['finishers']) + 1
            tracker['finishers'].append({
                "placement": placement,
                "coin": coin,
                "pnl": round(pnl, 2),
                "reached_at": datetime.now().isoformat()
            })
            print(f"\n🏁 PLACE #{placement}: {coin} crossed $15! (${pnl:.2f})")
            save_race_tracker(tracker)

    # Display current standings
    print("\n" + "="*70)
    print(f"💰 COIN RACE TO $15 PROFIT — {datetime.now().strftime('%H:%M:%S')}")
    print("="*70)

    # Finishers
    if tracker['finishers']:
        print("\n🏆 FINISHERS (crossed $15):")
        for f in tracker['finishers']:
            print(f"  #{f['placement']}: {f['coin']:12} — ${f['pnl']:7.2f} ✅")

    # In progress (sorted by PNL)
    in_progress = [(coin, coins[coin].get('pnl', 0))
                   for coin in coins
                   if coin not in finishers]
    in_progress.sort(key=lambda x: x[1], reverse=True)

    if in_progress:
        print("\n📈 IN PROGRESS (ranked by current PNL):")
        for idx, (coin, pnl) in enumerate(in_progress, 1):
            pct_to_15 = (pnl / 15.0) * 100 if pnl >= 0 else 0
            bar = "▓" * int(pct_to_15 / 5) + "░" * (20 - int(pct_to_15 / 5))
            print(f"  {idx}. {coin:12} — ${pnl:7.2f} [{bar}] {pct_to_15:5.1f}%")

    # Not yet crossed
    not_crossed = [coin for coin in coins if coins[coin].get('pnl', 0) < 0]
    if not_crossed:
        print(f"\n⚠️  BEHIND (negative P&L): {', '.join(sorted(not_crossed))}")

    print("="*70)

    # Summary
    if len(tracker['finishers']) == 9:
        print("\n🎉 ALL 9 COINS CROSSED $15! RACE COMPLETE!")
        print("Final Standings:")
        for f in tracker['finishers']:
            print(f"  #{f['placement']:2d}: {f['coin']:12} — ${f['pnl']:7.2f} @ {f['reached_at']}")
    else:
        remaining = 9 - len(tracker['finishers'])
        print(f"\n⏳ {remaining} coins still racing...")

if __name__ == "__main__":
    check_race()
