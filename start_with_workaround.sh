#!/bin/bash
# Start Empire v2 with network access workaround enabled
# Use this while waiting for Railway to add egress whitelist

set -e

cd "$(dirname "$0")"

echo "=================================================="
echo "🚀 EMPIRE v2 — Starting with Network Workaround"
echo "=================================================="

# Load workaround env vars
if [ -f .env.workaround ]; then
    echo "📋 Loading workaround configuration from .env.workaround..."
    export $(grep -v '^#' .env.workaround | xargs)
else
    echo "⚠️  .env.workaround not found, setting defaults..."
    export NETWORK_RETRY_ATTEMPTS=5
    export NETWORK_CACHE_TTL=600
    export CRYPTO_BOT_SKIP_ENTRIES_ON_API_FAILURE=true
    export ALPACA_BOT_SKIP_ENTRIES_ON_API_FAILURE=true
    export FALLBACK_PRICE_MODE=skip
fi

echo ""
echo "📊 Network Workaround Settings:"
echo "  • NETWORK_RETRY_ATTEMPTS: ${NETWORK_RETRY_ATTEMPTS:-3}"
echo "  • NETWORK_CACHE_TTL: ${NETWORK_CACHE_TTL:-300}s"
echo "  • CRYPTO_BOT_SKIP_ENTRIES: ${CRYPTO_BOT_SKIP_ENTRIES_ON_API_FAILURE:-false}"
echo "  • FALLBACK_PRICE_MODE: ${FALLBACK_PRICE_MODE:-cached}"
echo ""
echo "📌 Expected Behavior:"
echo "  ✓ Bots will retry failed API calls (with backoff)"
echo "  ✓ Successful responses cached for ${NETWORK_CACHE_TTL:-300}s"
echo "  ✓ New entries SKIPPED when Coinbase/Alpaca APIs blocked"
echo "  ✓ Exits CONTINUE on open positions"
echo "  ✓ Video order system WORKS normally"
echo ""
echo "⚠️  To restore FULL trading (new entries + exits):"
echo "  1. Go to Railway Dashboard → empire-v2 → main-app → Settings → Network"
echo "  2. Add these to Egress Allowlist:"
echo "     • api.coinbase.com"
echo "     • api.alpaca.markets"
echo "     • data.alpaca.markets"
echo "     • stripe.com"
echo "  3. Click Save and Redeploy"
echo "  4. Restart this script (new entries will resume)"
echo ""
echo "=================================================="
echo "✓ Starting FastAPI server on port 8000..."
echo "=================================================="
echo ""

# Clean up any existing server
pkill -f "python main.py" || true
sleep 2

# Start server with workaround env vars loaded
python main.py
