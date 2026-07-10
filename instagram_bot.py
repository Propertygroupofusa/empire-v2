"""
INSTAGRAM REELS AUTO-UPLOADER — YouTube Shorts Mirroring
===========================================================
Mirrors YouTube Shorts to Instagram Reels automatically.
Extends reach 3x with same content, optimized for Instagram audience.

REQUIRED ENV VARS:
  INSTAGRAM_ACCESS_TOKEN    - Instagram Graph API access token
  INSTAGRAM_BUSINESS_ACCOUNT_ID - Instagram business account ID
  STATE_DIR                 - default /data/bot_state
  INSTAGRAM_ENABLED         - kill switch, default true

OPTIONAL:
  INSTAGRAM_POST_HOURS_UTC  - post times (default "14,18,23")
"""

import os
import json
import logging
import time
from datetime import datetime
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("instagram_bot")

# Configuration
INSTAGRAM_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
INSTAGRAM_ACCOUNT_ID = os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "")
ENABLED = os.getenv("INSTAGRAM_ENABLED", "true").lower() == "true"
STATE_DIR = os.getenv("STATE_DIR", "/data/bot_state")
STATE_FILE = os.path.join(STATE_DIR, "instagram_bot_state.json")
POST_HOURS_UTC = [int(h) for h in os.getenv("INSTAGRAM_POST_HOURS_UTC", "14,18,23").split(",")]

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

def upload_to_instagram(video_path, title, description, tags):
    """Upload video to Instagram Reels"""
    if not INSTAGRAM_TOKEN or not INSTAGRAM_ACCOUNT_ID:
        log.warning("⚠️  INSTAGRAM_ACCESS_TOKEN or INSTAGRAM_BUSINESS_ACCOUNT_ID not set — skipping upload")
        return None

    try:
        # Instagram caption from title + description + hashtags
        caption = f"{title}\n\n{description}\n\n{' '.join(['#' + t for t in tags[:10]])}"

        with open(video_path, "rb") as f:
            files = {"video": f}
            data = {
                "caption": caption,
                "media_type": "REELS",
            }
            headers = {"Authorization": f"Bearer {INSTAGRAM_TOKEN}"}

            url = f"https://graph.instagram.com/v18.0/{INSTAGRAM_ACCOUNT_ID}/media"

            log.info(f"📤 Uploading to Instagram Reels: {title}")
            r = requests.post(url, files=files, data=data, headers=headers, timeout=300)

            if r.status_code in (200, 201):
                result = r.json()
                media_id = result.get("id", "unknown")
                log.info(f"✅ Instagram Reels upload successful: {media_id}")
                return f"https://instagram.com/reel/{media_id}"
            else:
                log.error(f"❌ Instagram upload failed {r.status_code}: {r.text}")
                return None
    except Exception as e:
        log.error(f"❌ Instagram upload error: {e}")
        return None

def check_youtube_state():
    """Check if new YouTube videos are ready to mirror to Instagram"""
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
    log.info("INSTAGRAM REELS AUTO-UPLOADER — YouTube Shorts Mirroring")
    log.info("=" * 60)

    if not ENABLED:
        log.info("INSTAGRAM_ENABLED=false — uploader disabled")
        return

    state = load_state()

    # Check for new YouTube videos to mirror
    yt_videos = check_youtube_state()
    instagram_uploaded = state.get("uploaded", [])

    for video in yt_videos:
        if video.get("id") not in instagram_uploaded:
            log.info(f"🔄 Mirroring to Instagram Reels: {video.get('title')}")
            instagram_url = upload_to_instagram(
                video_path=video.get("path", ""),
                title=video.get("title", ""),
                description=video.get("description", ""),
                tags=video.get("tags", [])
            )
            if instagram_url:
                instagram_uploaded.append(video.get("id"))
                state["uploaded"] = instagram_uploaded
                state["last_upload"] = datetime.utcnow().isoformat()
                save_state(state)

    log.info(f"Instagram Reels sync complete — {len(instagram_uploaded)} videos mirrored")

if __name__ == "__main__":
    run()
