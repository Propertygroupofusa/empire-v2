# Video Auto-Editor Setup Guide

Automatically edit, upgrade, and publish your Synthesia AI videos to YouTube — completely hands-free.

## How It Works

```
Synthesia generates video
    ↓ (webhook)
Auto-Editor catches it
    ↓
Applies professional edits (filters, overlays, transitions)
    ↓
Exports in high quality (1080p/4K)
    ↓
Auto-publishes to YouTube
    ↓
Done! Video is live.
```

## Features

✅ **Auto-Detection** — Monitors Synthesia webhooks for new videos  
✅ **Smart Templates** — Different presets for trading, property, social, cold-call videos  
✅ **Quality Upgrade** — Applies professional filters (brightness, contrast, saturation)  
✅ **Branding** — Adds text overlays (your logo, watermark, titles)  
✅ **Transitions** — Fade in/out for professional look  
✅ **Auto-Publish** — Uploads to YouTube and sets privacy/metadata  
✅ **Job Tracking** — Monitor status of every edited video  

## Architecture

Three services working together:

```
Synthesia (generates) → Auto-Editor (edits) → YouTube (publishes)
                            ↑
                       Video Editor API (processes)
```

- **video_auto_editor.py** (port 5002) — Orchestration & Synthesia webhook receiver
- **video_editor_api.py** (port 5001) — Video processing with FFmpeg
- **video_revenue_api.py** (port 10000) — YouTube publishing

## Setup

### 1. Environment Variables

Add to your `.env` or Railway Variables:

```
# Auto-Editor Configuration
VIDEO_EDITOR_API_URL=http://localhost:5001
YOUTUBE_API_URL=http://localhost:10000
AUTO_EDITOR_PORT=5002

# Synthesia Webhook Secret (get from Synthesia dashboard)
SYNTHESIA_WEBHOOK_SECRET=your_webhook_secret_here
```

### 2. Configure Synthesia Webhook

1. Go to **Synthesia Dashboard → Settings → Webhooks**
2. Add webhook endpoint:
   - **URL**: `https://your-domain.com/api/auto-editor/synthesia-webhook`
   - **Events**: `video.completed`, `video.failed`
   - **Secret**: `SYNTHESIA_WEBHOOK_SECRET` (save this)

3. Test webhook connection
4. Copy the secret to your `.env`

### 3. Start Services

**Local Development:**

```bash
# Terminal 1: Video Editor API
python video_editor_api.py
# Runs on :5001

# Terminal 2: YouTube/Video Revenue API
python video_revenue_api.py
# Runs on :10000

# Terminal 3: Auto-Editor
python video_auto_editor.py
# Runs on :5002
```

**Docker:**

```bash
docker-compose up
```

### 4. Verify Setup

```bash
# Check all services
curl http://localhost:5002/api/auto-editor/health

# Should see:
# {
#   "status": "ok",
#   "editor_api": "ok",
#   "youtube_api": "ok",
#   "active_jobs": 0
# }
```

## Video Type Templates

The system automatically classifies videos and applies presets:

### Trading Update
```json
{
  "videoType": "trading-update",
  "profile": "PREMIUM (1080p)",
  "filters": [
    {"type": "brightness", "intensity": 0.55},
    {"type": "contrast", "intensity": 0.6},
    {"type": "saturation", "intensity": 0.7}
  ],
  "textOverlays": [
    {"text": "EMPIRE TRADING", "position": "top-center", "fontSize": 36},
    {"text": "Real Results. Real Money.", "position": "bottom-center", "fontSize": 20}
  ]
}
```

### Property Listing
```json
{
  "videoType": "property-listing",
  "profile": "CINEMATIC (4K)",
  "quality": "4K",
  "textOverlays": [
    {"text": "PROPERTY LISTING", "position": "top-center", "fontSize": 40}
  ]
}
```

### Social Content
```json
{
  "videoType": "social-content",
  "profile": "PREMIUM (1080p)",
  "filters": [brightness, contrast, saturation],
  "textOverlays": []
}
```

