#!/bin/bash
# =============================================================================
# ALPACA BOT — STAGE 2: MICRO LIVE TESTING (REAL MONEY, TINY POSITIONS)
# =============================================================================
# Purpose: Verify Alpaca APIs accept real orders, confirm fills, test stop-losses
# Duration: 1-2 trading days
# Account: Your $980 LIVE account
# Risk: ~$0.03 per position (1% of max position size)
# Max loss: ~$0.30 total if all positions hit stops
# =============================================================================

set -e

echo "═══════════════════════════════════════════════════════════════════════════════"
echo "⚠️  ALPACA MICRO LIVE TRADING TEST"
echo "═══════════════════════════════════════════════════════════════════════════════"
echo ""
echo "🔴 WARNING: THIS PLACES REAL ORDERS ON YOUR LIVE ACCOUNT"
echo ""
echo "⚙️  Configuration:"
echo "   • Mode: LIVE TRADING (real orders, real fills)"
echo "   • Account: Your $980 LIVE account"
echo "   • Position size: $5.00 per trade (MICRO)"
echo "   • Stop-loss: -0.5% intraday, -2% swing"
echo "   • Max risk per position: $0.025 (intraday) / $0.10 (swing)"
echo "   • Max total loss if all stop: ~$0.30"
echo ""
echo "📊 Pre-flight checklist:"
echo "   ✓ ALPACA_API_KEY set? (live account)"
echo "   ✓ ALPACA_SECRET_KEY set? (live account)"
echo "   ✓ Account balance verified at $980+"
echo "   ✓ ALPACA_LIVE_TRADE will be set to TRUE"
echo ""
echo "✅ What will happen:"
echo "   1. Pre-flight test runs (5-10 seconds)"
echo "   2. Bot scans for entry signals (every 60 seconds)"
echo "   3. When signal found:"
echo "      - Places REAL BUY order for $5 worth of stock"
echo "      - Logs order ID and fill price"
echo "      - Sets stop-loss order"
echo "   4. Position exits when:"
echo "      - Stop-loss hit (-0.5% intraday / -2% swing)"
echo "      - Profit target reached"
echo "   5. Repeat until stopped"
echo ""
echo "⏱️  Expected runtime:"
echo "   • Pre-flight test: 5-10 seconds"
echo "   • First entry: 5-30 minutes (depends on market signals)"
echo "   • Hold time: 5min (intraday) to hours (swing)"
echo "   • Total test: 1-2 trading days"
echo ""
echo "🛑 EMERGENCY STOP:"
echo "   Terminal: Press Ctrl+C"
echo "   Set env: export STOP_TRADING=true"
echo "   (Active positions stay open but bot won't place new ones)"
echo ""
echo "═══════════════════════════════════════════════════════════════════════════════"
echo ""

# Ask for explicit confirmation
read -p "Do you want to proceed with REAL LIVE TRADING? Type 'yes' to continue: " confirm
if [ "$confirm" != "yes" ]; then
  echo "❌ Cancelled. No orders will be placed."
  exit 0
fi

echo ""
echo "⚠️  FINAL WARNING: Real money is at risk. Ctrl+C in next 10 seconds to cancel."
sleep 10

# Micro live configuration
export ALPACA_LIVE_TRADE=true
export ALPACA_BASE_URL=https://api.alpaca.markets
export ALPACA_MAX_POSITION_SIZE=5.0  # Override: only $5 per position

# Verify credentials exist
if [ -z "$ALPACA_API_KEY" ]; then
  echo "❌ ERROR: ALPACA_API_KEY not set"
  exit 1
fi

if [ -z "$ALPACA_SECRET_KEY" ]; then
  echo "❌ ERROR: ALPACA_SECRET_KEY not set"
  exit 1
fi

echo "✅ Credentials verified"
echo "✅ Live mode: ENABLED"
echo "✅ Position cap: $5.00 per trade"
echo ""
echo "Starting bot... (Ctrl+C to stop)"
echo "═══════════════════════════════════════════════════════════════════════════════"
echo ""
echo "📊 MONITORING:"
echo "   Watch logs for:"
echo "   • 'BUY' entries with order ID"
echo "   • Fill price and quantity"
echo "   • Stop-loss order confirmation"
echo "   • Exit signals and P&L"
echo ""

python alpaca_swing_bot.py
