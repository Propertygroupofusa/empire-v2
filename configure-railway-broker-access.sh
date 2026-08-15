#!/bin/bash
# Configure Railway Network Egress for Broker APIs
# Enables trading bots to connect to Alpaca & Coinbase

set -e

echo "🚀 Railway Broker API Network Configuration"
echo "=============================================="
echo ""

# Check if railway CLI is installed
if ! command -v railway &> /dev/null; then
    echo "❌ Railway CLI not found. Install it first:"
    echo "   npm install -g @railway/cli"
    echo ""
    echo "Then authenticate:"
    echo "   railway login"
    exit 1
fi

echo "✓ Railway CLI detected"
echo ""

# Get current Railway project
PROJECT_ID=$(railway project)
if [ -z "$PROJECT_ID" ]; then
    echo "❌ Not in a Railway project. Run:"
    echo "   railway link"
    exit 1
fi

echo "✓ Project ID: $PROJECT_ID"
echo ""

# Get service name (main-app)
SERVICE_NAME="main-app"
echo "✓ Target service: $SERVICE_NAME"
echo ""

# List of broker hosts to allowlist
ALLOWED_HOSTS=(
    "api.alpaca.markets"
    "data.alpaca.markets"
    "polygon.io"
    "api.coinbase.com"
    "*.coinbase.com"
    "stripe.com"
)

echo "📋 Hosts to allowlist:"
for host in "${ALLOWED_HOSTS[@]}"; do
    echo "   - $host"
done
echo ""

# Note: Railway currently requires dashboard setup for network policies
# This script documents what needs to be configured

echo "⚠️  Railway Network Configuration requires dashboard access"
echo ""
echo "Manual Setup Steps:"
echo "1. Go to https://railway.app/dashboard"
echo "2. Select project: empire-v2"
echo "3. Click service: $SERVICE_NAME"
echo "4. Go to Settings → Network (or Policies)"
echo "5. Find 'Egress Allowlist' section"
echo "6. Add these hosts:"
echo ""
for host in "${ALLOWED_HOSTS[@]}"; do
    echo "   $host"
done
echo ""
echo "7. Click 'Save' or 'Apply'"
echo "8. Wait for redeploy (2-3 minutes)"
echo ""
echo "✅ After redeploy, bots will auto-connect to brokers"
echo ""

# Verification after manual setup
echo "Verification (run after redeploy):"
echo "   ./verify-broker-access.sh"
echo ""
