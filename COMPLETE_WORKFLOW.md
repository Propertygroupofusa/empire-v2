# Complete Video Workflow: Edit → Upgrade → Publish

End-to-end video automation for Empire v2. From Synthesia AI generation to YouTube publication.

## The Three Services

```
┌─────────────────────────────────────────────────────────────┐
│                    COMPLETE WORKFLOW                         │
└─────────────────────────────────────────────────────────────┘

1. VIDEO EDITOR (Port 3000 - Web UI)
   └─ Manual editing for full control
   └─ Upload → Trim → Add filters/overlays/transitions → Export

2. VIDEO EDITOR API (Port 5001 - Backend)
   └─ FFmpeg video processing
   └─ Handles trim, filters, overlays, transitions
   └─ Exports in MP4/WebM/MOV at 720p/1080p/4K

3. AUTO-EDITOR (Port 5002 - Webhook Handler)
   └─ Listens for Synthesia video completion
   └─ Auto-applies professional edits
   └─ Auto-publishes to YouTube
   └─ Completely hands-free

4. YOUTUBE PUBLISHER (Port 10000 - Existing)
   └─ Handles YouTube upload
   └─ Sets metadata, privacy, descriptions
```

## Three Ways to Use It

### Option 1: Fully Automatic (Recommended for most)

```
You → Synthesia (generate AI video)
        ↓
    Auto-Editor catches webhook
        ↓
    Applies professional edits
        ↓
    Exports in high quality
        ↓
    Publishes to YouTube
        ↓
    Done! Video is live.
```

**Setup:**
1. Configure Synthesia webhook to your domain
2. Set YouTube API key
3. Done. All videos auto-edit and publish.

**No manual work required.**

---

### Option 2: Manual Edit + Auto-Publish

```
You → Upload video to editor (http://localhost:3000)
        ↓
    Choose filters, overlays, transitions
        ↓
    Click "Export & Download"
        ↓
    Call /api/auto-editor/edit endpoint
        ↓
    Auto-publishes to YouTube
        ↓
    Done! Video is live.
```

**Setup:**
1. Open web editor at `http://localhost:3000`
2. Edit video manually
3. Export
4. Call API to publish

**Takes 2-3 minutes of manual work per video.**

---

### Option 3: Pure Web UI (For non-technical users)

```
You → Open http://localhost:3000
        ↓
    Upload video
        ↓
    Use simple UI to edit
        ↓
    Click "Export & Download"
        ↓
    Video ready for download
```

**Setup:**
1. That's it. Just use the web interface.

**No API calls needed.**

---

## Complete Setup Guide

### Prerequisites

```bash
# Check requirements
ffmpeg -version          # Should be installed
python3 --version        # 3.9+
node --version          # 18+
```

### Local Development (5 minutes)

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Install React dependencies
cd video-editor
npm install
cd ..

# 3. Create directories
mkdir -p video_uploads video_temp video_exports

# 4. Start all services
# Terminal 1: Video processing API
python video_editor_api.py
# Runs on http://localhost:5001

# Terminal 2: Auto-editor (webhook handler)
python video_auto_editor.py
# Runs on http://localhost:5002

# Terminal 3: React web UI
cd video-editor
npm start
# Runs on http://localhost:3000

# Terminal 4 (if needed): YouTube/revenue API
python video_revenue_api.py
# Runs on http://localhost:10000
```

**OR use Docker:**

```bash
docker-compose up
# All services start automatically
```

### Production (Railway)

```bash
# 1. Push to main branch
git add .
git commit -m "Add video auto-editor"
git push origin main

# 2. Railway auto-deploys

# 3. Configure environment variables on Railway:
VIDEO_EDITOR_API_URL=https://your-app.railway.app/editor
YOUTUBE_API_URL=https://your-app.railway.app/api
SYNTHESIA_WEBHOOK_SECRET=your_secret_here

# 4. Set Synthesia webhook:
# https://your-app.railway.app/api/auto-editor/synthesia-webhook

# 5. Done! Your automation is live.
```

---

## Usage Examples

### Example 1: Synthesia → YouTube (100% Automatic)

**Your steps:**
1. Go to Synthesia.io
2. Generate video with text
3. Click "Generate"
4. Wait for email notification

**System does:**
```
(Automatic - you don't touch anything)
Synthesia finishes video
    ↓ [webhook]
Auto-Editor catches it
    ↓ [identifies as "social-content"]
Applies professional upgrades:
  - Brightness +0.55
  - Contrast +0.6
  - Saturation +0.7
    ↓
Exports as 1080p MP4
    ↓
Publishes to YouTube (unlisted)
    ↓
✅ Video is live!
```

**Your results:**
- Professional-quality video on YouTube
- Zero manual work
- Takes ~5 minutes total

---

### Example 2: Manual Edit → YouTube (5 minutes work)

**Step 1: Edit**
```bash
# Open http://localhost:3000
```

```
→ Drag & drop your video
→ Trim to 0:05 - 0:45
→ Add "TRADING UPDATE" text overlay (top-center, gold color)
→ Add brightness filter (0.6)
→ Add fade-in transition
→ Click "Export & Download" (1080p)
→ Download finishes
```

**Step 2: Publish**
```bash
curl -X POST http://localhost:5002/api/auto-editor/edit \
  -H "Content-Type: application/json" \
  -d '{
    "videoUrl": "https://your-storage.com/edited-video.mp4",
    "videoType": "trading-update",
    "youtubeSettings": {
      "autoPublish": true,
      "privacy": "unlisted",
      "title": "Today Trading Update",
      "tags": ["trading", "empire", "ai"]
    }
  }'
