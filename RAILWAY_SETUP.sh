#!/bin/bash
# Railway Deployment Setup Script
# Automatically configures your Railway project with all necessary environment variables

set -e

echo "🚀 Empire Video + Revenue Automation - Railway Deployment Setup"
echo "================================================================"
echo ""

# Check if railway CLI is installed
if ! command -v railway &> /dev/null; then
    echo "❌ Railway CLI not found. Install it:"
    echo "   npm install -g @railway/cli"
    exit 1
fi

echo "✅ Railway CLI found"
echo ""

# Prompt for environment variables
echo "📋 Enter your configuration (leave blank to skip):"
echo ""

read -p "YouTube API Key: " YOUTUBE_API_KEY
read -p "YouTube Client ID: " YOUTUBE_CLIENT_ID
read -p "YouTube Client Secret: " YOUTUBE_CLIENT_SECRET
read -p "YouTube Refresh Token: " YOUTUBE_REFRESH_TOKEN
read -p "Stripe Public Key (pk_...): " STRIPE_PUBLIC_KEY
read -p "Stripe Secret Key (sk_...): " STRIPE_SECRET_KEY
read -p "Anthropic API Key: " ANTHROPIC_API_KEY
read -p "Alpaca API Key: " ALPACA_API_KEY
read -p "Alpaca Secret Key: " ALPACA_SECRET_KEY
read -p "OpenAI API Key (optional): " OPENAI_API_KEY
read -p "Grok API Key (optional): " GROK_API_KEY

echo ""
echo "⚙️  Configuring Railway environment variables..."
echo ""

# Set variables via Railway CLI
railway variables set YOUTUBE_API_KEY="$YOUTUBE_API_KEY"
railway variables set YOUTUBE_CLIENT_ID="$YOUTUBE_CLIENT_ID"
railway variables set YOUTUBE_CLIENT_SECRET="$YOUTUBE_CLIENT_SECRET"
railway variables set YOUTUBE_REFRESH_TOKEN="$YOUTUBE_REFRESH_TOKEN"
railway variables set YOUTUBE_AUTO_PUBLISH=true
railway variables set STRIPE_PUBLIC_KEY="$STRIPE_PUBLIC_KEY"
railway variables set STRIPE_SECRET_KEY="$STRIPE_SECRET_KEY"
railway variables set ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"
railway variables set ALPACA_API_KEY="$ALPACA_API_KEY"
railway variables set ALPACA_SECRET_KEY="$ALPACA_SECRET_KEY"

if [ ! -z "$OPENAI_API_KEY" ]; then
    railway variables set OPENAI_API_KEY="$OPENAI_API_KEY"
fi

if [ ! -z "$GROK_API_KEY" ]; then
    railway variables set GROK_API_KEY="$GROK_API_KEY"
fi

# Set revenue automation variables
railway variables set DAILY_PUBLISHER_ENABLED=true
railway variables set VIDEO_GENERATOR_ENABLED=true
railway variables set VIDEO_AUTO_PUBLISH=true
railway variables set VIDEO_GENERATOR_URL=http://localhost:5003
railway variables set PORT=10000

echo "✅ Environment variables configured!"
echo ""
echo "📊 Deployment Status:"
echo "   - Check Railway dashboard for deployment progress"
echo "   - Services should be live in 2-3 minutes"
echo ""
echo "🎬 Next Steps:"
echo "   1. Wait for deployment to complete (green checkmarks in Railway UI)"
echo "   2. Get your app URL from Railway"
echo "   3. Start earning:"
echo ""
echo "   curl -X POST https://YOUR-APP.railway.app/revenue/publishing/start"
echo ""
echo "   4. Check your dashboard:"
echo ""
echo "   curl https://YOUR-APP.railway.app/revenue/dashboard/executive-summary"
echo ""
echo "💰 Your revenue streams are now live!"
