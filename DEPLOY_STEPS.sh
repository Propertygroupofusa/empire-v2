#!/bin/bash
# Quick deployment script for Empire v2 to GitHub + Railway

set -e

echo "🚀 Empire v2 Deployment Script"
echo "================================"
echo ""

# Step 1: Verify git status
echo "📋 Step 1: Checking git status..."
if [ -n "$(git status --porcelain)" ]; then
    echo "❌ Uncommitted changes detected!"
    echo "Run: git add . && git commit -m 'Your message'"
    exit 1
fi
echo "✅ Working tree clean"
echo ""

# Step 2: Verify requirements
echo "📋 Step 2: Verifying Python dependencies..."
python3 -m py_compile main.py routers/orders.py 2>/dev/null || {
    echo "❌ Python syntax errors found"
    exit 1
}
echo "✅ Python syntax valid"
echo ""

# Step 3: Verify Docker build
echo "📋 Step 3: Building Docker image (this may take 2-3 minutes)..."
if docker build -t empire-v2:latest . --quiet; then
    echo "✅ Docker build successful"
else
    echo "❌ Docker build failed"
    exit 1
fi
echo ""

# Step 4: Display current branch
BRANCH=$(git rev-parse --abbrev-ref HEAD)
echo "📋 Step 4: Current branch: $BRANCH"
echo ""

# Step 5: Push to GitHub
echo "📋 Step 5: Pushing to GitHub..."
git push -u origin "$BRANCH"
echo "✅ Pushed to GitHub"
echo ""

# Step 6: Display deployment URL
echo "✅ Deployment Initiated!"
echo ""
echo "📍 Next Steps:"
echo "1. Go to GitHub: https://github.com/Propertygroupofusa/empire-v2/tree/$BRANCH"
echo "2. Create Pull Request (if not main branch)"
echo "3. Watch deployment on Railway dashboard:"
echo "   https://railway.app/project/YOUR_PROJECT_ID"
echo ""
echo "📖 Full guide: Read DEPLOYMENT_GUIDE.md"
echo ""
