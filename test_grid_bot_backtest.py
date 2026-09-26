#!/usr/bin/env python3
"""
Grid Bot Logic Backtest - trace through trading logic without Coinbase API
"""
import asyncio
import sys
sys.path.insert(0, '/home/user/empire-v2')

from datetime import datetime
from database import get_session_factory, init_db
from models import CryptoGridBranch, CryptoGridSlice, TradingBotState
from sqlalchemy import select

async def test_grid_bot_setup():
    """Verify database schema and grid bot state"""
    print("\n=== GRID BOT BACKTEST ===\n")

    # Initialize database first
    await init_db()
    AsyncSessionLocal = get_session_factory()

    print("1. Checking grid bot activation status...")
    is_active = None
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(TradingBotState).where(TradingBotState.bot_name == 'crypto_grid_bot_mode_active')
            )
            state = result.scalar_one_or_none()
            if state:
                print(f"   ✓ Grid bot state found: base_capital={state.base_capital}")
                is_active = bool(state.base_capital and state.base_capital >= 1.0)
                print(f"   → Grid bot active: {is_active}")
            else:
                print(f"   ✗ Grid bot state NOT found (will default to True)")
                is_active = True
    except Exception as e:
        print(f"   ✗ Error: {e}")
        return False

    print("\n2. Checking grid branches in database...")
    branches = []
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(CryptoGridBranch))
            branches = list(result.scalars().all())
            print(f"   Found {len(branches)} grid branches")
            if not branches:
                print(f"   ✗ CRITICAL: No grid branches found - bot will exit run_grid_branches_cycle()")
                print(f"      → At line 2454: if not branches: return")
                return False
            for b in branches:
                print(f"      - {b.bot_name} ({b.product_id}): ${b.allocated_usd:.2f}, active={b.active}")
    except Exception as e:
        print(f"   ✗ Error: {e}")
        return False

    print("\n3. Tracing run_grid_branches_cycle() logic...")
    print(f"   Step 1: is_grid_bot_active() = {is_active}")
    if not is_active:
        print(f"   → EARLY EXIT: is_grid_bot_active() returned False")
        return False

    print(f"   Step 2: Get active branches")
    active_branches = [b for b in branches if b.active]
    print(f"   → Found {len(active_branches)} active branches")
    if not active_branches:
        print(f"   → EARLY EXIT: No active branches found")
        return False

    print(f"   Step 3: For each active branch, call run_grid_branch_cycle()")
    for b in active_branches:
        print(f"      - Would process {b.bot_name} on {b.product_id}")

    print("\n4. Checking grid slices (open positions)...")
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(CryptoGridSlice))
            slices = list(result.scalars().all())
            print(f"   Found {len(slices)} grid slices")
            for s in slices:
                print(f"      - {s.bot_name}: {s.quantity} @ ${s.entry_price:.2f}")
    except Exception as e:
        print(f"   ✗ Error: {e}")

    print("\n=== DIAGNOSIS ===")
    if len(branches) == 0:
        print("❌ PROBLEM: Database has ZERO grid branches")
        print("   CAUSE: create_grid_branch() failed during app startup")
        print("   LIKELY REASON: get_price_and_volatility() failed (Coinbase API call)")
        print("   FIX: Check Railway logs for API errors during startup")
        return False
    else:
        print(f"✓ Grid bot has {len(branches)} branches configured")
        print(f"✓ Grid bot active: {is_active}")
        if len(active_branches) == 0:
            print("❌ But NO branches are marked as active")
            return False
        else:
            print(f"✓ {len(active_branches)} branches are active and ready to trade")
            return True

asyncio.run(test_grid_bot_setup())
