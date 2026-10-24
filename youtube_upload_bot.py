"""
YOUTUBE AUTO-UPLOAD BOT
========================
Downloads Synthesia videos and auto-uploads to YouTube.
Revenue: AI Social Media content channel + client deliverables.
"""

import os
import logging
import httpx

log = logging.getLogger("youtube")

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
YOUTUBE_REFRESH_TOKEN = os.getenv("YOUTUBE_REFRESH_TOKEN", "")
YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET", "")


async def get_access_token(channel: str = None) -> str:
    """An access token for one channel. `channel` is a configured label.

    ONE TOKEN PER CHANNEL, BECAUSE THAT IS HOW GOOGLE ISSUES THEM - the
    consent screen asks which channel you are authorising, so a network of
    channels cannot share one. With no label this reads the environment
    variables exactly as it always did, so a service configured the old way
    is unaffected.
    """
    creds = await _resolve_channel(channel)
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "refresh_token": creds.refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if not r.is_success:
            # Without this, a bad refresh token/client surfaces as a bare
            # KeyError: 'access_token' with no indication it was an OAuth
            # failure at all. Log Google's actual error before raising.
            log.error(f"YouTube OAuth token refresh failed {r.status_code}: {r.text[:500]}")
        r.raise_for_status()
        return r.json()["access_token"]


async def _resolve_channel(channel: str = None):
    """Credentials for `channel`, or the environment channel when None.

    Raises rather than returning something falsy. A caller that gets None
    here and carries on would build an upload request with an empty token and
    read Google's refusal as a channel problem; the refusal belongs at the
    point the credentials were missing, naming which channel and why.
    """
    import youtube_channels
    res = (youtube_channels.env_channel() if channel is None
           else await youtube_channels.resolve(channel))
    if not res.ok:
        raise RuntimeError(
            f"no usable YouTube credentials for "
            f"{'the default channel' if channel is None else repr(channel)}: "
            f"{res.reason}")
    return res


async def download_video(download_url: str, save_path: str = "/tmp/video.mp4") -> str:
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.get(download_url)
        with open(save_path, "wb") as f:
            f.write(r.content)
    log.info(f"Downloaded video to {save_path}")
    return save_path


async def upload_to_youtube(
    video_path: str,
    title: str,
    description: str,
    tags: list = None,
    category_id: str = "22",
    privacy: str = "public",
    channel: str = None,
) -> dict:
    """
    Uploads a video file to YouTube using resumable upload.
    Returns: { "id": "...", "url": "https://youtube.com/watch?v=..." }

    `channel` names a configured channel; None keeps the old behaviour of
    using the environment variables. Resolving it is what enforces that a
    channel has publishing turned on - a configured channel is not a channel
    that may post, and that check lives in youtube_channels.resolve, not here.
    """
    access_token = await get_access_token(channel)

    metadata = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags or [],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    file_size = os.path.getsize(video_path)

    async with httpx.AsyncClient(timeout=300) as client:
        init = await client.post(
            "https://www.googleapis.com/upload/youtube/v3/videos"
            "?uploadType=resumable&part=snippet,status",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(file_size),
                "X-Upload-Content-Type": "video/mp4",
            },
            json=metadata,
        )
        upload_url = init.headers.get("Location")

        with open(video_path, "rb") as f:
            video_data = f.read()

        upload_resp = await client.put(
            upload_url,
            headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(file_size),
            },
            content=video_data,
        )

        result = upload_resp.json()
        video_id = result.get("id")
        log.info(f"Uploaded to YouTube | ID: {video_id}")

        if video_id:
            try:
                from youtube_seo_optimizer import optimize_uploaded_video
                await optimize_uploaded_video(video_id, title, description, tags)
            except Exception as e:
                log.warning(f"SEO optimize skipped for {video_id}: {e}")

        return {
            "id": video_id,
            "url": f"https://youtube.com/watch?v={video_id}",
        }


async def synthesia_to_youtube(
    synthesia_download_url: str,
    title: str,
    description: str,
    tags: list = None,
    privacy: str = "unlisted",
    channel: str = None,
) -> dict:
    """Full pipeline: download Synthesia video -> upload to YouTube."""
    video_path = await download_video(synthesia_download_url)
    result = await upload_to_youtube(
        video_path=video_path,
        title=title,
        description=description,
        tags=tags,
        privacy=privacy,
        channel=channel,
    )
    os.remove(video_path)
    return result


def property_listing_metadata(address: str, price: str, beds: int, baths: int) -> dict:
    return {
        "title": f"FOR SALE: {address} | {price} | {beds}BR/{baths}BA",
        "description": (
            f"New listing in San Antonio! {beds} bedrooms, {baths} bathrooms.\n"
            f"Price: {price}\n\n"
            f"Contact Property Group of USA for a private tour.\n"
            f"#SanAntonioRealEstate #ForSale #PropertyGroupUSA"
        ),
        "tags": ["real estate", "san antonio", "for sale", "property group usa"],
    }


def social_content_metadata(business_name: str, topic: str) -> dict:
    return {
        "title": f"{business_name}: {topic}",
        "description": (
            f"{business_name} shares insights on {topic}.\n\n"
            f"Follow for more content like this!"
        ),
        "tags": [business_name.lower().replace(" ", ""), topic.lower()],
    }


if __name__ == "__main__":
    print("YouTube upload bot ready.")
    print("Required env vars: YOUTUBE_API_KEY, YOUTUBE_REFRESH_TOKEN, YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET")
