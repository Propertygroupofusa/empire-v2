#!/bin/bash
# RAILWAY BOT RECOVERY DEPLOYMENT SCRIPT
# De-Risk Restart with Real-Time Log Monitoring
#
# Usage: bash deploy_recovery.sh
#
# This script:
# 1. Restarts the crypto-trading service on Railway
# 2. Tails logs in real-time to verify three recovery conditions
# 3. Monitors for successful margin restoration

set -e

echo "=========================================="
echo "RAILWAY BOT RECOVERY DEPLOYMENT"
echo "=========================================="
echo ""
echo "Prerequisites:"
echo "  - Railway CLI installed (railway --version)"
echo "  - Logged in to Railway (railway login)"
echo "  - Coinbase API credentials set in Railway env"
echo ""

# Check if railway CLI is available
if ! command -v railway &> /dev/null; then
    echo "❌ Railway CLI not found"
    echo "Install: npm install -g @railway/cli"
    exit 1
fi

echo "✓ Railway CLI detected"
echo ""

# Step 1: Stop the service
echo "=========================================="
echo "STEP 1: Stopping crypto-trading service..."
echo "=========================================="
railway down crypto-trading || echo "⚠️  Service may already be stopped"
sleep 3

# Step 2: Start the service
echo ""
echo "=========================================="
echo "STEP 2: Starting crypto-trading service..."
echo "=========================================="
railway up crypto-trading &
SERVICE_PID=$!
echo "✓ Service started (PID: $SERVICE_PID)"
sleep 5

# Step 3: Tail logs with real-time monitoring
echo ""
echo "=========================================="
echo "STEP 3: Monitoring logs for recovery conditions..."
echo "=========================================="
echo ""
echo "Tailing railway logs (Ctrl+C to stop)..."
echo "-----------------------------------------"

# Function to check for recovery conditions
check_recovery_conditions() {
    local log_output="$1"
    local conditions_met=0

    # Check 1: Event loop and Semaphore bind
    if echo "$log_output" | grep -q "✓ Event loop initialized"; then
        echo "✓ [CONDITION 1] Event loop initialized"
        ((conditions_met++))
    fi

    if ! echo "$log_output" | grep -q "bound to a different event loop"; then
        echo "✓ [CONDITION 1] No Semaphore binding errors"
        ((conditions_met++))
    fi

    # Check 2: Order book sync
    if echo "$log_output" | grep -q "canceled\|refreshed\|order.*reconcil"; then
        echo "✓ [CONDITION 2] Order book sync detected"
        ((conditions_met++))
    fi

    # Check 3: Buying power restoration
    if echo "$log_output" | grep -iE "buying.power.*[0-9]{2,}" | grep -qv "0.23"; then
        echo "✓ [CONDITION 3] Buying power increased from \$0.23"
        ((conditions_met++))
    fi

    # Check 4: Circuit breaker state
    if echo "$log_output" | grep -q "PAUSED.*NORMAL\|circuit.*clear\|state.*NORMAL"; then
        echo "✓ [CONDITION 3] Circuit breaker cleared to NORMAL"
        ((conditions_met++))
    fi

    return $conditions_met
}

# Tail logs and check conditions
railway logs crypto-trading --tail 200 2>&1 | tee /tmp/railway_recovery.log &
TAIL_PID=$!

# Monitor logs for recovery conditions
echo ""
echo "Monitoring for 90 seconds..."
echo ""

START_TIME=$(date +%s)
TIMEOUT=90
LAST_CONDITION_CHECK=0

while true; do
    CURRENT_TIME=$(date +%s)
    ELAPSED=$((CURRENT_TIME - START_TIME))

    # Check conditions every 10 seconds
    if [ $((ELAPSED - LAST_CONDITION_CHECK)) -ge 10 ]; then
        echo "[${ELAPSED}s] Checking recovery conditions..."
        check_recovery_conditions "$(tail -50 /tmp/railway_recovery.log)" || true
        LAST_CONDITION_CHECK=$ELAPSED
    fi

    # Exit if timeout reached
    if [ $ELAPSED -ge $TIMEOUT ]; then
        echo ""
        echo "⏱️  Monitoring period ended (${TIMEOUT}s)"
        break
    fi

    sleep 5
done

echo ""
echo "=========================================="
echo "RECOVERY MONITORING COMPLETE"
echo "=========================================="
echo ""
echo "Full log saved to: /tmp/railway_recovery.log"
echo ""
echo "To continue monitoring:"
echo "  railway logs crypto-trading --tail 100"
echo ""
echo "To check bot status:"
echo "  railway logs crypto-trading --tail 50 | grep -E 'buying.power|equity|PAUSED|NORMAL|ERROR'"
echo ""
