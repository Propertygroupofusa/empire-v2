#!/bin/bash
# Verify Broker API Access from Railway Container
# Run this after egress allowlist is configured and deployed

echo "🔍 Broker API Access Verification"
echo "===================================="
echo ""

# Test hosts and their critical endpoints
declare -A BROKER_TESTS=(
    ["api.alpaca.markets"]="/v2/account"
    ["data.alpaca.markets"]="/v1/bars"
    ["api.coinbase.com"]="/v1/accounts"
    ["stripe.com"]="/v1/charges"
)

PASS=0
FAIL=0

for host in "${!BROKER_TESTS[@]}"; do
    endpoint="${BROKER_TESTS[$host]}"

    echo -n "Testing $host... "

    # Try DNS resolution first
    if ! nslookup "$host" &> /dev/null; then
        echo "❌ DNS FAILED (host unreachable)"
        ((FAIL++))
        continue
    fi

    # Try HTTPS connection (without auth, just to test network access)
    if curl -s --connect-timeout 3 "https://$host$endpoint" > /dev/null 2>&1; then
        echo "✓ OPEN (network access confirmed)"
        ((PASS++))
    elif curl -s --connect-timeout 3 -I "https://$host" 2>&1 | grep -q "HTTP"; then
        echo "✓ OPEN (TLS handshake OK)"
        ((PASS++))
    else
        echo "❌ BLOCKED (403/timeout - check egress allowlist)"
        ((FAIL++))
    fi
done

echo ""
echo "────────────────────────────────────"
echo "Results: $PASS passed, $FAIL blocked"
echo "────────────────────────────────────"
echo ""

if [ $FAIL -eq 0 ]; then
    echo "✅ All broker APIs reachable!"
    echo ""
    echo "Next: Bots should auto-start trading"
    echo "Monitor earnings: /payments/bot/earnings"
    exit 0
else
    echo "❌ Some brokers still blocked"
    echo ""
    echo "Action: Re-check Railway egress allowlist:"
    echo "  - Hosts added correctly?"
    echo "  - Service redeployed?"
    echo "  - Wait 2-3 min after deploy before retesting"
    exit 1
fi
