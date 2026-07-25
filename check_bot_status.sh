#!/bin/bash
echo "=========================================="
echo "BOT STATUS CHECK"
echo "=========================================="
echo ""
echo "1. Checking for bot state file (proves bot ran):"
if [ -f bot2_state.json ]; then
    echo "✅ bot2_state.json EXISTS"
    cat bot2_state.json | head -20
else
    echo "❌ bot2_state.json NOT FOUND - bot hasn't run yet"
fi
echo ""
echo "2. Checking bot log:"
if [ -f bot2_crypto.log ]; then
    echo "Latest 10 lines:"
    tail -10 bot2_crypto.log
else
    echo "❌ bot2_crypto.log NOT FOUND"
fi
echo ""
echo "3. Checking environment variables:"
echo "ALPACA_API_KEY: ${ALPACA_API_KEY:0:10}..." 
echo "ALPACA_SECRET_KEY: ${ALPACA_SECRET_KEY:0:10}..."
echo "ALPACA_LIVE_TRADE: $ALPACA_LIVE_TRADE"