```

**Results:**
- Professional-edited video
- On YouTube within 30 seconds
- You did manual editing (control quality)

---

### Example 3: Property Listing (Cinema Quality)

**Synthesia generates:** Professional property tour video  
**Auto-Editor applies:** CINEMATIC profile (4K, high contrast, saturation boost)  
**Result:** YouTube-ready property listing video  

**Command to trigger:**
```bash
curl -X POST http://localhost:5002/api/auto-editor/edit \
  -H "Content-Type: application/json" \
  -d '{
    "videoUrl": "https://synthesia-videos.s3.amazonaws.com/property-tour.mp4",
    "videoType": "property-listing",
    "youtubeSettings": {
      "autoPublish": true,
      "privacy": "public",
      "title": "Beautiful Home at 123 Main St",
      "tags": ["realestate", "property", "listing"]
    }
  }'
```

---

## API Quick Reference

### Auto-Editor Endpoints (Port 5002)

**Auto-triggered by Synthesia:**
```
POST /api/auto-editor/synthesia-webhook
```

**Manually submit for editing:**
```
POST /api/auto-editor/edit
{
  "videoUrl": "https://...",
  "videoType": "trading-update|property-listing|social-content|cold-call-followup",
  "youtubeSettings": {
    "autoPublish": true|false,
    "privacy": "public|unlisted|private",
    "title": "Your title",
    "tags": ["tag1", "tag2"]
  }
}
```

**Check job status:**
```
GET /api/auto-editor/status/{jobId}
```

**List all jobs:**
```
GET /api/auto-editor/jobs
```

**Get available profiles:**
```
GET /api/auto-editor/profiles
```

**Get templates:**
```
GET /api/auto-editor/templates
```

**Health check:**
```
GET /api/auto-editor/health
```

---

### Video Editor API Endpoints (Port 5001)

**Export video:**
```
POST /api/video/export
  video: <file>
  edits: {"trim": {...}, "filters": [...], ...}
  format: "mp4|webm|mov"
  quality: "720p|1080p|4k"
```

**Check export status:**
```
GET /api/video/export/{jobId}
```

**Download:**
```
GET /api/video/download/{filename}
```

---

## Job Status States

```
editing         → Processing video
completed       → Ready for YouTube
published       → Live on YouTube ✅
failed          → Error occurred
publish_failed  → Editing worked, but YouTube upload failed
timeout         → Processing took too long
```

---

## Performance & Costs

### Processing Time
- Small video (< 5 min): 2-5 minutes
- Medium video (5-15 min): 5-15 minutes
- Large video (> 15 min): 15-30 minutes

### Quality vs Speed
- 720p: Fastest, smallest file
- 1080p: Balanced (recommended)
- 4K: Slowest, largest file

### Disk Space
- Temp storage for processing: ~5GB
- Exports directory: ~2GB per 10 videos
- Clean up exports periodically

---

## Monitoring & Alerts

### Check All Services Are Running

```bash
# Video Editor API
curl http://localhost:5001/api/video/health

# Auto-Editor
curl http://localhost:5002/api/auto-editor/health

# YouTube API
curl http://localhost:10000/health
```

### Monitor Active Jobs

```bash
# Real-time job status
watch -n 5 'curl -s http://localhost:5002/api/auto-editor/jobs | jq .total'
```

### Check Logs

```bash
# Auto-editor logs
tail -f logs/video_auto_editor.log

# Video editor logs
tail -f logs/video_editor_api.log
```

---

## Troubleshooting

### "Video not auto-publishing"
1. Check YouTube API is running: `curl http://localhost:10000/health`
2. Verify YOUTUBE_API_KEY is set
3. Check job status: `curl http://localhost:5002/api/auto-editor/status/{jobId}`

### "Webhook not firing"
1. Verify Synthesia webhook configuration
2. Test endpoint is accessible from internet
3. Check SECRET key matches

### "Videos stuck in editing"
1. Check video editor API: `curl http://localhost:5001/api/video/health`
2. Verify FFmpeg installed: `ffmpeg -version`
3. Check disk space: `df -h`

---

## Next Steps

1. **Try fully automatic** — Create Synthesia video, watch it auto-publish
2. **Try manual editing** — Use web UI to customize videos
3. **Create templates** — Add custom edit profiles for your brand
4. **Monitor jobs** — Track video quality and publishing metrics
5. **Integrate** — Add to your existing automation workflows

---

## Support

- Video Editor Setup: See `VIDEO_EDITOR_SETUP.md`
- Auto-Editor Setup: See `AUTO_EDITOR_SETUP.md`
- API Reference: Check individual service docs

---

**You now have a complete, production-grade video automation system.** 🚀

From AI generation to YouTube publication — all hands-free.
