"""
TIKTOK AUTO-UPLOADER — Multi-source video distribution
========================================================
Mirrors YouTube Shorts to TikTok automatically.
Extends reach 2x with same content, optimized for TikTok audience.

REQUIRED ENV VARS:
  TIKTOK_ACCESS_TOKEN    - TikTok API access token
  TIKTOK_VIDEO_UPLOAD_URL - TikTok upload endpoint
  STATE_DIR              - default /data/bot_state
  TIKTOK_ENABLED         - kill switch, default true

OPTIONAL:
  TIKTOK_POST_HOURS_UTC  - post times (default "14,18,23" same as YouTube)
"""

import os
import json
import logging
import time
from datetime import datetime
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tiktok_bot")

# Configuration
TIKTOK_TOKEN = os.getenv("TIKTOK_ACCESS_TOKEN", "")
TIKTOK_UPLOAD_URL = os.getenv("TIKTOK_VIDEO_UPLOAD_URL", "https://open.tiktokapis.com/v1/video/upload/")
ENABLED = os.getenv("TIKTOK_ENABLED", "true").lower() == "true"
STATE_DIR = os.getenv("STATE_DIR", "/data/bot_state")
STATE_FILE = os.path.join(STATE_DIR, "tiktok_bot_state.json")
POST_HOURS_UTC = [int(h) for h in os.getenv("TIKTOK_POST_HOURS_UTC", "14,18,23").split(",")]

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"uploaded": [], "last_upload": ""}

def save_state(s):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)

def upload_to_tiktok(video_path, title, description, tags):
    """Upload video to TikTok"""
    if not TIKTOK_TOKEN:
        log.warning("⚠️  TIKTOK_ACCESS_TOKEN not set — skipping upload")
        return None

    try:
        with open(video_path, "rb") as f:
            files = {"video": f}
            data = {
                "description": f"{description}\n\n{' '.join(['#' + t for t in tags[:10]])}",
                "title": title,
            }
            headers = {"Authorization": f"Bearer {TIKTOK_TOKEN}"}

            log.info(f"📤 Uploading to TikTok: {title}")
            r = requests.post(TIKTOK_UPLOAD_URL, files=files, data=data, headers=headers, timeout=300)

            if r.status_code in (200, 201):
                result = r.json()
                video_id = result.get("data", {}).get("video_id", "unknown")
                tiktok_url = f"https://www.tiktok.com/@your_account/video/{video_id}"
                log.info(f"✅ TikTok upload successful: {tiktok_url}")
                return tiktok_url
            else:
                log.error(f"❌ TikTok upload failed {r.status_code}: {r.text}")
                return None
    except Exception as e:
        log.error(f"❌ TikTok upload error: {e}")
        return None

def check_youtube_state():
    """Check if new YouTube videos are ready to mirror to TikTok"""
    youtube_state_file = os.path.join(STATE_DIR, "content_bot_state.json")
    try:
        with open(youtube_state_file) as f:
            yt_state = json.load(f)
            published = yt_state.get("published", [])
            return published
    except Exception:
        return []

def run():
    log.info("=" * 60)
    log.info("TIKTOK AUTO-UPLOADER — YouTube Shorts Mirroring")
    log.info("=" * 60)

    if not ENABLED:
        log.info("TIKTOK_ENABLED=false — uploader disabled")
        return

    state = load_state()

    # Check for new YouTube videos to mirror
    yt_videos = check_youtube_state()
    tiktok_uploaded = state.get("uploaded", [])

    for video in yt_videos:
        if video.get("id") not in tiktok_uploaded:
            log.info(f"🔄 Mirroring to TikTok: {video.get('title')}")
            # Video path would be from content_bot's temp output
            # This is a placeholder — in production, content_bot passes the video file
            tiktok_url = upload_to_tiktok(
                video_path=video.get("path", ""),
                title=video.get("title", ""),
                description=video.get("description", ""),
                tags=video.get("tags", [])
            )
            if tiktok_url:
                tiktok_uploaded.append(video.get("id"))
                state["uploaded"] = tiktok_uploaded
                state["last_upload"] = datetime.utcnow().isoformat()
                save_state(state)

    log.info(f"TikTok sync complete — {len(tiktok_uploaded)} videos mirrored")

if __name__ == "__main__":
    run()
