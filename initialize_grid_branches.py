#!/usr/bin/env python3
"""Initialize grid branches for 9-coin scalping fleet"""
import asyncio
import sys
sys.path.insert(0, '/home/user/empire-v2')

from crypto_grid_bot import create_grid_branch

NINE_COINS = {
    "BTC-USD": 138.49,
    "ETH-USD": 138.49,
    "SOL-USD": 138.49,
    "ADA-USD": 138.49,
    "DOGE-USD": 138.49,
    "XRP-USD": 138.49,
    "LINK-USD": 138.49,
    "AVAX-USD": 138.49,
    "DOT-USD": 138.49,
}

async def init_branches():
    print("🚀 Initializing grid branches for 9-coin fleet...")
    for coin, capital in NINE_COINS.items():
        try:
            branch = await create_grid_branch(coin, capital, skip_free_cash_check=True)
            print(f"✓ {coin:12} — ${capital:.2f} grid branch created")
        except Exception as e:
            print(f"✗ {coin:12} — ERROR: {e}")
    print("✅ Grid branch initialization complete")

if __name__ == "__main__":
    asyncio.run(init_branches())
