#!/usr/bin/env bash
# Parse trading-related logs and summarize recent balances and trades
# Usage: bash scripts/parse_trading_logs.sh [logfile] [lines]
# Defaults: logfile=/tmp/empire_server.log lines=200

LOGFILE=${1:-/tmp/empire_server.log}
LINES=${2:-200}

if [ ! -f "$LOGFILE" ]; then
  echo "Log file not found: $LOGFILE"
  exit 2
fi

echo "Parsing last $LINES lines from $LOGFILE"

# Tail the log and look for balance lines and trade/order lines

echo
echo "-- Recent Balance Lines (Equity / Cash) --"

tail -n "$LINES" "$LOGFILE" | grep -E "Equity: \$|Cash: \$" || echo "(none found)"

echo
echo "-- Recent Connection Status (Alpaca / Coinbase) --"

tail -n "$LINES" "$LOGFILE" | grep -iE "Alpaca account connected|Alpaca|Coinbase|connected successfully" || echo "(none found)"

echo
echo "-- Recent Trade / Order Lines --"

tail -n "$LINES" "$LOGFILE" | grep -iE "order|trade executed|filled|placed|buy|sell|executed" || echo "(none found)"

echo
echo "-- Last 20 Log Lines (context) --"

tail -n 20 "$LOGFILE"

echo
echo "Done. If you see balance lines, copy them here and I'll interpret. If you see trade/order lines, paste a few samples for analysis."
