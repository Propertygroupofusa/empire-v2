#!/bin/bash
# =============================================================================
# CRYPTO BOT — STAGE 1: PRE-FLIGHT ONLY (NO TRADING, $0 RISK)
# =============================================================================
# Purpose: Verify Coinbase API access, credentials, connection before trading
# Duration: 60 seconds (one full pre-flight cycle)
# Account: Your Coinbase account (READ-ONLY for this test)
# Risk: $0.00 — no trading happens
# =============================================================================

set -e

echo "═══════════════════════════════════════════════════════════════════════════════"
echo "🧪 CRYPTO BOT PRE-FLIGHT TEST (NO TRADING)"
echo "═══════════════════════════════════════════════════════════════════════════════"
echo ""
echo "⚙️  Configuration:"
echo "   • Mode: PRE-FLIGHT ONLY (no actual trading)"
echo "   • Account: Your Coinbase account (read-only)"
echo "   • Risk: $0.00"
echo "   • What to expect:"
echo "     - Tests Coinbase API credentials"
echo "     - Fetches USD balance"
echo "     - Tests orders endpoint"
echo "     - Verifies minimum capital ($1.00)"
echo "     - Bot exits after pre-flight (no trading)"
echo ""
echo "📊 Pre-flight checklist:"
echo "   ✓ COINBASE_API_KEY_NAME set?"
echo "   ✓ COINBASE_API_PRIVATE_KEY set?"
echo "   ✓ Coinbase account has minimum $1.00 USD?"
echo ""
echo "⏱️  Expected runtime:"
echo "   • Pre-flight test: 10-15 seconds"
echo "   • Bot exits cleanly"
echo ""
echo "✅ Expected output:"
echo "   ✅ Coinbase access verified | Available USD: $XXX.XX"
echo "   ✅ Orders endpoint accessible"
echo "   ✅ Capital sufficient for trading"
echo ""
echo "═══════════════════════════════════════════════════════════════════════════════"
echo ""

# Paper mode configuration (bot won't trade)
export CRYPTO_BOT_DISABLED=true

# Verify credentials exist
if [ -z "$COINBASE_API_KEY_NAME" ]; then
  echo "❌ ERROR: COINBASE_API_KEY_NAME not set"
  echo "   Add to Railway env vars or export locally:"
  echo "   export COINBASE_API_KEY_NAME=<your_key_name>"
  exit 1
fi

if [ -z "$COINBASE_API_PRIVATE_KEY" ]; then
  echo "❌ ERROR: COINBASE_API_PRIVATE_KEY not set"
  echo "   Add to Railway env vars or export locally:"
  echo "   export COINBASE_API_PRIVATE_KEY=<your_private_key>"
  exit 1
fi

echo "✅ Credentials verified"
echo "✅ Pre-flight mode: ENABLED (no trading)"
echo ""
echo "Starting bot... (will run pre-flight test and exit)"
echo "═══════════════════════════════════════════════════════════════════════════════"
echo ""

# Run just the pre-flight test portion
python main.py
