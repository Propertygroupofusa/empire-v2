"""
FACEBOOK VIDEO AUTO-UPLOADER — YouTube Shorts Mirroring
==========================================================
Mirrors YouTube Shorts to Facebook automatically.
Extends reach 4x with same content, optimized for Facebook audience.

REQUIRED ENV VARS:
  FACEBOOK_ACCESS_TOKEN     - Facebook Graph API access token
  FACEBOOK_PAGE_ID          - Facebook page ID to post videos
  STATE_DIR                 - default /data/bot_state
  FACEBOOK_ENABLED          - kill switch, default true

OPTIONAL:
  FACEBOOK_POST_HOURS_UTC   - post times (default "14,18,23")
"""

import os
import json
import logging
import time
from datetime import datetime
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("facebook_bot")

# Configuration
FACEBOOK_TOKEN = os.getenv("FACEBOOK_ACCESS_TOKEN", "")
FACEBOOK_PAGE_ID = os.getenv("FACEBOOK_PAGE_ID", "")
ENABLED = os.getenv("FACEBOOK_ENABLED", "true").lower() == "true"
STATE_DIR = os.getenv("STATE_DIR", "/data/bot_state")
STATE_FILE = os.path.join(STATE_DIR, "facebook_bot_state.json")
POST_HOURS_UTC = [int(h) for h in os.getenv("FACEBOOK_POST_HOURS_UTC", "14,18,23").split(",")]

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

def upload_to_facebook(video_path, title, description, tags):
    """Upload video to Facebook page"""
    if not FACEBOOK_TOKEN or not FACEBOOK_PAGE_ID:
        log.warning("⚠️  FACEBOOK_ACCESS_TOKEN or FACEBOOK_PAGE_ID not set — skipping upload")
        return None

    try:
        # Facebook description from title + description + hashtags
        message = f"{title}\n\n{description}\n\n{' '.join(['#' + t for t in tags[:10]])}"

        with open(video_path, "rb") as f:
            files = {"video.mp4": f}
            data = {
                "description": message,
                "access_token": FACEBOOK_TOKEN,
            }

            url = f"https://graph.facebook.com/v18.0/{FACEBOOK_PAGE_ID}/videos"

            log.info(f"📤 Uploading to Facebook: {title}")
            r = requests.post(url, files=files, data=data, timeout=300)

            if r.status_code in (200, 201):
                result = r.json()
                video_id = result.get("id", "unknown")
                facebook_url = f"https://www.facebook.com/{FACEBOOK_PAGE_ID}/videos/{video_id}"
                log.info(f"✅ Facebook upload successful: {facebook_url}")
                return facebook_url
            else:
                log.error(f"❌ Facebook upload failed {r.status_code}: {r.text}")
                return None
    except Exception as e:
        log.error(f"❌ Facebook upload error: {e}")
        return None

def check_youtube_state():
    """Check if new YouTube videos are ready to mirror to Facebook"""
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
    log.info("FACEBOOK VIDEO AUTO-UPLOADER — YouTube Shorts Mirroring")
    log.info("=" * 60)

    if not ENABLED:
        log.info("FACEBOOK_ENABLED=false — uploader disabled")
        return

    state = load_state()

    # Check for new YouTube videos to mirror
    yt_videos = check_youtube_state()
    facebook_uploaded = state.get("uploaded", [])

    for video in yt_videos:
        if video.get("id") not in facebook_uploaded:
            log.info(f"🔄 Mirroring to Facebook: {video.get('title')}")
            facebook_url = upload_to_facebook(
                video_path=video.get("path", ""),
                title=video.get("title", ""),
                description=video.get("description", ""),
                tags=video.get("tags", [])
            )
            if facebook_url:
                facebook_uploaded.append(video.get("id"))
                state["uploaded"] = facebook_uploaded
                state["last_upload"] = datetime.utcnow().isoformat()
                save_state(state)

    log.info(f"Facebook sync complete — {len(facebook_uploaded)} videos mirrored")

if __name__ == "__main__":
    run()
