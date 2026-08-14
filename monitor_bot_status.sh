#!/bin/bash
# CRITICAL: Alert if trading bots stop running

MAIN_PID=$(pgrep -f "python3 main.py" | head -1)
PROP_PID=$(pgrep -f "prop_bot.py" | head -1)
CRYPTO_PID=$(pgrep -f "crypto_coinbase_bot" | head -1)

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

if [ -z "$MAIN_PID" ]; then
  echo "[$TIMESTAMP] CRITICAL: Main app crashed! Restarting..." >> /tmp/empire-critical.log
  cd /home/user/empire-v2 && nohup python3 main.py > /tmp/main.log 2>&1 &
fi

if [ -z "$PROP_PID" ]; then
  echo "[$TIMESTAMP] CRITICAL: Prop bot crashed! Restarting..." >> /tmp/empire-critical.log
  cd /home/user/empire-v2 && nohup python3 prop_bot.py > /tmp/prop_bot.log 2>&1 &
fi

if [ -z "$CRYPTO_PID" ]; then
  echo "[$TIMESTAMP] CRITICAL: Crypto bot crashed! Restarting..." >> /tmp/empire-critical.log
  cd /home/user/empire-v2 && nohup python3 crypto_coinbase_bot.py > /tmp/crypto_bot.log 2>&1 &
fi

# Check if all are running
if [ ! -z "$MAIN_PID" ] && [ ! -z "$PROP_PID" ] && [ ! -z "$CRYPTO_PID" ]; then
  echo "[$TIMESTAMP] All bots running - Main:$MAIN_PID Prop:$PROP_PID Crypto:$CRYPTO_PID" >> /tmp/empire-monitor.log
fi
