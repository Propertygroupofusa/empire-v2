#!/usr/bin/env python3
"""Test Alpaca API connection with provided credentials"""
import os
import json
import urllib.request
import urllib.error

# Your Alpaca keys
API_KEY = os.getenv("ALPACA_API_KEY", "")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
BASE_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"

print("=" * 60)
print("ALPACA CONNECTION TEST")
print("=" * 60)

# Check if keys are set
print(f"\n✓ API_KEY set: {bool(API_KEY)}")
print(f"✓ SECRET_KEY set: {bool(SECRET_KEY)}")

if not API_KEY or not SECRET_KEY:
    print("\n❌ MISSING CREDENTIALS - Bot cannot trade")
    print("   Set ALPACA_API_KEY and ALPACA_SECRET_KEY in Railway")
    exit(1)

# Test connection
def test_account(url, label):
    print(f"\n📡 Testing {label}...")
    headers = {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
    }
    try:
        req = urllib.request.Request(f"{url}/v2/account", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
            print(f"  ✅ Connection successful")
            print(f"  Portfolio Value: ${float(data.get('portfolio_value', 0)):,.2f}")
            print(f"  Cash: ${float(data.get('cash', 0)):,.2f}")
            print(f"  Buying Power: ${float(data.get('buying_power', 0)):,.2f}")
            print(f"  Account Status: {data.get('status')}")
            return data
    except urllib.error.HTTPError as e:
        print(f"  ❌ HTTP Error {e.code}: {e.reason}")
        try:
            error_body = e.read().decode()
            print(f"     {error_body}")
        except:
            pass
        return None
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return None

# Test paper and live
paper = test_account(BASE_URL, "PAPER (Demo)")
live = test_account(LIVE_URL, "LIVE")

print("\n" + "=" * 60)
if paper or live:
    print("✅ At least one connection working - bot can trade")
else:
    print("❌ No connection - check API key validity")
print("=" * 60)
