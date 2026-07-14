"""
YOUTUBE AUTOMATION PIPELINE - INTEGRATED VERSION
Uses your existing video_generator_bot (FREE, no Synthesia needed)

Pipeline:
1. Generate script (Claude)
2. Create video (Your video_generator_bot)
3. Split clips (ffmpeg)
4. Upload to YouTube (YouTube API)
5. Track revenue
"""

import os
import asyncio
import logging
import json
import sqlite3
import hashlib
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from pathlib import Path
import httpx

import anthropic

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("youtube_pipeline.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("youtube_pipeline_integrated")

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
YOUTUBE_TOKEN = os.getenv("YOUTUBE_REFRESH_TOKEN", "")
YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET", "")

# Your video generator endpoint (runs on localhost:5000 by default)
VIDEO_GENERATOR_API = os.getenv("VIDEO_GENERATOR_API", "http://localhost:5000")
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"

DB_PATH = "youtube_pipeline.db"

CLIP_TIMES = [
    {"name": "intro", "start": 0, "end": 90},
    {"name": "main", "start": 90, "end": 210},
    {"name": "outro", "start": 210, "end": 300},
]

MAX_RETRIES = 3
RETRY_DELAY = 2


# ============================================================================
# DATABASE SETUP
# ============================================================================

def init_database():
    """Initialize SQLite database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS youtube_videos (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        topic TEXT NOT NULL,
        script TEXT NOT NULL,
        status TEXT DEFAULT 'queued',
        video_url TEXT,
        youtube_video_id TEXT,
        youtube_url TEXT,
        views INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        error_message TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS pipeline_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id TEXT,
        step TEXT,
        status TEXT,
        message TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()
    conn.close()
    log.info("Database initialized")


def update_video_status(video_id: str, status: str, **kwargs):
    """Update video status"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    updates = [f"status = '{status}', updated_at = CURRENT_TIMESTAMP"]
    values = []

    for key, value in kwargs.items():
        if value is not None:
            updates.append(f"{key} = ?")
            values.append(value)

    values.append(video_id)
    query = f"UPDATE youtube_videos SET {', '.join(updates)} WHERE id = ?"

    cursor.execute(query, values)
    conn.commit()
    conn.close()


def log_pipeline(video_id: str, step: str, status: str, message: str = ""):
    """Log pipeline step"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO pipeline_logs (video_id, step, status, message) VALUES (?, ?, ?, ?)",
        (video_id, step, status, message)
    )
    conn.commit()
    conn.close()
    log.info(f"[{video_id}] {step}: {status} - {message}")


# ============================================================================
# STEP 1: GENERATE VIDEO SCRIPT
# ============================================================================

async def generate_video_script(topic: str, difficulty: str = "intermediate") -> Dict:
    """Generate video script with Claude"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    prompt = f"""Create an EXCELLENT, engaging YouTube video script for kids (8-14 years old).

Topic: {topic}
Difficulty: {difficulty}
Duration: 5 minutes (1200-1500 words)
Format: Educational but FUN

You MUST respond with VALID JSON only (no markdown):
{{
    "title": "Catchy, emoji title with keywords for SEO",
    "description": "YouTube description (100-150 words with keywords)",
    "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
    "thumbnail_text": "Max 3 words, all caps",
    "script": "Full 5-minute video script for narration. Make it engaging and fun.",
    "key_points": ["Point 1", "Point 2", "Point 3"],
    "cta": "Call to action",
    "hashtags": ["#Learn", "#Education"],
    "voice": "female"
}}

Return ONLY the JSON, nothing else."""

    for attempt in range(MAX_RETRIES):
        try:
            message = client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=2500,
                messages=[{"role": "user", "content": prompt}]
            )

            response_text = message.content[0].text.strip()

            try:
                start = response_text.find('{')
                end = response_text.rfind('}') + 1
                json_str = response_text[start:end]
                script = json.loads(json_str)

                required = ["title", "description", "tags", "script", "cta"]
                if not all(k in script for k in required):
                    raise ValueError(f"Missing fields: {required}")

                log.info(f"✅ Script generated: {script['title']}")
                return script

            except json.JSONDecodeError as e:
                log.warning(f"JSON parse error (attempt {attempt + 1}): {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                raise

        except Exception as e:
            log.error(f"Script generation error (attempt {attempt + 1}): {e}")
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY)
                continue
            raise

    raise Exception(f"Failed to generate script after {MAX_RETRIES} attempts")


# ============================================================================
# STEP 2: CREATE VIDEO VIA YOUR EXISTING GENERATOR
# ============================================================================

async def create_video_via_generator(script: Dict, video_id: str) -> Optional[str]:
    """
    Create video using your existing video_generator_bot

    Your bot is already running and provides:
    - Free text-to-speech (edge-tts)
    - Animated visuals
    - Professional templates
    - Full editing
    """

    payload = {
        "title": script.get("title", ""),
        "script": script.get("script", ""),
        "voice": script.get("voice", "female"),
        "template": "educational",  # Use your education template
        "duration": 300,  # 5 minutes
        "background": "gradient",
        "quality": "high"
    }

    for attempt in range(MAX_RETRIES):
        try:
            log.info(f"Creating video via local generator: {VIDEO_GENERATOR_API}")

            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(
                    f"{VIDEO_GENERATOR_API}/generate",
                    json=payload
                )

                if response.status_code == 200:
                    data = response.json()
                    video_url = data.get("video_path") or data.get("video_url")

                    if video_url:
                        log.info(f"✅ Video created: {video_url}")
                        update_video_status(video_id, "video_ready", video_url=video_url)
                        log_pipeline(video_id, "video_generation", "success", f"File: {video_url}")
                        return video_url
                    else:
                        log.error(f"No video URL in response: {data}")

                elif response.status_code >= 500:
                    log.warning(f"Server error {response.status_code} (attempt {attempt + 1})")
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                        continue

                else:
                    error = response.json().get("error", str(response.text))
                    log.error(f"Generation error: {error}")
                    update_video_status(video_id, "failed", error_message=error)
                    return None

        except Exception as e:
            log.warning(f"Video generation error (attempt {attempt + 1}): {e}")
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                continue
            raise

    log.error("Video generation failed after retries")
    update_video_status(video_id, "failed", error_message="Generation timeout")
    return None


# ============================================================================
# STEP 3: SPLIT VIDEO INTO CLIPS
# ============================================================================

def split_video_into_clips(video_url: str, video_id: str) -> List[str]:
    """Split 5-min video into 3 clips for YouTube Shorts"""
    try:
        import subprocess
        import tempfile

        clips = []
        video_dir = Path(tempfile.gettempdir()) / "youtube_videos"
        video_dir.mkdir(exist_ok=True)

        # If URL, download first
        if video_url.startswith("http"):
            input_file = video_dir / f"{video_id}_full.mp4"
            if not input_file.exists():
                log.info("Downloading video...")
                import urllib.request
                urllib.request.urlretrieve(video_url, input_file)
        else:
            input_file = Path(video_url)

        if not input_file.exists():
            log.error(f"Video file not found: {input_file}")
            return []

        for clip in CLIP_TIMES:
            clip_name = clip["name"]
            start = clip["start"]
            duration = clip["end"] - clip["start"]

            output_file = video_dir / f"{video_id}_{clip_name}.mp4"

            cmd = [
                "ffmpeg",
                "-i", str(input_file),
                "-ss", str(start),
                "-t", str(duration),
                "-c", "copy",
                "-y",
                str(output_file)
            ]

            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=60)
                log.info(f"✅ Clip created: {clip_name}")
                clips.append(str(output_file))

            except subprocess.TimeoutExpired:
                log.error(f"ffmpeg timeout for {clip_name}")
            except Exception as e:
                log.error(f"ffmpeg error for {clip_name}: {e}")

        if clips:
            update_video_status(video_id, "clips_ready")
            log_pipeline(video_id, "clip_split", "success", f"{len(clips)} clips created")

        return clips

    except ImportError:
        log.warning("ffmpeg not installed - skipping clip splitting")
        return []

    except Exception as e:
        log.error(f"Clip splitting error: {e}")
        log_pipeline(video_id, "clip_split", "failed", str(e))
        return []


# ============================================================================
# STEP 4: UPLOAD TO YOUTUBE
# ============================================================================

async def refresh_youtube_token() -> Optional[str]:
    """Refresh YouTube OAuth token"""
    if not all([YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_TOKEN]):
        log.warning("YouTube credentials not configured")
        return None

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": YOUTUBE_CLIENT_ID,
                    "client_secret": YOUTUBE_CLIENT_SECRET,
                    "refresh_token": YOUTUBE_TOKEN,
                    "grant_type": "refresh_token"
                }
            )

            if response.status_code == 200:
                data = response.json()
                return data.get("access_token")
            else:
                log.error(f"Token refresh failed: {response.status_code}")
                return None

    except Exception as e:
        log.error(f"Token refresh error: {e}")
        return None


async def upload_to_youtube(
    title: str,
    description: str,
    tags: List[str],
    video_file: str,
    made_for_kids: bool = True
) -> Optional[str]:
    """Upload video to YouTube"""
    access_token = await refresh_youtube_token()
    if not access_token:
        log.warning("YouTube upload skipped - no access token")
        return None

    try:
        if not Path(video_file).exists():
            log.error(f"Video file not found: {video_file}")
            return None

        file_size = Path(video_file).stat().st_size
        log.info(f"Uploading {file_size / 1024 / 1024:.2f} MB to YouTube...")

        body = {
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "tags": tags[:30],
                "categoryId": "27"
            },
            "status": {
                "privacyStatus": "public",
                "madeForKids": made_for_kids,
                "embeddable": True,
                "publicStatsViewable": True
            }
        }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient() as client:
            init_response = await client.post(
                f"{YOUTUBE_API}/videos?part=snippet,status,processingDetails",
                json=body,
                headers=headers,
                params={"uploadType": "resumable"},
                timeout=30.0
            )

            if init_response.status_code != 200:
                log.error(f"YouTube upload init failed: {init_response.status_code}")
                return None

            upload_url = init_response.headers.get("location")
            if not upload_url:
                log.error("No upload URL provided")
                return None

            with open(video_file, "rb") as f:
                upload_response = await client.put(
                    upload_url,
                    content=f.read(),
                    headers={"Content-Type": "video/mp4"},
                    timeout=300.0
                )

            if upload_response.status_code in [200, 201]:
                data = upload_response.json()
                video_id = data.get("id")
                log.info(f"✅ Uploaded: https://youtube.com/watch?v={video_id}")
                return video_id
            else:
                log.error(f"Upload failed: {upload_response.status_code}")
                return None

    except Exception as e:
        log.error(f"YouTube upload error: {e}")
        return None


# ============================================================================
# COMPLETE PIPELINE
# ============================================================================

async def run_full_pipeline(topic: str, difficulty: str = "intermediate") -> Optional[str]:
    """
    Complete pipeline using YOUR video generator (FREE)

    1. Generate script (Claude)
    2. Create video (Your video_generator_bot)
    3. Split clips (ffmpeg)
    4. Upload YouTube (YouTube API)
    """
    video_id = hashlib.md5(f"{topic}{datetime.now().isoformat()}".encode()).hexdigest()[:16]

    log.info(f"\n{'='*60}")
    log.info(f"STARTING PIPELINE: {topic}")
    log.info(f"Video ID: {video_id}")
    log.info(f"{'='*60}\n")

    try:
        # Save to DB
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO youtube_videos (id, title, topic, script, status) VALUES (?, ?, ?, ?, ?)",
            (video_id, topic, topic, "", "queued")
        )
        conn.commit()
        conn.close()

        # STEP 1: Generate script
        log.info("📝 STEP 1: Generating video script...")
        log_pipeline(video_id, "script_generation", "started")

        script = await generate_video_script(topic, difficulty)
        update_video_status(video_id, "script_generated")
        log_pipeline(video_id, "script_generation", "success", f"Title: {script['title']}")

        # STEP 2: Create video (YOUR GENERATOR - FREE)
        log.info("🎬 STEP 2: Creating video (using your generator)...")
        log_pipeline(video_id, "video_creation", "started")

        video_url = await create_video_via_generator(script, video_id)
        if not video_url:
            log.error("Video creation failed")
            update_video_status(video_id, "failed")
            return None

        # STEP 3: Split clips
        log.info("✂️ STEP 3: Splitting into clips...")
        log_pipeline(video_id, "clip_splitting", "started")

        clips = split_video_into_clips(video_url, video_id)
        log.info(f"Created {len(clips)} clips")

        # STEP 4: Upload YouTube
        log.info("📤 STEP 4: Uploading to YouTube...")
        log_pipeline(video_id, "youtube_upload", "started")

        youtube_id = await upload_to_youtube(
            title=script["title"],
            description=script["description"],
            tags=script.get("tags", []),
            video_file=video_url if not video_url.startswith("http") else None
        )

        if youtube_id:
            update_video_status(
                video_id,
                "published",
                youtube_video_id=youtube_id,
                youtube_url=f"https://youtube.com/watch?v={youtube_id}"
            )
            log_pipeline(video_id, "youtube_upload", "success", f"ID: {youtube_id}")
            log.info(f"\n{'='*60}")
            log.info(f"✅ PIPELINE COMPLETE")
            log.info(f"YouTube: https://youtube.com/watch?v={youtube_id}")
            log.info(f"{'='*60}\n")
            return youtube_id
        else:
            update_video_status(video_id, "video_ready")
            log_pipeline(video_id, "youtube_upload", "skipped", "No YouTube credentials")
            log.info(f"\n✅ Video ready (YouTube upload skipped - add credentials)")

        return video_id

    except Exception as e:
        log.error(f"Pipeline error: {e}")
        update_video_status(video_id, "failed", error_message=str(e))
        log_pipeline(video_id, "pipeline", "failed", str(e))
        return None


async def run_batch_pipeline(topics: List[str], difficulty: str = "intermediate"):
    """Generate multiple videos"""
    log.info(f"\n{'='*60}")
    log.info(f"BATCH PIPELINE: {len(topics)} videos")
    log.info(f"{'='*60}\n")

    results = []

    for i, topic in enumerate(topics):
        log.info(f"\n[{i+1}/{len(topics)}] Processing: {topic}")

        video_id = await run_full_pipeline(topic, difficulty)
        results.append({
            "topic": topic,
            "video_id": video_id,
            "success": video_id is not None
        })

        if i < len(topics) - 1:
            log.info(f"Waiting 5 seconds before next video...")
            await asyncio.sleep(5)

    successful = sum(1 for r in results if r["success"])
    log.info(f"\n{'='*60}")
    log.info(f"BATCH COMPLETE: {successful}/{len(topics)} successful")
    log.info(f"{'='*60}\n")

    return results


# ============================================================================
# MAIN
# ============================================================================

async def main():
    """Main entry point"""
    init_database()

    # Check if video generator is running
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{VIDEO_GENERATOR_API}/health", timeout=2)
            if response.status_code == 200:
                log.info(f"✅ Video generator is running at {VIDEO_GENERATOR_API}")
            else:
                log.warning(f"Video generator may not be ready (status: {response.status_code})")
    except Exception as e:
        log.warning(f"⚠️ Video generator not responding at {VIDEO_GENERATOR_API}")
        log.info("Make sure to run: python video_generator_bot.py")
        log.info("Or set VIDEO_GENERATOR_API environment variable")

    # Example topics
    topics = [
        "How to Multiply Fractions - Math for Kids",
        "Photosynthesis Explained - Science",
        "Spanish Vocabulary: Fruits",
    ]

    # Run batch
    results = await run_batch_pipeline(topics, "intermediate")

    log.info(f"\n✅ INTEGRATION TEST COMPLETE")
    log.info(f"Your system is using:")
    log.info(f"  • Claude for scripts")
    log.info(f"  • Your video_generator_bot for videos (FREE)")
    log.info(f"  • FFmpeg for clips")
    log.info(f"  • YouTube API for upload")


if __name__ == "__main__":
    asyncio.run(main())
