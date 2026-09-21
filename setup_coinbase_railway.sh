#!/bin/bash
# Automated Coinbase Credentials Setup for Railway
# Usage: ./setup_coinbase_railway.sh <COINBASE_API_KEY> <COINBASE_API_PRIVATE_KEY>

set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Validate arguments
if [ $# -lt 2 ]; then
    echo -e "${RED}Error: Missing required arguments${NC}"
    echo "Usage: $0 <COINBASE_API_KEY> <COINBASE_API_PRIVATE_KEY>"
    echo ""
    echo -e "${YELLOW}Example:${NC}"
    echo '  $0 "org-12345678-1234-1234-1234-123456789012" "-----BEGIN EC PRIVATE KEY-----"'
    echo ""
    echo -e "${YELLOW}Arguments:${NC}"
    echo "  COINBASE_API_KEY          Your Coinbase Organization ID (starts with org-)"
    echo "  COINBASE_API_PRIVATE_KEY  Your Coinbase private key (PEM format or base64)"
    echo ""
    echo -e "${BLUE}Where to get these:${NC}"
    echo "  1. Log into https://www.coinbase.com"
    echo "  2. Go to Settings → API → Create New Key"
    echo "  3. Copy the Organization ID → that's your COINBASE_API_KEY"
    echo "  4. Copy the Private Key → that's your COINBASE_API_PRIVATE_KEY"
    echo ""
    exit 1
fi

COINBASE_API_KEY="$1"
COINBASE_API_PRIVATE_KEY="$2"

echo -e "${YELLOW}🚀 Starting Coinbase Railway Setup${NC}"
echo ""

# Check if Railway CLI is installed
if ! command -v railway &> /dev/null; then
    echo -e "${YELLOW}ℹ️  Railway CLI not found. Installing...${NC}"
    npm install -g @railway/cli
    echo ""
fi

# Validate we're in the right directory
if [ ! -f "main.py" ]; then
    echo -e "${RED}Error: main.py not found. Run this script from the empire-v2 root directory.${NC}"
    exit 1
fi

# Check if we're in a git repository
if [ ! -d ".git" ]; then
    echo -e "${RED}Error: Not a git repository.${NC}"
    exit 1
fi

echo -e "${YELLOW}🔐 Setting Coinbase API credentials...${NC}"
echo "  Setting COINBASE_API_KEY..."
railway variables set COINBASE_API_KEY "$COINBASE_API_KEY"
echo "  Setting COINBASE_API_PRIVATE_KEY..."
railway variables set COINBASE_API_PRIVATE_KEY "$COINBASE_API_PRIVATE_KEY"

echo ""
echo -e "${GREEN}✓ Environment variables set${NC}"
echo ""

# Verify variables were set
echo -e "${YELLOW}✓ Verifying variables...${NC}"
if railway variables get COINBASE_API_KEY > /dev/null 2>&1; then
    echo -e "${GREEN}  ✓ COINBASE_API_KEY${NC}"
else
    echo -e "${RED}  ✗ COINBASE_API_KEY${NC}"
fi

if railway variables get COINBASE_API_PRIVATE_KEY > /dev/null 2>&1; then
    echo -e "${GREEN}  ✓ COINBASE_API_PRIVATE_KEY${NC}"
else
    echo -e "${RED}  ✗ COINBASE_API_PRIVATE_KEY${NC}"
fi

echo ""
echo -e "${YELLOW}📋 Next steps:${NC}"
echo "  1. Go to https://railway.app/dashboard"
echo "  2. Select 'empire-v2' project"
echo "  3. Click on 'main-app' service"
echo "  4. Click 'Redeploy' to apply changes"
echo "  5. Wait 2-3 minutes for container to restart"
echo "  6. Dashboard will automatically show Coinbase balance"
echo ""
echo -e "${GREEN}✓ Setup complete! Coinbase credentials are now configured.${NC}"
echo ""
echo -e "${YELLOW}💡 Tip: Monitor your dashboard for:${NC}"
echo "    • 🪙 Coinbase: \$[amount] (real USD balance)"
echo "    • 📈 Alpaca: \$[amount] (equity balance)"
echo "    • Both updating every 30-60 seconds"
echo ""
