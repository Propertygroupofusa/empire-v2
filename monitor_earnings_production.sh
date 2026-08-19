#!/bin/bash
# Monitor bot earnings system on Railway production
# Usage: ./monitor_earnings_production.sh

PROD_URL="https://empire-v2-production.up.railway.app"
CYCLE=0

echo "════════════════════════════════════════════════════════════════════════════════"
echo "  BOT EARNINGS MONITOR — Railway Production"
echo "════════════════════════════════════════════════════════════════════════════════"
echo "Monitoring: $PROD_URL"
echo ""

while true; do
    CYCLE=$((CYCLE + 1))
    NOW=$(date '+%Y-%m-%d %H:%M:%S')

    echo ""
    echo "[$NOW] CYCLE #$CYCLE"
    echo "────────────────────────────────────────────────────────────────────────────────"

    # Check API health
    HEALTH=$(curl -s -w "\n%{http_code}" "$PROD_URL/health" | tail -1)

    if [ "$HEALTH" != "200" ]; then
        echo "❌ API health check failed (HTTP $HEALTH)"
        echo "   Deployment may be starting or crashed"
        sleep 30
        continue
    fi

    echo "✅ API responding"

    # Check bot earnings endpoint
    EARNINGS=$(curl -s "$PROD_URL/payments/bot/earnings")

    # Parse JSON response
    TOTAL_EARNED=$(echo "$EARNINGS" | grep -o '"total_earned":[0-9.]*' | cut -d':' -f2)
    PENDING=$(echo "$EARNINGS" | grep -o '"pending_payout":[0-9.]*' | cut -d':' -f2)
    PAID_OUT=$(echo "$EARNINGS" | grep -o '"paid_out":[0-9.]*' | cut -d':' -f2)

    if [ -z "$TOTAL_EARNED" ]; then
        echo "❌ /payments/bot/earnings returned invalid response"
        echo "$EARNINGS" | head -20
        sleep 30
        continue
    fi

    echo ""
    echo "💰 EARNINGS STATUS:"
    echo "   Total Earned:    \$$TOTAL_EARNED"
    echo "   Pending Payout:  \$$PENDING"
    echo "   Paid Out:        \$$PAID_OUT"

    # Count payment records
    PAYMENT_COUNT=$(echo "$EARNINGS" | grep -o '"id":"bot' | wc -l)
    echo "   Payment Records: $PAYMENT_COUNT"

    # Diagnostics
    echo ""
    echo "🔍 DIAGNOSTICS:"

    if (( $(echo "$TOTAL_EARNED > 0" | bc -l) )); then
        echo "   ✅ Earnings flowing - bots are profitable"
    else
        echo "   ⚠️  NO EARNINGS YET"
        echo "      - Wait for bots to complete trades"
        echo "      - Check prop_bot and btc_profit_lock_bot logs"
    fi

    if (( $(echo "$PENDING > 0" | bc -l) )); then
        echo "   ⚠️  \$$PENDING pending - verify Stripe payouts executing"
    fi

    echo ""
    echo "════════════════════════════════════════════════════════════════════════════════"
    echo "⏳ Next check in 30 seconds... (Ctrl+C to stop)"
    echo "════════════════════════════════════════════════════════════════════════════════"

    sleep 30
done
