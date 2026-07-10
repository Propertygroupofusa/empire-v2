# Empire Video Editor Platform Setup

A complete video editing platform for Empire v2. Create, edit, and export professional videos — then publish directly to YouTube.

## Features

✅ **Upload** — Drag & drop or click to select video files  
✅ **Trim** — Cut videos to exact start/end times  
✅ **Filters** — Apply brightness, contrast, saturation, grayscale, blur  
✅ **Text Overlays** — Add custom text with position, size, color, timing  
✅ **Transitions** — Fade in/out effects  
✅ **Export** — MP4, WebM, MOV formats at 720p/1080p/4K quality  
✅ **Integration** — Works with Synthesia AI videos & YouTube auto-publish  

---

## System Requirements

- **FFmpeg** (video processing engine)
- **Node.js 18+** (React frontend)
- **Python 3.9+** (Backend API)
- **4GB RAM** minimum (8GB recommended)
- **50GB free disk** for processing large videos

### Install FFmpeg

**macOS:**
```bash
brew install ffmpeg
```

**Ubuntu/Debian:**
```bash
sudo apt-get install ffmpeg
```

**Windows:**
```bash
choco install ffmpeg
```

---

## Quick Start (Development)

### 1. Backend Setup

```bash
# Install Python dependencies
pip install -r requirements.txt

# Start video editor API (runs on :5001)
python video_editor_api.py
```

The API will start on `http://localhost:5001`

### 2. Frontend Setup

```bash
cd video-editor

# Install React dependencies
npm install

# Start dev server (runs on :3000)
npm start
```

The editor opens at `http://localhost:3000`

### 3. Test It

1. Go to `http://localhost:3000`
2. Upload a video (MP4, WebM, MOV up to 500MB)
3. Trim, add filters, text overlays, transitions
4. Click "Export & Download"
5. Video processes and downloads automatically

---

## Production Deployment (Railway)

### Add to Railway

1. **Ensure `railway.json` includes both services:**

```json
{
  "services": {
    "python": {
      "startCommand": "python main.py"
    },
    "video-editor-api": {
      "root": ".",
      "startCommand": "python video_editor_api.py"
    },
    "video-editor-frontend": {
      "root": "video-editor",
      "startCommand": "npm install && npm build && npx serve -s build -l 3000"
    }
  }
}
```

2. **Railway Environment Variables** (add to Variables tab):

```
# Existing variables (keep these)
ALPACA_API_KEY=your_key
ANTHROPIC_API_KEY=your_key
YOUTUBE_API_KEY=your_key
...

# New variables
VIDEO_EDITOR_PORT=5001
FRONTEND_PORT=3000
MAX_VIDEO_SIZE=500000000
TEMP_STORAGE=/tmp/video-editor
```

3. **Install FFmpeg on Railway:**

Create a `Procfile` if using dyno-based deployment, or add to start script:
```bash
apt-get update && apt-get install -y ffmpeg
python video_editor_api.py
```

### Deploy

```bash
git add .
git commit -m "Add video editing platform"
git push origin claude/video-editing-platform-ib585z
```

Railway auto-deploys. Check logs for:
```
Video Editor API running on 0.0.0.0:5001
React app serving on :3000
```

---

## API Endpoints

### Export Video
```http
POST /api/video/export
Content-Type: multipart/form-data

video: <file>
edits: {
  "trim": { "start": 5, "end": 60 },
  "filters": [
    { "type": "brightness", "intensity": 0.7 },
    { "type": "contrast", "intensity": 0.8 }
  ],
  "textOverlays": [
    {
      "text": "Empire Trading",
      "startTime": 0,
      "endTime": 5,
      "position": "center",
      "fontSize": 32,
      "color": "#ffffff"
    }
  ],
  "transitions": [
    { "type": "fade", "duration": 0.5, "position": "start" }
  ]
}
format: mp4
quality: 1080p
```

**Response:**
```json
{
  "jobId": "abc123",
  "status": "processing",
  "progress": 0
}
```

### Check Export Status
```http
GET /api/video/export/{jobId}
```

**Response:**
```json
{
  "status": "completed",
  "progress": 100,
  "downloadUrl": "/api/video/download/export_abc123.mp4"
}
```

### Download Video
```http
GET /api/video/download/{filename}
```

