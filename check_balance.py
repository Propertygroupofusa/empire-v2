#!/usr/bin/env python3
"""Check live Alpaca account balance."""
import os
import json
import urllib.request
import urllib.error

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = "https://api.alpaca.markets"

if not API_KEY or not SECRET_KEY:
    print("❌ Missing ALPACA_API_KEY or ALPACA_SECRET_KEY")
    exit(1)

try:
    url = f"{BASE_URL}/v2/account"
    headers = {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
    }
    req = urllib.request.Request(url, headers=headers)

    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode())

        print("=" * 70)
        print("🏦 ALPACA LIVE ACCOUNT (APEX_589296)")
        print("=" * 70)
        print(f"Portfolio Value:    ${float(data.get('portfolio_value', 0)):>15,.2f}")
        print(f"Cash Available:     ${float(data.get('cash', 0)):>15,.2f}")
        print(f"Buying Power:       ${float(data.get('buying_power', 0)):>15,.2f}")
        print(f"Unrealized P&L:     ${float(data.get('unrealized_pl', 0)):>15,.2f}")
        print(f"Unrealized P&L %:   {float(data.get('unrealized_plpc', 0)):>15.2f}%")
        print(f"Account Status:     {data.get('status', 'unknown'):>15}")
        print(f"Trading Mode:       {'🔴 LIVE' if 'api.alpaca.markets' in BASE_URL else '📄 PAPER':>15}")
        print("=" * 70)

        # Calculate change from starting capital
        starting = 980.0
        current = float(data.get('portfolio_value', 0))
        profit = current - starting
        profit_pct = (profit / starting * 100) if starting > 0 else 0

        print(f"\n📊 PROFIT TRACKING")
        print(f"Starting Capital:   ${starting:>15,.2f}")
        print(f"Current Value:      ${current:>15,.2f}")
        print(f"Total Profit:       ${profit:>15,.2f}")
        print(f"Return %:           {profit_pct:>15.2f}%")

        # Milestones
        print(f"\n🎯 MILESTONES")
        milestones = [1000, 5000, 10000, 25000, 50000, 100000, 250000, 500000, 1000000]
        for target in milestones:
            if current >= target:
                print(f"   ${target:>7,.0f} ✅ REACHED")
            else:
                remaining = target - current
                pct_complete = (current / target * 100)
                print(f"   ${target:>7,.0f} → ${remaining:>10,.2f} ({pct_complete:>5.1f}% complete)")

        # Get positions
        url_pos = f"{BASE_URL}/v2/positions"
        req_pos = urllib.request.Request(url_pos, headers=headers)
        with urllib.request.urlopen(req_pos, timeout=10) as r_pos:
            positions = json.loads(r_pos.read().decode())

            if positions:
                print(f"\n📈 OPEN POSITIONS ({len(positions)})")
                for pos in positions:
                    symbol = pos.get('symbol', '?')
                    qty = float(pos.get('qty', 0))
                    market_value = float(pos.get('market_value', 0))
                    unrealized = float(pos.get('unrealized_pl', 0))
                    unrealized_pct = float(pos.get('unrealized_plpc', 0))
                    print(f"   {symbol:>8} x {qty:>6.0f} | ${market_value:>10,.2f} | P&L: ${unrealized:>+8,.2f} ({unrealized_pct:>+5.2f}%)")
            else:
                print(f"\n📈 No open positions")

        print("\n" + "=" * 70)

except Exception as e:
    print(f"❌ Error connecting to Alpaca: {e}")
    exit(1)
