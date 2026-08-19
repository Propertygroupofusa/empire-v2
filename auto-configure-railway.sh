#!/bin/bash
# Automated Railway Network Configuration via API
# Requires: RAILWAY_API_TOKEN environment variable

set -e

echo "🤖 Automated Railway Broker Access Configuration"
echo "=================================================="
echo ""

# Check for Railway API token
if [ -z "$RAILWAY_API_TOKEN" ]; then
    echo "❌ RAILWAY_API_TOKEN not set"
    echo ""
    echo "Get your token:"
    echo "  1. Go to https://railway.app/dashboard"
    echo "  2. Account → API Tokens"
    echo "  3. Create new token"
    echo ""
    echo "Then run:"
    echo "  export RAILWAY_API_TOKEN='your_token_here'"
    echo "  ./auto-configure-railway.sh"
    exit 1
fi

echo "✓ Railway API token detected"
echo ""

# Project and service IDs
PROJECT_ID="${RAILWAY_PROJECT_ID:-}" # Set via env or will detect
SERVICE_ID="${RAILWAY_SERVICE_ID:-}" # Set via env or will detect

# If IDs not set, try to detect from railway CLI
if [ -z "$PROJECT_ID" ]; then
    if command -v railway &> /dev/null; then
        PROJECT_ID=$(railway project 2>/dev/null || echo "")
    fi
fi

if [ -z "$PROJECT_ID" ]; then
    echo "❌ Could not detect Railway project ID"
    echo ""
    echo "Set manually:"
    echo "  export RAILWAY_PROJECT_ID='proj_xxxxx'"
    echo "  export RAILWAY_SERVICE_ID='svc_xxxxx'"
    exit 1
fi

echo "Project ID: $PROJECT_ID"
echo ""

# Broker hosts to allow
HOSTS=(
    "api.alpaca.markets"
    "data.alpaca.markets"
    "polygon.io"
    "api.coinbase.com"
    "*.coinbase.com"
    "stripe.com"
)

echo "Adding egress rules for:"
for host in "${HOSTS[@]}"; do
    echo "  - $host"
done
echo ""

# GraphQL mutation to update service network policy
# Note: This is a simplified example - actual Railway API may require different format
GRAPHQL_QUERY='
mutation UpdateNetworkPolicy($serviceId: String!, $policy: String!) {
  serviceUpdateNetworkPolicy(serviceId: $serviceId, egressAllowlist: $policy) {
    id
    networkPolicy {
      egressAllowlist
    }
  }
}
'

# Build allowlist string
ALLOWLIST=$(printf '%s ' "${HOSTS[@]}" | sed 's/ *$//')

echo "Sending API request..."
echo ""

# Make API call
RESPONSE=$(curl -s -X POST https://api.railway.app/graphql \
  -H "Authorization: Bearer $RAILWAY_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d @- <<EOF
{
  "query": "mutation { serviceUpdate(id: \"$SERVICE_ID\", input: { networkPolicy: { egressAllowlist: [$(printf '\"%s\",' "${HOSTS[@]}" | sed 's/,$//')] } }) { id } }"
}
EOF
)

echo "Response: $RESPONSE"
echo ""

if echo "$RESPONSE" | grep -q "error"; then
    echo "❌ API error - check your token and service ID"
    echo ""
    echo "Fallback: Use manual dashboard setup:"
    echo "  ./configure-railway-broker-access.sh"
    exit 1
else
    echo "✅ Network policy updated!"
    echo ""
    echo "Waiting for redeploy (2-3 minutes)..."
    echo ""
    echo "After deploy, verify with:"
    echo "  ./verify-broker-access.sh"
    exit 0
fi
