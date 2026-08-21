#!/bin/bash
# =============================================================================
# ALPACA BOT — STAGE 1: PAPER TRADING (DRY-RUN, $0 RISK)
# =============================================================================
# Purpose: Test order logic, entry signals, stop-loss triggers with ZERO real money
# Duration: 1-2 trading days to see full cycle (entry → exit)
# Account: Paper trading ($25k virtual balance)
# Risk: $0.00 — no real orders placed
# =============================================================================

set -e

echo "═══════════════════════════════════════════════════════════════════════════════"
echo "🧪 ALPACA PAPER TRADING TEST"
echo "═══════════════════════════════════════════════════════════════════════════════"
echo ""
echo "⚙️  Configuration:"
echo "   • Mode: PAPER TRADING (dry-run, no real fills)"
echo "   • Account: Virtual $25k paper balance"
echo "   • Risk: $0.00"
echo "   • What to expect:"
echo "     - Logs all entry/exit signals"
echo "     - Shows would-be position sizes"
echo "     - Simulates stop-loss triggers"
echo "     - No actual orders sent to Alpaca"
echo ""
echo "📊 Pre-flight checklist:"
echo "   ✓ ALPACA_API_KEY set?"
echo "   ✓ ALPACA_SECRET_KEY set?"
echo "   ✓ ALPACA_BASE_URL pointing to paper-trading.alpaca.markets?"
echo ""
echo "⏱️  Expected runtime:"
echo "   • Pre-flight test: 5-10 seconds"
echo "   • Live signal scanning: ~60 second cycles"
echo "   • First entry signal: 5-30 minutes (depends on market)"
echo "   • Full cycle (entry→exit): 1-2 trading days"
echo ""
echo "🛑 How to stop: Press Ctrl+C"
echo "═══════════════════════════════════════════════════════════════════════════════"
echo ""

# Paper trading configuration
export ALPACA_LIVE_TRADE=false
export ALPACA_BASE_URL=https://paper-trading.alpaca.markets

# Verify credentials exist
if [ -z "$ALPACA_API_KEY" ]; then
  echo "❌ ERROR: ALPACA_API_KEY not set"
  echo "   Add to Railway env vars or export locally:"
  echo "   export ALPACA_API_KEY=<your_key>"
  exit 1
fi

if [ -z "$ALPACA_SECRET_KEY" ]; then
  echo "❌ ERROR: ALPACA_SECRET_KEY not set"
  echo "   Add to Railway env vars or export locally:"
  echo "   export ALPACA_SECRET_KEY=<your_secret>"
  exit 1
fi

echo "✅ Credentials verified"
echo "✅ Paper mode: ENABLED"
echo ""
echo "Starting bot... (Ctrl+C to stop)"
echo "═══════════════════════════════════════════════════════════════════════════════"
echo ""

python alpaca_swing_bot.py
