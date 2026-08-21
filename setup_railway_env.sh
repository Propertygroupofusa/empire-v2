#!/bin/bash
# Automated Railway Environment Setup Script
# Usage: ./setup_railway_env.sh <ALPACA_API_KEY> <ALPACA_SECRET_KEY> [ALPACA_BASE_URL]

set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Validate arguments
if [ $# -lt 2 ]; then
    echo -e "${RED}Error: Missing required arguments${NC}"
    echo "Usage: $0 <ALPACA_API_KEY> <ALPACA_SECRET_KEY> [ALPACA_BASE_URL]"
    echo ""
    echo "Example:"
    echo "  $0 'your_api_key' 'your_secret_key' 'https://paper-api.alpaca.markets'"
    echo ""
    echo "Arguments:"
    echo "  ALPACA_API_KEY        Your Alpaca API key ID"
    echo "  ALPACA_SECRET_KEY     Your Alpaca secret key"
    echo "  ALPACA_BASE_URL       (Optional) Paper or live API endpoint"
    echo "                        Default: https://paper-api.alpaca.markets"
    exit 1
fi

ALPACA_API_KEY="$1"
ALPACA_SECRET_KEY="$2"
ALPACA_BASE_URL="${3:-https://paper-api.alpaca.markets}"

echo -e "${YELLOW}🚀 Starting Railway Environment Setup${NC}"
echo ""

# Check if Railway CLI is installed
if ! command -v railway &> /dev/null; then
    echo -e "${YELLOW}ℹ️  Railway CLI not found. Installing...${NC}"
    npm install -g @railway/cli
fi

echo -e "${YELLOW}📝 Configuring environment variables...${NC}"
echo ""

# Validate we're in the right directory
if [ ! -f "main.py" ]; then
    echo -e "${RED}Error: main.py not found. Run this script from the empire-v2 root directory.${NC}"
    exit 1
fi

# Check if we're in a git repository with Railway configured
if [ ! -d ".git" ]; then
    echo -e "${RED}Error: Not a git repository. Railway requires git integration.${NC}"
    exit 1
fi

echo -e "${YELLOW}🔐 Setting Alpaca API credentials...${NC}"
railway variables set ALPACA_API_KEY "$ALPACA_API_KEY"
railway variables set ALPACA_SECRET_KEY "$ALPACA_SECRET_KEY"
railway variables set ALPACA_BASE_URL "$ALPACA_BASE_URL"

echo -e "${GREEN}✓ Environment variables set${NC}"
echo ""

# Verify variables were set
echo -e "${YELLOW}✓ Verifying variables...${NC}"
railway variables get ALPACA_API_KEY > /dev/null && echo -e "${GREEN}  ✓ ALPACA_API_KEY${NC}" || echo -e "${RED}  ✗ ALPACA_API_KEY${NC}"
railway variables get ALPACA_SECRET_KEY > /dev/null && echo -e "${GREEN}  ✓ ALPACA_SECRET_KEY${NC}" || echo -e "${RED}  ✗ ALPACA_SECRET_KEY${NC}"
railway variables get ALPACA_BASE_URL > /dev/null && echo -e "${GREEN}  ✓ ALPACA_BASE_URL${NC}" || echo -e "${RED}  ✗ ALPACA_BASE_URL${NC}"

echo ""
echo -e "${YELLOW}📋 Next steps:${NC}"
echo "  1. Go to https://railway.app/dashboard"
echo "  2. Select 'empire-v2' → 'main-app' service"
echo "  3. Click 'Redeploy' to apply environment changes"
echo "  4. Wait 2-3 minutes for container restart"
echo "  5. Visit: https://empire-v2-production.up.railway.app/live-dashboard"
echo ""
echo -e "${GREEN}✓ Setup complete! Alpaca credentials are now configured.${NC}"
echo ""
echo -e "${YELLOW}💡 Tip: Monitor logs in Railway dashboard for:${NC}"
echo "    • 💰 PROFIT LOCK - Coinbase bot taking profits"
echo "    • ALPACA PROFIT TAKING - Alpaca bot closing positions"
echo "    • Dashboard will update every 30 seconds with real data"
