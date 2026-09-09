#!/usr/bin/env python3
"""Check recent Coinbase trades to verify bot position sizing (0.22 scale)."""

import os
import json
from datetime import datetime, timedelta
from coinbase.client import Client

# Initialize Coinbase client
api_key = os.getenv('COINBASE_API_KEY')
api_secret = os.getenv('COINBASE_API_SECRET')
account_id = os.getenv('COINBASE_ACCOUNT_ID')

if not all([api_key, api_secret, account_id]):
    print("❌ ERROR: Coinbase credentials not set in environment")
    print("   COINBASE_API_KEY:", "SET" if api_key else "MISSING")
    print("   COINBASE_API_SECRET:", "SET" if api_secret else "MISSING")
    print("   COINBASE_ACCOUNT_ID:", "SET" if account_id else "MISSING")
    exit(1)

try:
    client = Client(api_key, api_secret)

    # Get account info
    print("\n" + "="*80)
    print("COINBASE ACCOUNT STATUS")
    print("="*80)

    account = client.get_account(account_id)
    print(f"\nAccount ID: {account_id}")
    print(f"Currency: {account.get('currency', 'N/A')}")
    print(f"Balance: {account.get('balance', 'N/A')} BTC")
    print(f"Available: {account.get('available', 'N/A')} BTC")
    print(f"Hold: {account.get('hold', 'N/A')} BTC")

    # Get recent trades/fills (last 24 hours)
    print("\n" + "="*80)
    print("RECENT TRADES (Last 24 Hours)")
    print("="*80)

    # Get fills (executed orders)
    try:
        fills = client.get_fills(product_id='BTC-USD', limit=50)

        if fills:
            print(f"\nFound {len(fills)} recent fills:")
            print(f"\n{'Time':<25} {'Side':<6} {'Size (BTC)':<12} {'Price':<12} {'Fee':<10}")
            print("-" * 70)

            total_bought = 0
            total_sold = 0

            for fill in fills:
                timestamp = fill.get('created_at', '')
                # Parse ISO timestamp
                try:
                    dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    time_str = dt.strftime('%Y-%m-%d %H:%M:%S')
                except:
                    time_str = timestamp[:19]

                side = fill.get('side', 'N/A').upper()
                size = float(fill.get('size', 0))
                price = float(fill.get('price', 0))
                fee = float(fill.get('fee', 0))

                print(f"{time_str:<25} {side:<6} {size:<12.6f} ${price:<11.2f} ${fee:<9.6f}")

                if side == 'BUY':
                    total_bought += size
                elif side == 'SELL':
                    total_sold += size

            print("-" * 70)
            print(f"Total Bought (24h): {total_bought:.6f} BTC")
            print(f"Total Sold (24h):   {total_sold:.6f} BTC")

            # Infer position sizing
            print("\n" + "="*80)
            print("POSITION SIZING ANALYSIS")
            print("="*80)

            if total_bought > 0:
                avg_buy_size = total_bought / (len([f for f in fills if f['side'] == 'BUY']) or 1)
                print(f"\nAverage buy size per trade: {avg_buy_size:.6f} BTC")

                # Expected: 0.22 position size
                # Estimated capital: $1,719 (from context)
                # At current price ~$61,000, 0.22 * $1,719 / $61,000 = ~0.0062 BTC per grid level
                print(f"Expected size (0.22 scale): ~0.006 BTC per grid level")

                if 0.004 < avg_buy_size < 0.010:
                    print("✅ Position sizing matches 0.22 scale expectation")
                elif avg_buy_size < 0.004:
                    print("⚠️  Position sizing appears SMALLER than 0.22 scale")
                else:
                    print("⚠️  Position sizing appears LARGER than 0.22 scale")
        else:
            print("\n⚠️  No recent fills found in last 50 orders")
            print("   (Bot may be waiting for entry signals or holding existing positions)")

    except Exception as e:
        print(f"\n❌ Error fetching fills: {e}")

    # Get open orders
    print("\n" + "="*80)
    print("OPEN ORDERS")
    print("="*80)

    try:
        orders = client.get_orders(product_id='BTC-USD', status='open')

        if orders:
            print(f"\nFound {len(orders)} open orders:")
            print(f"\n{'ID':<16} {'Side':<6} {'Size':<12} {'Price':<12} {'Status':<10}")
            print("-" * 60)

            for order in orders:
                order_id = order.get('id', 'N/A')[:16]
                side = order.get('side', 'N/A').upper()
                size = float(order.get('size', 0))
                price = float(order.get('price', 0))
                status = order.get('status', 'N/A')

                print(f"{order_id:<16} {side:<6} {size:<12.6f} ${price:<11.2f} {status:<10}")
        else:
            print("\n✅ No open orders (grid currently filled or waiting)")

    except Exception as e:
        print(f"\n❌ Error fetching orders: {e}")

    print("\n" + "="*80)

except Exception as e:
    print(f"❌ ERROR: {e}")
    print("\nMake sure you're running this in the Empire-v2 container:")
    print("  railway shell -s empire-v2")
    exit(1)