### Cold Call Follow-up
```json
{
  "videoType": "cold-call-followup",
  "profile": "STANDARD (1080p)",
  "textOverlays": [
    {"text": "Follow Up", "position": "bottom-right", "fontSize": 24}
  ]
}
```

## API Endpoints

### Receive Synthesia Webhook (Auto)
```http
POST /api/auto-editor/synthesia-webhook
```
**Auto-triggered by Synthesia** — you don't call this directly.

Event payload:
```json
{
  "eventType": "video.completed",
  "data": {
    "id": "video-123",
    "downloadUrl": "https://...",
    "title": "Trading Update",
    "description": "Today's trades"
  }
}
```

Response:
```json
{
  "status": "editing",
  "jobId": "job-456",
  "message": "Video submitted for editing and will auto-publish"
}
```

### Manually Submit Video for Auto-Edit
```http
POST /api/auto-editor/edit
Content-Type: application/json

{
  "videoUrl": "https://your-video.mp4",
  "videoType": "trading-update",
  "youtubeSettings": {
    "autoPublish": true,
    "privacy": "unlisted",
    "title": "Daily Trading Update",
    "description": "Check out today's trades",
    "tags": ["trading", "empire", "ai"]
  }
}
```

Response:
```json
{
  "status": "processing",
  "jobId": "job-789",
  "videoId": "manual_1234567890"
}
```

### Check Job Status
```http
GET /api/auto-editor/status/{jobId}
```

Response:
```json
{
  "jobId": "job-456",
  "status": "published",
  "progress": 100,
  "videoType": "trading-update",
  "downloadUrl": "http://localhost:5001/api/video/download/export_456.mp4",
  "youtubeUrl": "https://youtube.com/watch?v=...",
  "createdAt": "2026-07-10T12:34:56"
}
```

**Status values:**
- `editing` — Processing video
- `completed` — Editing done, ready to publish
- `published` — Live on YouTube
- `failed` — Error occurred
- `publish_failed` — Editing worked but YouTube upload failed

### List All Jobs
```http
GET /api/auto-editor/jobs
```

### Get Quality Profiles
```http
GET /api/auto-editor/profiles
```

Returns available STANDARD / PREMIUM / CINEMATIC profiles

### Get Edit Templates
```http
GET /api/auto-editor/templates
```

Returns all video type templates and their settings

## Workflows

### Workflow 1: Full Auto (Default)

1. Generate video in Synthesia → Choose type (Trading, Property, Social, Cold Call)
2. Synthesia webhook fires
3. Auto-Editor catches it
4. Professional edits applied automatically
5. Video exported in high quality
6. Auto-publishes to YouTube as `unlisted`
7. You're done! Check YouTube for the link

### Workflow 2: Manual Edit + Auto-Publish

```bash
# Step 1: You have a video file
curl -X POST http://localhost:5002/api/auto-editor/edit \
  -H "Content-Type: application/json" \
  -d '{
    "videoUrl": "https://my-server.com/video.mp4",
    "videoType": "trading-update",
    "youtubeSettings": {
      "autoPublish": true,
      "privacy": "public",
      "title": "Market Analysis"
    }
  }'

# Step 2: Check status
curl http://localhost:5002/api/auto-editor/status/job-789

# Step 3: Video is edited, exported, and published
```

### Workflow 3: Edit Only (No Publish)

```bash
curl -X POST http://localhost:5002/api/auto-editor/edit \
  -H "Content-Type: application/json" \
  -d '{
    "videoUrl": "https://...",
    "videoType": "social-content",
    "youtubeSettings": {
      "autoPublish": false
    }
  }'

# Returns downloadUrl in status — video is ready but not published
```

## Integration Examples

### Python Integration

