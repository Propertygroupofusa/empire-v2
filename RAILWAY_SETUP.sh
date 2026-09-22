#!/bin/bash
# Railway Coinbase Credentials Setup Script
# This script sets your Coinbase API credentials in Railway via CLI

set -e

echo "============================================================"
echo "RAILWAY COINBASE CREDENTIALS SETUP"
echo "============================================================"
echo ""
echo "This script sets your Coinbase API credentials in Railway."
echo "You'll need the Railway CLI installed: npm install -g @railway/cli"
echo ""

# Check if railway CLI is installed
if ! command -v railway &> /dev/null; then
    echo "❌ Railway CLI not found. Install it first:"
    echo "   npm install -g @railway/cli"
    echo "   Then run: railway login"
    exit 1
fi

# Prompt for credentials
echo "Enter your Coinbase credentials:"
echo ""

read -p "COINBASE_API_KEY (Organization ID): " API_KEY
read -sp "COINBASE_SECRET_KEY (Private Key - will be hidden): " SECRET_KEY
echo ""
read -p "COINBASE_PASSPHRASE (any string, e.g., MyGridBot#2026): " PASSPHRASE

if [ -z "$API_KEY" ] || [ -z "$SECRET_KEY" ] || [ -z "$PASSPHRASE" ]; then
    echo "❌ All fields are required"
    exit 1
fi

echo ""
echo "============================================================"
echo "Setting variables in Railway..."
echo "============================================================"
echo ""

# Set variables in Railway
railway variable set COINBASE_API_KEY "$API_KEY" || { echo "❌ Failed to set COINBASE_API_KEY"; exit 1; }
railway variable set COINBASE_SECRET_KEY "$SECRET_KEY" || { echo "❌ Failed to set COINBASE_SECRET_KEY"; exit 1; }
railway variable set COINBASE_PASSPHRASE "$PASSPHRASE" || { echo "❌ Failed to set COINBASE_PASSPHRASE"; exit 1; }

echo "✅ COINBASE_API_KEY set"
echo "✅ COINBASE_SECRET_KEY set"
echo "✅ COINBASE_PASSPHRASE set"
echo ""

echo "============================================================"
echo "Triggering redeploy..."
echo "============================================================"
echo ""

# Redeploy by pushing an empty commit
git commit --allow-empty -m "🚀 Trigger redeploy - Coinbase credentials configured"
git push origin main

echo ""
echo "✅ Redeploy triggered!"
echo ""
echo "============================================================"
echo "NEXT STEPS:"
echo "============================================================"
echo ""
echo "1. Go to Railway Dashboard: https://railway.app"
echo "2. Select your project"
echo "3. Wait for redeploy to complete (watch the Deployments tab)"
echo "4. Once green, check your dashboard:"
echo "   - Coinbase USD balance should show real value"
echo "   - Grid Bot chart should populate with real data"
echo "   - Scale Bot chart should show tier progression"
echo ""
