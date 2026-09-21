#!/usr/bin/env python3
"""
Verify that Coinbase API credentials are valid and can authenticate.
Run this after setting COINBASE_API_KEY and COINBASE_API_PRIVATE_KEY in Railway.
"""
import os
import sys
import asyncio
import aiohttp

# Import the auth functions from the bot
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import crypto_btc_compound_bot as bot
except ImportError:
    print("❌ Error: Could not import crypto_btc_compound_bot module")
    sys.exit(1)

async def test_coinbase_auth():
    """Test if we can authenticate and fetch account info from Coinbase."""
    print("=" * 70)
    print("COINBASE AUTHENTICATION TEST")
    print("=" * 70)
    print()

    # Check if credentials are available
    if not bot.COINBASE_API_KEY_NAME:
        print("❌ COINBASE_API_KEY not found in environment")
        print()
        print("To fix:")
        print("  1. In Railway dashboard: go to Variables")
        print("  2. Add COINBASE_API_KEY = [your-org-id]")
        print("  3. Click Redeploy")
        sys.exit(1)

    if not bot.COINBASE_API_PRIVATE_KEY:
        print("❌ COINBASE_API_PRIVATE_KEY not found in environment")
        print()
        print("To fix:")
        print("  1. In Railway dashboard: go to Variables")
        print("  2. Add COINBASE_API_PRIVATE_KEY = [your-private-key]")
        print("  3. Click Redeploy")
        sys.exit(1)

    print("✓ Credentials found in environment")
    print(f"  API Key: {bot.COINBASE_API_KEY_NAME[:20]}...")
    print(f"  Private Key format: {'PEM (EC)' if bot.COINBASE_API_PRIVATE_KEY.startswith('-----BEGIN') else 'Base64 (Ed25519)'}")
    print()

    # Try to authenticate
    print("Testing authentication...")
    try:
        async with aiohttp.ClientSession() as session:
            balance, error = await bot.get_usd_balance(session)

            if error:
                print(f"❌ Authentication failed: {error}")
                print()
                if "401" in str(error):
                    print("HTTP 401 means Coinbase rejected the credentials:")
                    print("  • Check that API key is correct")
                    print("  • Check that private key matches")
                    print("  • Verify key wasn't corrupted during copy/paste")
                    print("  • Try recreating the key in Coinbase dashboard")
                sys.exit(1)
            elif balance is not None:
                print(f"✅ SUCCESS! Coinbase API authenticated")
                print(f"   Current USD Balance: ${balance:.2f}")
                print()
                print("Your dashboard will now show this balance automatically.")
                print("Updates every 30-60 seconds on the dashboard header.")
                sys.exit(0)
            else:
                print("❓ Unexpected result: no balance or error")
                sys.exit(1)

    except Exception as e:
        print(f"❌ Error during authentication test: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(test_coinbase_auth())