```python
import requests
import json

# Auto-edit and publish a Synthesia video
response = requests.post(
    'http://localhost:5002/api/auto-editor/edit',
    json={
        'videoUrl': 'https://synthesia-videos.s3.amazonaws.com/video-123.mp4',
        'videoType': 'trading-update',
        'youtubeSettings': {
            'autoPublish': True,
            'privacy': 'unlisted',
            'title': 'Trading Analysis - AI Generated',
            'tags': ['ai', 'trading', 'empire']
        }
    }
)

job_data = response.json()
job_id = job_data['jobId']

# Check status
import time
time.sleep(60)  # Wait for processing

status = requests.get(f'http://localhost:5002/api/auto-editor/status/{job_id}').json()
print(f"Status: {status['status']}")
print(f"YouTube URL: {status.get('youtubeUrl')}")
```

### Synthesia API Integration

```python
# In your Synthesia video generation code:

# After generating video with Synthesia API, 
# the webhook will automatically trigger auto-editor

# Your code:
synthesia_response = generate_synthesia_video(
    avatar='olivia',
    text='Today we made $5000 on AAPL calls',
    # ... other params
)

# Synthesia generates video in background
# When done, webhook fires automatically
# Auto-editor catches it → edits → publishes
# You get YouTube URL via /api/auto-editor/status endpoint
```

## Customization

### Create Custom Template

Edit `EDIT_TEMPLATES` in `video_auto_editor.py`:

```python
EDIT_TEMPLATES = {
    'my-custom-video': {
        'profile': QualityProfile.PREMIUM,
        'textOverlays': [
            {
                'text': 'My Brand',
                'position': 'bottom-center',
                'fontSize': 28,
                'color': '#FFD700',
                'startTime': 0,
                'endTime': 5
            }
        ],
        'transitions': [
            {'type': 'fade', 'duration': 0.5, 'position': 'start'}
        ]
    }
}
```

Then use:
```bash
curl -X POST http://localhost:5002/api/auto-editor/edit \
  -H "Content-Type: application/json" \
  -d '{"videoUrl": "...", "videoType": "my-custom-video"}'
```

### Adjust Quality Profile

Modify `QualityProfile` enum to change filters:

```python
class QualityProfile(Enum):
    MY_PROFILE = {
        'quality': '1080p',
        'filters': [
            {'type': 'brightness', 'intensity': 0.7},
            {'type': 'contrast', 'intensity': 0.8},
            {'type': 'saturation', 'intensity': 0.9}
        ]
    }
```

## Monitoring

### View Active Jobs
```bash
curl http://localhost:5002/api/auto-editor/jobs | jq
```

### Monitor in Real-Time
```bash
watch -n 5 'curl -s http://localhost:5002/api/auto-editor/jobs | jq .active_jobs'
```

### Check Service Health
```bash
curl http://localhost:5002/api/auto-editor/health | jq
```

## Troubleshooting

### Webhook Not Firing
1. Check Synthesia webhook configuration is correct
2. Verify endpoint is accessible: `curl https://your-domain.com/api/auto-editor/synthesia-webhook`
3. Check logs: `tail -f video_auto_editor.log`

### Video Not Auto-Publishing
1. Verify YouTube API is running: `curl http://localhost:10000/health`
2. Check YOUTUBE_API_KEY is set
3. Check job status: `curl http://localhost:5002/api/auto-editor/status/{jobId}`

### Videos Stuck in "Editing" Status
1. Check video editor API: `curl http://localhost:5001/api/video/health`
2. Verify FFmpeg is installed and working
3. Check disk space for temp video files

### Wrong Template Applied
1. Video type auto-detection uses keywords (trading, property, cold, etc.)
2. Manual edit: explicitly set `videoType` in request
3. Custom template: edit `EDIT_TEMPLATES` mapping

## Performance Tips

- **Batch Processing** — Multiple videos process in parallel automatically
- **Cloud Storage** — Store videos on S3/GCS, not local disk
- **Memory** — Ensure 4GB+ RAM for concurrent processing
- **Network** — Stable connection for webhook delivery

---

🚀 **You're now fully automated!** Videos edit themselves and go live on YouTube.