### Health Check
```http
GET /api/video/health
```

---

## Integration with Empire v2

### Publish to YouTube After Export

After exporting a video, integrate with your existing `video_revenue_api.py`:

```python
import requests

# After getting download_url from video editor
response = requests.post('http://localhost:10000/publish/youtube/social-content', json={
    'video_url': 'http://localhost:5001/api/video/download/export_abc123.mp4',
    'title': 'Trading Update - Empire v2',
    'description': 'Auto-generated trading video',
    'privacy': 'unlisted'  # or 'public'
})
```

### Batch Edit + Publish Workflow

```python
# 1. Generate Synthesia video
synthesia_video = generate_synthesia_video(
    avatar_id='e49ecfaf-1d39-4561-8355-29ebf8b71a4f',
    text='Check out our latest trade...'
)

# 2. Wait for Synthesia webhook
# (video_revenue_api.py handles this)

# 3. Auto-send to editor for processing
edit_request = {
    'video': synthesia_video.url,
    'edits': {
        'filters': [{'type': 'brightness', 'intensity': 0.5}],
        'textOverlays': [
            {
                'text': 'Empire Trading - Live Results',
                'position': 'bottom-center',
                'startTime': 0,
                'endTime': 5
            }
        ]
    },
    'format': 'mp4',
    'quality': '1080p'
}

# 4. Export
job = requests.post('http://localhost:5001/api/video/export', 
                   files={'video': synthesia_video.file},
                   data={'edits': json.dumps(edit_request['edits'])})

# 5. Publish to YouTube
requests.post('http://localhost:10000/publish/youtube/social-content',
             json={'video_url': download_url})
```

---

## File Structure

```
empire-v2/
├── video-editor/              # React frontend
│   ├── src/
│   │   ├── App.jsx           # Main component
│   │   ├── components/
│   │   │   ├── VideoUploader.jsx
│   │   │   ├── VideoEditor.jsx
│   │   │   └── ExportPanel.jsx
│   │   └── index.js
│   ├── public/
│   │   └── index.html
│   └── package.json
├── video_editor_api.py        # Flask backend
├── video_uploads/             # Uploaded videos
├── video_temp/               # Processing temp files
├── video_exports/            # Exported videos
└── VIDEO_EDITOR_SETUP.md     # This file
```

---

## Performance Tips

### Large Videos (100MB+)
- Use **720p export** for faster processing
- **Trim first** before applying filters
- Process during **off-peak hours** on Railway

### Multiple Concurrent Exports
- API handles queue automatically
- Each job gets unique temp folder
- Cleanup runs every hour (auto-removes old temps)

### Memory Usage
- FFmpeg uses ~2GB per video processing
- Set Railway RAM to 4GB+ if processing 4K
- Temp files auto-delete after 1 hour

---

## Troubleshooting

### "FFmpeg not found"
```bash
# Verify FFmpeg is installed
ffmpeg -version

# On Railway, ensure Procfile includes:
apt-get install -y ffmpeg
```

### Export fails with "Invalid filter"
- Check filter intensity is 0-1
- Ensure text overlay times are within video duration
- Verify color codes are hex (#RRGGBB)

### Video upload too slow
- Check file size (max 500MB)
- Use MP4 format for fastest upload
- Consider compressing before upload

### Frontend won't connect to API
```javascript
// Check src/App.jsx axios URL
// Change '/api/video/export' to:
// Development: 'http://localhost:5001/api/video/export'
// Production: 'https://your-railway-url/api/video/export'
```

---

## Monitoring

### Check API Health
```bash
curl http://localhost:5001/api/video/health
```

Response shows:
- FFmpeg availability
- Service status
- Temp file count

### View Processing Jobs
Add endpoint to track active jobs:
```python
@app.route('/api/video/jobs', methods=['GET'])
def list_jobs():
    return jsonify(jobs)
```

---

## Next Steps

1. **Test with Synthesia videos** — Upload AI-generated videos for editing
2. **Create presets** — Save common filter/overlay combinations
3. **Add templates** — Pre-built social media templates (TikTok, Instagram, YouTube Shorts)
4. **Batch processing** — Edit multiple videos automatically
5. **Advanced effects** — Color grading, custom transitions, animation

---

Built with ❤️ for Empire v2 trading system.
