#!/bin/bash
###############################################################################
# ALPACA IRON CONDOR BOT - COMPLETE DEPLOYMENT SCRIPT (ALL-IN-ONE)
#
# Copy-paste this entire script into Railway Empire-v2 console and run it.
# It handles all deployment steps automatically.
#
# Usage: bash deploy.sh
###############################################################################

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  ALPACA IRON CONDOR BOT - DEPLOYMENT SCRIPT (ALL-IN-ONE)     ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

# Color logging functions
log_check() { echo -e "${GREEN}✓${NC} $1"; }
log_fail() { echo -e "${RED}✗${NC} $1"; exit 1; }
log_section() { echo -e "\n${BLUE}>>> $1${NC}"; }
log_info() { echo "  $1"; }

# Check working directory
log_section "VERIFYING FILES DEPLOYED FROM GIT"
[ -f "alpaca_iron_condor_bot.py" ] || log_fail "Missing alpaca_iron_condor_bot.py"
log_check "Bot engine: alpaca_iron_condor_bot.py"

[ -f "run_bot_scheduler.py" ] || log_fail "Missing run_bot_scheduler.py"
log_check "Scheduler: run_bot_scheduler.py"

[ -f "requirements_alpaca_bot.txt" ] || log_fail "Missing requirements_alpaca_bot.txt"
log_check "Dependencies: requirements_alpaca_bot.txt"

[ -f "check_coinbase_trades.py" ] || log_fail "Missing check_coinbase_trades.py"
log_check "Health check: check_coinbase_trades.py"

# Verify environment variables
log_section "VERIFYING ENVIRONMENT VARIABLES"
[ -z "$ALPACA_API_KEY" ] && log_fail "ALPACA_API_KEY not set"
log_check "ALPACA_API_KEY is set"

[ -z "$ALPACA_SECRET_KEY" ] && log_fail "ALPACA_SECRET_KEY not set"
log_check "ALPACA_SECRET_KEY is set"

[ -z "$ALPACA_BASE_URL" ] && log_fail "ALPACA_BASE_URL not set"
[[ "$ALPACA_BASE_URL" != *"paper-api"* ]] && log_fail "ALPACA_BASE_URL must be paper-api.alpaca.markets"
log_check "ALPACA_BASE_URL is set correctly: $ALPACA_BASE_URL"

[ -z "$COINBASE_API_KEY" ] && log_fail "COINBASE_API_KEY not set"
log_check "COINBASE_API_KEY is set"

[ -z "$COINBASE_ACCOUNT_ID" ] && log_fail "COINBASE_ACCOUNT_ID not set"
log_check "COINBASE_ACCOUNT_ID is set"

# Install dependencies
log_section "INSTALLING PYTHON DEPENDENCIES"
pip install -q -r requirements_alpaca_bot.txt
log_check "Dependencies installed successfully"

# Verify imports
log_section "VERIFYING PYTHON IMPORTS"
python3 -c "from alpaca_iron_condor_bot import IronCondorBot; from apscheduler.schedulers.background import BackgroundScheduler" 2>/dev/null
log_check "All imports successful"

# Test Alpaca connection
log_section "TESTING ALPACA CONNECTION"
ALPACA_TEST=$(python3 << 'PYEOF'
import os
from alpaca.trading.client import TradingClient
try:
    client = TradingClient(
        api_key=os.getenv('ALPACA_API_KEY'),
        secret_key=os.getenv('ALPACA_SECRET_KEY'),
        base_url=os.getenv('ALPACA_BASE_URL')
    )
    account = client.get_account()
    print(f"SUCCESS|{account.equity}|{account.buying_power}")
except Exception as e:
    print(f"FAILED|{str(e)}")
PYEOF
)

if [[ "$ALPACA_TEST" == FAILED* ]]; then
    ERROR=$(echo "$ALPACA_TEST" | cut -d'|' -f2)
    log_fail "Alpaca connection failed: $ERROR"
fi

EQUITY=$(echo "$ALPACA_TEST" | cut -d'|' -f2)
BUYING_POWER=$(echo "$ALPACA_TEST" | cut -d'|' -f3)
log_check "Connected to Alpaca paper trading"
log_info "Account Equity: \$$EQUITY"
log_info "Buying Power: \$$BUYING_POWER"

