"""
HeyGen API Integration for automated video generation
"""

import httpx
import logging
import os
from typing import Optional

log = logging.getLogger("heygan")

HEYGAN_API_KEY = os.getenv("HEYGAN_API_KEY")
HEYGAN_API_BASE = "https://api.heygen.com"

# Avatar mapping: user-friendly names to HeyGen avatar IDs
AVATAR_MAP = {
    "anna": "anna_public_ca_en",
    "carlos": "carlos_public_ca_en",
    "emma": "emma_public_ca_en",
    "james": "james_public_ca_en",
    "lisa": "lisa_public_ca_en",
    "marcus": "marcus_public_ca_en",
    "olivia": "olivia_public_ca_en",
    "ryan": "ryan_public_ca_en",
}

# Language/Voice mapping
VOICE_MAP = {
    "english_us": {"language": "English (US)", "accent": "American"},
    "english_uk": {"language": "English (UK)", "accent": "British"},
    "english_au": {"language": "English (AU)", "accent": "Australian"},
    "spanish": {"language": "Spanish", "accent": "Spain"},
    "spanish_mx": {"language": "Spanish (Mexico)", "accent": "Mexican"},
    "french": {"language": "French", "accent": "France"},
    "german": {"language": "German", "accent": "Germany"},
    "italian": {"language": "Italian", "accent": "Italy"},
    "portuguese": {"language": "Portuguese", "accent": "Brazil"},
    "portuguese_pt": {"language": "Portuguese (PT)", "accent": "Portugal"},
    "dutch": {"language": "Dutch", "accent": "Netherlands"},
    "swedish": {"language": "Swedish", "accent": "Sweden"},
    "norwegian": {"language": "Norwegian", "accent": "Norway"},
    "danish": {"language": "Danish", "accent": "Denmark"},
    "polish": {"language": "Polish", "accent": "Poland"},
    "russian": {"language": "Russian", "accent": "Russia"},
    "japanese": {"language": "Japanese", "accent": "Japan"},
    "korean": {"language": "Korean", "accent": "Korea"},
    "chinese_simplified": {"language": "Chinese (Simplified)", "accent": "Mainland"},
    "chinese_traditional": {"language": "Chinese (Traditional)", "accent": "Taiwan"},
    "arabic": {"language": "Arabic", "accent": "Modern Standard"},
    "hindi": {"language": "Hindi", "accent": "India"},
}


async def generate_video(
    order_id: int,
    script: str,
    avatar: str,
    language: str,
    video_type: str,
) -> Optional[str]:
    """
    Generate a video using HeyGen API
    Returns video URL if successful, None if failed
    """

    if not HEYGAN_API_KEY:
        log.error("HEYGAN_API_KEY not configured")
        return None

    try:
        # Map user-friendly avatar name to HeyGen ID
        avatar_id = AVATAR_MAP.get(avatar.lower(), "anna_public_ca_en")

        # Prepare voice settings
        voice_key = language.lower()
        voice_settings = VOICE_MAP.get(voice_key, VOICE_MAP["english_us"])

        # Create video generation request
        headers = {
            "X-Api-Key": HEYGAN_API_KEY,
            "Content-Type": "application/json",
        }

        payload = {
            "avatar_id": avatar_id,
            "script": script,
            "voice": {
                "language": voice_settings["language"],
                "accent": voice_settings["accent"],
            },
            "title": f"Order #{order_id} - {video_type}",
            "video_resolution": "1080p",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{HEYGAN_API_BASE}/v1/videos/generate",
                json=payload,
                headers=headers,
            )

        if response.status_code == 201:
            data = response.json()
            video_id = data.get("video_id")
            log.info(f"Video generation started for order {order_id}: {video_id}")
            return video_id
        else:
            log.error(f"HeyGen API error: {response.status_code} - {response.text}")
            return None

    except Exception as e:
        log.error(f"Video generation failed for order {order_id}: {str(e)}")
        return None


async def get_video_url(video_id: str) -> Optional[str]:
    """
    Poll HeyGen API to get the video URL once it's ready
    Returns video URL if ready, None if still processing
    """

    if not HEYGAN_API_KEY:
        return None

    try:
        headers = {
            "X-Api-Key": HEYGAN_API_KEY,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{HEYGAN_API_BASE}/v1/videos/{video_id}",
                headers=headers,
            )

        if response.status_code == 200:
            data = response.json()
            status = data.get("status")

            if status == "completed":
                video_url = data.get("video_url")
                log.info(f"Video ready: {video_id}")
                return video_url
            else:
                log.info(f"Video still processing: {video_id} ({status})")
                return None
        else:
            log.error(f"HeyGen API error: {response.status_code}")
            return None

    except Exception as e:
        log.error(f"Failed to get video URL: {str(e)}")
        return None
