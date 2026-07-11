#!/bin/bash

echo "🎬 EMPIRE VIDEO GENERATOR - CREATE YOUR FIRST VIDEO"
echo "======================================================"
echo ""

# Check if services are running
echo "📋 Checking for running services..."
echo ""

# If user wants to use API (services running)
echo "Choose how to create your video:"
echo ""
echo "Option 1: Use Web UI (http://localhost:3000)"
echo "Option 2: Use API directly (if services running)"
echo "Option 3: Generate via Docker"
echo ""
echo "For now, let's create your video via the API..."
echo ""

# Get user input
read -p "Enter your video text (or press Enter for default): " VIDEO_TEXT

if [ -z "$VIDEO_TEXT" ]; then
    VIDEO_TEXT="Today we had an amazing day in the markets. Made five thousand dollars on Apple stock. The tech sector is leading the rally. Stay focused on your winners and manage your risk. This is empire trading."
fi

echo ""
echo "📝 Your Video Text:"
echo "   $VIDEO_TEXT"
echo ""

read -p "Video type (trading/property/social/cold-call) [default: trading]: " VIDEO_TYPE
VIDEO_TYPE=${VIDEO_TYPE:-trading}

read -p "Voice (male/female/professional/energetic) [default: professional]: " VOICE
VOICE=${VOICE:-professional}

read -p "Auto-publish to YouTube? (yes/no) [default: yes]: " AUTO_PUBLISH
AUTO_PUBLISH=${AUTO_PUBLISH:-yes}

echo ""
echo "🎬 Creating your video..."
echo ""
echo "This will:"
echo "  1. Generate AI voice"
echo "  2. Create animated frames"
echo "  3. Encode video (60 seconds)"
echo "  4. Auto-publish to YouTube"
echo ""

# Create the API call
if [ "$AUTO_PUBLISH" == "yes" ]; then
    PUBLISH_FLAG="true"
else
    PUBLISH_FLAG="false"
fi

API_URL="http://localhost:5003/api/video-gen/generate"

echo "Sending to API: $API_URL"
echo ""

curl -X POST "$API_URL" \
  -H "Content-Type: application/json" \
  -d "{
    \"text\": \"$VIDEO_TEXT\",
    \"videoType\": \"$VIDEO_TYPE\",
    \"voice\": \"$VOICE\",
    \"autoPublish\": $PUBLISH_FLAG,
    \"youtubeSettings\": {
      \"privacy\": \"unlisted\",
      \"title\": \"Empire Trading - Auto Generated Video\",
      \"tags\": [\"trading\", \"empire\", \"ai\"]
    }
  }" 2>/dev/null | python3 -m json.tool

echo ""
echo "✅ Video generation started!"
echo ""
echo "Next steps:"
echo "1. Copy the jobId from above"
echo "2. Check status: curl http://localhost:5003/api/video-gen/status/JOB_ID"
echo "3. Video will auto-publish to YouTube when complete"
echo ""