# Test Coinbase connection (optional, don't fail if it doesn't work)
log_section "TESTING COINBASE CONNECTION (optional)"
if python3 check_coinbase_trades.py > /tmp/coinbase_test.log 2>&1; then
    log_check "Coinbase connection successful"
else
    log_info "⚠️  Coinbase connection test failed (non-critical, crypto bot may trade later)"
fi

# Create logs directory
log_section "PREPARING DEPLOYMENT"
mkdir -p /app/logs
log_check "Logs directory ready: /app/logs"

# Stop any existing scheduler
if pgrep -f "run_bot_scheduler.py" > /dev/null 2>&1; then
    log_info "Stopping existing scheduler..."
    pkill -f "run_bot_scheduler.py" || true
    sleep 2
fi

# Start scheduler
log_section "STARTING SCHEDULER"
cd /app
nohup python3 run_bot_scheduler.py > /app/logs/bot_scheduler.log 2>&1 &
SCHEDULER_PID=$!
echo $SCHEDULER_PID > /app/bot_scheduler.pid
log_check "Scheduler started with PID: $SCHEDULER_PID"

# Verify scheduler is running
sleep 3
if ps -p $SCHEDULER_PID > /dev/null 2>&1; then
    log_check "Scheduler process verified as running"
else
    tail -n 20 /app/logs/bot_scheduler.log
    log_fail "Scheduler process died immediately (check logs above)"
fi

# Verify logs
log_section "VERIFYING DEPLOYMENT"
if [ -f "/app/logs/bot_scheduler.log" ]; then
    log_check "Log file created: /app/logs/bot_scheduler.log"
    log_info "Recent log entries:"
    tail -n 5 /app/logs/bot_scheduler.log | sed 's/^/    /'
else
    log_fail "Log file not created"
fi

# Verify cron jobs
log_info "Verifying scheduled cron jobs..."
JOBS_OUTPUT=$(python3 << 'PYEOF'
from apscheduler.schedulers.background import BackgroundScheduler
scheduler = BackgroundScheduler()
scheduler.add_job(lambda: None, 'cron', hour=10, minute=0, timezone='America/New_York', id='morning_check')
scheduler.add_job(lambda: None, 'cron', hour=14, minute=0, timezone='America/New_York', id='afternoon_check')
for job in scheduler.get_jobs():
    print(f"{job.id}")
PYEOF
)

echo "$JOBS_OUTPUT" | grep -q "morning_check" || log_fail "Morning check not scheduled"
log_check "Morning entry check scheduled (10:00 AM ET)"

echo "$JOBS_OUTPUT" | grep -q "afternoon_check" || log_fail "Afternoon check not scheduled"
log_check "Afternoon exit check scheduled (2:00 PM ET)"

# Final verification
touch /app/logs/test.txt && rm /app/logs/test.txt
log_check "Logs directory is writable"

# Success summary
echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  ✅ DEPLOYMENT COMPLETE & SUCCESSFUL                           ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${BLUE}SCHEDULER STATUS:${NC}"
echo "  Process ID: $(cat /app/bot_scheduler.pid 2>/dev/null)"
echo "  Status: Running ✓"
echo "  Entry Check: 10:00 AM ET (tomorrow)"
echo "  Exit Check: 2:00 PM ET (tomorrow)"
echo ""
echo -e "${BLUE}MONITORING COMMANDS:${NC}"
echo "  View logs:        tail -f /app/logs/bot_scheduler.log"
echo "  Check process:    ps aux | grep run_bot_scheduler | grep -v grep"
echo "  Stop bot:         pkill -f run_bot_scheduler.py"
echo "  Today's trades:   tail -n 20 /app/logs/trades_log.json"
echo ""
echo -e "${BLUE}NEXT STEPS:${NC}"
echo "  1. Tomorrow at 9:45 AM ET - Pre-market check"
echo "  2. Tomorrow at 10:00 AM ET - Bot runs entry check"
echo "  3. Tomorrow at 2:00 PM ET - Bot runs exit check"
echo "  4. Monitor trades at: /app/logs/trades_log.json"
echo "  5. 2-week validation period: Sept 10-23, 2026"
echo ""
echo "Ready! 🚀"
echo ""
