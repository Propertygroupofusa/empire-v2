#!/usr/bin/env python3
"""
Diagnostic script to verify Coinbase API credentials are properly set up.
Run this to check if COINBASE_API_KEY and COINBASE_API_PRIVATE_KEY are available.
"""
import os
import sys

print("=" * 70)
print("COINBASE API CREDENTIAL DIAGNOSTIC")
print("=" * 70)
print()

# Check for credentials
coinbase_key = os.getenv('COINBASE_API_KEY', '')
coinbase_private = os.getenv('COINBASE_API_PRIVATE_KEY', '')

print("STATUS CHECK:")
print(f"  COINBASE_API_KEY present:           {bool(coinbase_key)}")
print(f"  COINBASE_API_PRIVATE_KEY present:   {bool(coinbase_private)}")
print()

if not coinbase_key or not coinbase_private:
    print("❌ PROBLEM DETECTED: Credentials are missing!")
    print()
    print("TO FIX THIS:")
    print()
    print("1. Log into Railway: https://railway.app")
    print("2. Select your 'empire-v2' project")
    print("3. Go to Settings → Variables")
    print("4. Add these environment variables:")
    print()
    print("   COINBASE_API_KEY = <your-org-id>")
    print("       (e.g., org-12345678-1234-1234-1234-123456789012)")
    print()
    print("   COINBASE_API_PRIVATE_KEY = <your-private-key>")
    print("       (PEM format or base64 Ed25519 key)")
    print()
    print("5. Click 'Redeploy' for the main-app service")
    print("6. Wait 2-3 minutes for the new container to start")
    print()
    print("QUESTIONS:")
    print("  - If you have the credentials, post them in your .env file locally")
    print("  - Then set them manually in Railway dashboard")
    print("  - The dashboard will auto-detect them on next cycle")
    print()
    sys.exit(1)
else:
    print("✅ CREDENTIALS FOUND!")
    print()
    print("DETAILS:")
    print(f"  COINBASE_API_KEY length:         {len(coinbase_key)}")
    print(f"  COINBASE_API_KEY preview:        {coinbase_key[:30]}...")
    print()
    print(f"  COINBASE_API_PRIVATE_KEY length: {len(coinbase_private)}")
    key_format = "PEM (EC)" if coinbase_private.startswith("-----BEGIN") else "Base64 (Ed25519)"
    print(f"  COINBASE_API_PRIVATE_KEY format: {key_format}")
    print(f"  COINBASE_API_PRIVATE_KEY preview: {coinbase_private[:50]}...")
    print()
    print("The credentials appear to be properly configured.")
    print("If you're still getting 401 errors, the JWT signing might be failing.")
    print("Check the application logs for detailed error messages.")
    sys.exit(0)
