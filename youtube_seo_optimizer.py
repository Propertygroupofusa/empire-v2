"""
YOUTUBE SEO METADATA OPTIMIZER
================================
Rewrites video titles/descriptions/tags for search ranking and
click-through, and keeps every future upload optimized automatically.

Two entry points:
- optimize_uploaded_video(video_id, ...) - called right after an upload
  succeeds (from youtube_upload_bot.py / youtube_auto_pipeline*.py) to
  push an SEO-rewritten title/description/tags onto the video that just
  went live. Never raises - a failure here must not fail the upload.
- backfill_channel() - one-time (re-runnable) pass over every video
  already on the channel, rewriting each one's metadata in place.
  Trigger this explicitly (see routers/video_seo.py) once you're ready
  to edit everything that's already live - it is NOT run automatically.
"""

import os
import json
import asyncio
import logging

import httpx
import anthropic

log = logging.getLogger("youtube_seo")

YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET", "")
YOUTUBE_REFRESH_TOKEN = os.getenv("YOUTUBE_REFRESH_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

_claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
if _claude is None:
    log.warning("ANTHROPIC_API_KEY not set - YouTube SEO rewrites will be skipped")

# Tracks which video IDs have already been rewritten so re-running the
# backfill (or restarting the app) doesn't keep re-editing the same videos.
SEEN_PATH = os.getenv("YOUTUBE_SEO_SEEN_PATH", "youtube_seo_seen.json")


async def get_access_token() -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": YOUTUBE_CLIENT_ID,
                "client_secret": YOUTUBE_CLIENT_SECRET,
                "refresh_token": YOUTUBE_REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
        )
        r.raise_for_status()
        return r.json()["access_token"]


def _generate_seo_metadata(current_title: str, current_description: str, current_tags: list):
    """Ask Claude for an SEO/CTR-optimized rewrite, grounded in whatever
    metadata already exists - that's the only signal available for videos
    that were already uploaded, so it doubles as the input for backfill
    and for brand-new uploads alike."""
    if _claude is None:
        return None

    prompt = f"""You are a YouTube SEO expert. Rewrite this video's metadata to maximize search ranking and click-through rate, while staying true to the video's actual subject - never invent facts not implied by the existing metadata.

Current title: {current_title}
Current description: {current_description}
Current tags: {", ".join(current_tags) if current_tags else "(none)"}

Return ONLY valid JSON with this exact shape, no other text:
{{"title": "...", "description": "...", "tags": ["...", "..."]}}

Rules:
- title: <= 100 characters, keyword-forward, compelling but not clickbait/misleading
- description: <= 1500 characters, first 2 lines are the hook (shown before "Show more"), include relevant keywords naturally, end with a short call-to-action
- tags: 8-15 relevant single/short-phrase tags, no hashtags, no duplicates
"""
    try:
        resp = _claude.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text.split("\n", 1)[1] if "\n" in text else text
        data = json.loads(text)
        return {
            "title": str(data["title"])[:100],
            "description": str(data["description"])[:5000],
            "tags": [str(t) for t in data.get("tags", [])][:30],
        }
    except Exception as e:
        log.error(f"SEO metadata generation failed: {e}")
        return None


async def optimize_uploaded_video(
    video_id: str,
    current_title: str = None,
    current_description: str = None,
    current_tags: list = None,
) -> bool:
    """Rewrite one video's live title/description/tags for SEO. If the
    caller already has the metadata it just uploaded, pass it in to skip
    a lookup; otherwise it's fetched from the API. Any failure here is
    logged and swallowed - it must never take down the upload that
    triggered it."""
    try:
        access_token = await get_access_token()
    except Exception as e:
        log.error(f"YouTube token refresh failed, cannot SEO-optimize {video_id}: {e}")
        return False

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            category_id = "22"
            if current_title is None:
                r = await client.get(
                    "https://www.googleapis.com/youtube/v3/videos",
                    params={"part": "snippet", "id": video_id},
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                r.raise_for_status()
                items = r.json().get("items", [])
                if not items:
                    log.warning(f"Video {video_id} not found, skipping SEO optimize")
                    return False
                snippet = items[0]["snippet"]
                current_title = snippet.get("title", "")
                current_description = snippet.get("description", "")
                current_tags = snippet.get("tags", [])
                category_id = snippet.get("categoryId", "22")

            new_meta = _generate_seo_metadata(current_title, current_description or "", current_tags or [])
            if new_meta is None:
                return False

            r = await client.put(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "snippet"},
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                json={
                    "id": video_id,
                    "snippet": {
                        "title": new_meta["title"],
                        "description": new_meta["description"],
                        "tags": new_meta["tags"],
                        "categoryId": category_id,
                    },
                },
            )
            if r.status_code != 200:
                log.error(f"SEO metadata update failed for {video_id}: {r.status_code} {r.text[:300]}")
                return False

            log.info(f'SEO-optimized {video_id}: "{current_title[:50]}" -> "{new_meta["title"][:50]}"')
            return True
    except Exception as e:
        log.error(f"SEO optimize error for {video_id}: {e}")
        return False


async def _list_all_video_ids(access_token: str) -> list:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={"part": "contentDetails", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            return []
        uploads_playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

        video_ids = []
        page_token = None
        while True:
            params = {"part": "contentDetails", "playlistId": uploads_playlist_id, "maxResults": 50}
            if page_token:
                params["pageToken"] = page_token
            r = await client.get(
                "https://www.googleapis.com/youtube/v3/playlistItems",
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            r.raise_for_status()
            data = r.json()
            video_ids.extend(item["contentDetails"]["videoId"] for item in data.get("items", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return video_ids


def _load_seen() -> set:
    if os.path.exists(SEEN_PATH):
        try:
            with open(SEEN_PATH) as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def _save_seen(seen: set):
    try:
        with open(SEEN_PATH, "w") as f:
            json.dump(sorted(seen), f)
    except Exception as e:
        log.warning(f"Could not persist SEO backfill progress: {e}")


async def backfill_channel(force: bool = False, limit: int = None) -> dict:
    """Rewrite every video already on the channel. Skips videos already
    optimized in a prior run unless force=True. Sleeps briefly between
    videos to stay well under YouTube's write quota."""
    access_token = await get_access_token()
    video_ids = await _list_all_video_ids(access_token)
    seen = set() if force else _load_seen()
    to_process = [v for v in video_ids if v not in seen]
    if limit:
        to_process = to_process[:limit]

    results = {
        "total_on_channel": len(video_ids),
        "processed": 0,
        "skipped_already_done": len(video_ids) - len(to_process),
        "failed": [],
    }
    for video_id in to_process:
        ok = await optimize_uploaded_video(video_id)
        if ok:
            seen.add(video_id)
            results["processed"] += 1
        else:
            results["failed"].append(video_id)
        _save_seen(seen)
        await asyncio.sleep(1)

    return results
