#!/bin/bash
###############################################################################
# ALPACA BOT - DAILY MONITORING SCRIPT (ALL-IN-ONE)
#
# Run this script daily to check bot status, verify it's running,
# and review trades from the day.
#
# Usage: bash monitor.sh
###############################################################################

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_ok() { echo -e "${GREEN}✓${NC} $1"; }
log_warn() { echo -e "${YELLOW}⚠${NC} $1"; }
log_fail() { echo -e "${RED}✗${NC} $1"; }
log_section() { echo -e "\n${BLUE}>>> $1${NC}"; }

echo -e "${BLUE}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  ALPACA BOT - DAILY MONITORING CHECK                          ║${NC}"
echo -e "${BLUE}║  $(date '+%Y-%m-%d %H:%M:%S %Z')${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

# Check 1: Scheduler running
log_section "1. SCHEDULER STATUS"
if ps aux | grep -v grep | grep -q "run_bot_scheduler.py"; then
    PID=$(pgrep -f "run_bot_scheduler.py")
    log_ok "Scheduler is running (PID: $PID)"
else
    log_fail "Scheduler is NOT running"
    echo ""
    echo -e "${YELLOW}To restart:${NC}"
    echo "  cd /app"
    echo "  nohup python3 run_bot_scheduler.py > /app/logs/bot_scheduler.log 2>&1 &"
    echo ""
fi

# Check 2: Log file exists
log_section "2. LOG FILES"
if [ -f "/app/logs/bot_scheduler.log" ]; then
    SIZE=$(ls -lh /app/logs/bot_scheduler.log | awk '{print $5}')
    log_ok "Scheduler log exists ($SIZE)"
    log_section "Recent log entries:"
    tail -n 10 /app/logs/bot_scheduler.log | sed 's/^/  /'
else
    log_warn "Scheduler log not found"
fi

# Check 3: Trades log
log_section "3. TRADES LOG"
if [ -f "/app/logs/trades_log.json" ]; then
    TRADE_COUNT=$(wc -l < /app/logs/trades_log.json)
    SIZE=$(ls -lh /app/logs/trades_log.json | awk '{print $5}')
    log_ok "Trades log exists ($TRADE_COUNT lines, $SIZE)"
    log_section "Latest trades:"
    tail -n 3 /app/logs/trades_log.json | python3 -m json.tool 2>/dev/null || tail -n 3 /app/logs/trades_log.json | sed 's/^/  /'
else
    log_warn "Trades log not found yet (no trades today)"
fi

# Check 4: Alpaca connectivity
log_section "4. ALPACA CONNECTIVITY"
ALPACA_CHECK=$(python3 << 'PYEOF'
import os
from alpaca.trading.client import TradingClient
try:
    client = TradingClient(
        api_key=os.getenv('ALPACA_API_KEY'),
        secret_key=os.getenv('ALPACA_SECRET_KEY'),
        base_url=os.getenv('ALPACA_BASE_URL')
    )
    account = client.get_account()
    print(f"OK|{account.equity}|{account.buying_power}")
except Exception as e:
    print(f"FAIL|{str(e)}")
PYEOF
)

if [[ "$ALPACA_CHECK" == OK* ]]; then
    EQUITY=$(echo "$ALPACA_CHECK" | cut -d'|' -f2)
    BP=$(echo "$ALPACA_CHECK" | cut -d'|' -f3)
    log_ok "Connected to Alpaca"
    echo "  Equity: \$$EQUITY"
    echo "  Buying Power: \$$BP"
else
    ERROR=$(echo "$ALPACA_CHECK" | cut -d'|' -f2)
    log_fail "Alpaca connection failed: $ERROR"
fi

# Check 5: Open positions
log_section "5. OPEN POSITIONS"
POSITIONS=$(python3 << 'PYEOF'
import os
from alpaca.trading.client import TradingClient
try:
    client = TradingClient(
        api_key=os.getenv('ALPACA_API_KEY'),
        secret_key=os.getenv('ALPACA_SECRET_KEY'),
        base_url=os.getenv('ALPACA_BASE_URL')
    )
    positions = client.get_positions()
    print(f"COUNT|{len(positions)}")
    for pos in positions:
        print(f"POS|{pos.symbol}|{pos.qty}|{pos.current_price}|{pos.unrealized_pl}")
except Exception as e:
    print(f"ERROR|{str(e)}")
PYEOF
)

if [[ "$POSITIONS" == COUNT* ]]; then
    COUNT=$(echo "$POSITIONS" | grep "^COUNT" | cut -d'|' -f2)
    log_ok "No open positions"
    if [ "$COUNT" != "0" ]; then
        echo "  Current positions:"
        echo "$POSITIONS" | grep "^POS" | while IFS='|' read -r type symbol qty price pnl; do
            echo "    $symbol: $qty shares @ \$$price (P&L: \$$pnl)"
        done
    fi
else
    log_warn "Could not check positions"
fi

# Check 6: System resources
log_section "6. SYSTEM RESOURCES"
DISK=$(df -h /app | tail -1 | awk '{print $5}')
MEM=$(free -h | grep Mem | awk '{print $3 "/" $2}')
log_ok "Disk usage: $DISK"
log_ok "Memory usage: $MEM"

# Check 7: Market status
log_section "7. MARKET STATUS"
MARKET_CHECK=$(python3 << 'PYEOF'
from datetime import datetime, time
import pytz

ny_tz = pytz.timezone('America/New_York')
now = datetime.now(ny_tz)
market_open = time(9, 30)
market_close = time(16, 0)
weekday = now.weekday()

if weekday >= 5:  # Saturday or Sunday
    print("CLOSED|Weekend")
elif now.time() < market_open:
    print("CLOSED|Before market open")
elif now.time() >= market_close:
    print("CLOSED|After market close")
else:
    print("OPEN|Market is trading")
PYEOF
)

STATUS=$(echo "$MARKET_CHECK" | cut -d'|' -f1)
MSG=$(echo "$MARKET_CHECK" | cut -d'|' -f2)

if [ "$STATUS" = "OPEN" ]; then
    log_ok "$MSG (9:30 AM - 4:00 PM ET)"
else
    log_warn "$MSG"
fi

# Summary
echo ""
echo -e "${BLUE}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  MONITORING CHECK COMPLETE                                    ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${BLUE}QUICK REFERENCE:${NC}"
echo "  View live logs:      tail -f /app/logs/bot_scheduler.log"
echo "  Check scheduler:     ps aux | grep run_bot_scheduler | grep -v grep"
echo "  View today's trades: tail -n 20 /app/logs/trades_log.json"
echo "  Restart scheduler:   pkill -f run_bot_scheduler.py && nohup python3 run_bot_scheduler.py > /app/logs/bot_scheduler.log 2>&1 &"
echo ""
