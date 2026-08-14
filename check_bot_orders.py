#!/usr/bin/env python3
"""
Bot Order Execution Monitor
Verifies that signals actually result in orders (prevents signal-but-no-order failures)

Run this script to check if the bot is executing orders on Alpaca in real-time.
"""

import asyncio
import aiohttp
import os
from datetime import datetime, timedelta

ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type": "application/json"
}

async def check_recent_orders():
    """Check if orders were placed in the last 5 minutes"""
    if not ALPACA_KEY or not ALPACA_SECRET:
        print("❌ ERROR: Alpaca credentials not configured")
        print("   Set ALPACA_API_KEY and ALPACA_SECRET_KEY environment variables")
        return

    async with aiohttp.ClientSession() as session:
        # Get account info
        try:
            async with session.get(f"{BASE_URL}/v2/account", headers=HEADERS) as r:
                if r.status != 200:
                    print(f"❌ ERROR: Failed to connect to Alpaca ({r.status})")
                    return
                account = await r.json()
        except Exception as e:
            print(f"❌ ERROR: Connection failed: {e}")
            return

        # Get orders from last 24 hours
        try:
            async with session.get(f"{BASE_URL}/v2/orders?limit=100&status=all", headers=HEADERS) as r:
                orders = await r.json()
        except Exception as e:
            print(f"❌ ERROR: Failed to fetch orders: {e}")
            return

        # Parse and display recent orders
        now = datetime.utcnow()
        five_min_ago = now - timedelta(minutes=5)

        print(f"\n{'='*70}")
        print(f"  Bot Order Execution Monitor")
        print(f"  Account: {account.get('account_number', 'unknown')}")
        print(f"  Equity: ${float(account.get('equity', 0)):.2f}")
        print(f"  Buying Power: ${float(account.get('buying_power', 0)):.2f}")
        print(f"  Timestamp: {now.isoformat()}")
        print(f"{'='*70}\n")

        recent_orders = []
        for order in orders:
            created_at = datetime.fromisoformat(order['created_at'].replace('Z', '+00:00'))
            created_at = created_at.replace(tzinfo=None)  # Remove timezone for comparison

            if created_at >= five_min_ago:
                recent_orders.append(order)

        if not recent_orders:
            print("⏳ No orders in last 5 minutes")
            print("   Bot may be waiting for RSI signal (< 25 or > 75)")
            print("   Check Railway logs for signal detection\n")
        else:
            print(f"✅ {len(recent_orders)} ORDERS PLACED IN LAST 5 MINUTES:\n")
            for order in sorted(recent_orders, key=lambda x: x['created_at'], reverse=True):
                status = "✅" if order['status'] == 'filled' else ("⏳" if order['status'] == 'pending' else "❌")
                print(f"  {status} {order['created_at']}")
                print(f"     {order['side'].upper()} {order.get('qty', 0)} {order['symbol']}")
                print(f"     Status: {order['status']}")
                if order.get('filled_avg_price'):
                    print(f"     Filled @ ${float(order['filled_avg_price']):.2f}")
                print()

        # Check for OPEN POSITIONS
        try:
            async with session.get(f"{BASE_URL}/v2/positions", headers=HEADERS) as r:
                positions = await r.json()
        except:
            positions = []

        if positions:
            print(f"\n{'='*70}")
            print(f"  OPEN POSITIONS: {len(positions)}")
            print(f"{'='*70}\n")
            for pos in positions:
                pnl = float(pos.get('unrealized_pl', 0))
                pnl_pct = float(pos.get('unrealized_plpc', 0)) * 100
                status = "📈" if pnl > 0 else ("📉" if pnl < 0 else "➡️")
                print(f"  {status} {pos['symbol']}")
                print(f"     Qty: {pos['qty']} | Entry: ${float(pos.get('avg_fill_price', 0)):.2f}")
                print(f"     Current: ${float(pos.get('current_price', 0)):.2f} | P&L: ${pnl:.2f} ({pnl_pct:.2f}%)\n")
        else:
            print("\n⏳ No open positions")
            print("   Bot has not yet executed buy orders for detected signals")
            print("   (This is normal if no extreme RSI signals found yet)\n")

        print(f"{'='*70}\n")

if __name__ == "__main__":
    asyncio.run(check_recent_orders())
