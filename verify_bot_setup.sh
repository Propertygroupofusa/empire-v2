#!/bin/bash
# Verify bot setup and configuration

echo "===== BOT SETUP VERIFICATION ====="
echo ""

# Check Python files
echo "📄 Checking Python files..."
test -f bot_2_crypto_scalper.py && echo "  ✅ bot_2_crypto_scalper.py" || echo "  ❌ bot_2_crypto_scalper.py MISSING"
test -f bot_api.py && echo "  ✅ bot_api.py" || echo "  ❌ bot_api.py MISSING"
test -f main.py && echo "  ✅ main.py" || echo "  ❌ main.py MISSING"
echo ""

# Check dashboard
echo "🎨 Checking dashboard..."
test -f trading_dashboard.html && echo "  ✅ trading_dashboard.html" || echo "  ❌ trading_dashboard.html MISSING"
echo ""

# Check railway.json services
echo "🚂 Railway services configured..."
grep -q "crypto-trading-bot" railway.json && echo "  ✅ crypto-trading-bot service" || echo "  ❌ crypto-trading-bot service MISSING"
grep -q "bot-api" railway.json && echo "  ✅ bot-api service" || echo "  ❌ bot-api service MISSING"
echo ""

# Check environment variables
echo "🔑 Environment variables to set in Railway:"
echo "  • ALPACA_API_KEY — Your Alpaca API key (enables live trading)"
echo "  • ALPACA_SECRET_KEY — Your Alpaca secret key"
echo "  • ALPACA_LIVE_TRADE — Set to 'true' to enable live trading (or 'false' for demo)"
echo ""

# Check for state files (if bot has run)
echo "💾 State files (from previous runs):"
test -f bot2_state.json && echo "  ✅ bot2_state.json (exists)" || echo "  ℹ bot2_state.json (not created yet - will be created on first run)"
test -f bot2_positions.json && echo "  ✅ bot2_positions.json (exists)" || echo "  ℹ bot2_positions.json (not created yet - will be created on first run)"
test -f bot2_trades.json && echo "  ✅ bot2_trades.json (exists)" || echo "  ℹ bot2_trades.json (not created yet - will be created on first run)"
echo ""

# Check API endpoints
echo "🌐 API endpoints available:"
echo "  • /api/bot/health — Health check"
echo "  • /api/bot/status — Portfolio value, P&L, win rate"
echo "  • /api/bot/positions — Open positions"
echo "  • /api/bot/trades — Closed trades history"
echo ""

# Check dashboard URL
echo "📊 Dashboard URL:"
echo "  • Local: http://localhost:8000/trading-dashboard"
echo "  • Production: https://empire-v2-production.up.railway.app/trading-dashboard"
echo ""

echo "===== DEPLOYMENT CHECKLIST ====="
echo "Before deploying to Railway:"
echo "  1. Set ALPACA_API_KEY in Railway environment variables"
echo "  2. Set ALPACA_SECRET_KEY in Railway environment variables"
echo "  3. Set ALPACA_LIVE_TRADE to 'true' if ready for live trading"
echo "  4. Restart crypto-trading-bot service after setting env vars"
echo "  5. Restart bot-api service"
echo "  6. Check bot logs to confirm API keys are loaded"
echo ""

echo "===== VERIFICATION COMPLETE ====="
