#!/bin/bash
# =============================================================================
# CRYPTO BOT — STAGE 2: MICRO LIVE TESTING (REAL MONEY, TINY POSITIONS)
# =============================================================================
# Purpose: Verify Coinbase APIs accept real orders, confirm fills, test stops
# Duration: 24-48 hours
# Account: Your Coinbase account (separate from stock trading)
# Risk: ~$0.50 per position (1% of max position size)
# Max loss: ~$5.00 total if all positions hit stops
# =============================================================================

set -e

echo "═══════════════════════════════════════════════════════════════════════════════"
echo "⚠️  CRYPTO BOT MICRO LIVE TRADING TEST"
echo "═══════════════════════════════════════════════════════════════════════════════"
echo ""
echo "🔴 WARNING: THIS PLACES REAL ORDERS ON YOUR COINBASE LIVE ACCOUNT"
echo ""
echo "⚙️  Configuration:"
echo "   • Mode: LIVE TRADING (real orders, real fills)"
echo "   • Account: Your Coinbase LIVE account (24/7 trading)"
echo "   • Position size: $10.00 per trade (MICRO)"
echo "   • Stop-loss: 1% hard stop"
echo "   • Max risk per position: $0.10 (1% of $10)"
echo "   • Max total loss if all stop: ~$0.50 (5 positions max)"
echo ""
echo "📊 Pre-flight checklist:"
echo "   ✓ COINBASE_API_KEY_NAME set? (live account)"
echo "   ✓ COINBASE_API_PRIVATE_KEY set? (live account)"
echo "   ✓ Coinbase USD balance verified at $10+"
echo "   ✓ CRYPTO_BOT_DISABLED will be set to FALSE"
echo "   ✓ CRYPTO_MAX_ALLOCATION will be set to $10"
echo ""
echo "✅ What will happen:"
echo "   1. Pre-flight test runs (10-15 seconds)"
echo "      - Verifies API access"
echo "      - Checks minimum capital"
echo "   2. Bot scans for entry signals (every 60 seconds, 24/7)"
echo "   3. When signal found on BTC/USD or ETH/USD:"
echo "      - Places REAL market BUY order for $10 worth"
echo "      - Logs order ID and fill price"
echo "      - Sets stop-loss order (1%)"
echo "   4. Position exits when:"
echo "      - Stop-loss hit (-1%)"
echo "      - Profit target reached (3-10%)"
echo "   5. Repeat until stopped"
echo ""
echo "⏱️  Expected runtime:"
echo "   • Pre-flight test: 10-15 seconds"
echo "   • First entry: 5min-2hrs (depends on RSI signals)"
echo "   • Hold time: 30min-48hrs (crypto is 24/7)"
echo "   • Total test: 24-48 hours of monitoring"
echo ""
echo "🛑 EMERGENCY STOP:"
echo "   Terminal: Press Ctrl+C"
echo "   Set env: export CRYPTO_BOT_DISABLED=true"
echo "   (Active positions stay open but bot won't place new ones)"
echo ""
echo "═══════════════════════════════════════════════════════════════════════════════"
echo ""

# Ask for explicit confirmation
read -p "Do you want to proceed with REAL LIVE CRYPTO TRADING? Type 'yes' to continue: " confirm
if [ "$confirm" != "yes" ]; then
  echo "❌ Cancelled. No orders will be placed."
  exit 0
fi

echo ""
echo "⚠️  FINAL WARNING: Real money is at risk. Ctrl+C in next 10 seconds to cancel."
sleep 10

# Micro live configuration
export CRYPTO_BOT_DISABLED=false
export CRYPTO_MAX_ALLOCATION=10.0  # Override: only use $10 of balance

# Verify credentials exist
if [ -z "$COINBASE_API_KEY_NAME" ]; then
  echo "❌ ERROR: COINBASE_API_KEY_NAME not set"
  exit 1
fi

if [ -z "$COINBASE_API_PRIVATE_KEY" ]; then
  echo "❌ ERROR: COINBASE_API_PRIVATE_KEY not set"
  exit 1
fi

echo "✅ Credentials verified"
echo "✅ Live mode: ENABLED"
echo "✅ Position cap: $10.00 per trade"
echo "✅ Stop-loss: 1% hard stops"
echo ""
echo "Starting bot... (Ctrl+C to stop)"
echo "═══════════════════════════════════════════════════════════════════════════════"
echo ""
echo "📊 MONITORING:"
echo "   Watch logs for:"
echo "   • 'BUY' entries with order ID"
echo "   • Fill price and quantity"
echo "   • RSI/SMA signal confirmations"
echo "   • Stop-loss order confirmation"
echo "   • Exit signals and P&L"
echo ""
echo "💡 CRYPTO TIPS:"
echo "   • Market runs 24/7 (unlike stock market)"
echo "   • Signals might take hours to trigger"
echo "   • BTC/ETH are the first pairs scanned"
echo "   • RSI oversold (~30) = potential entries"
echo ""

python main.py
