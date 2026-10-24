"""
VIDEO REVENUE API
==================
REST endpoints for selling AI video services to clients.
Includes Synthesia generation + YouTube auto-publish.
"""

import os
import logging
from typing import List, Optional
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel

from synthesia_video_bot import (
    create_video, get_video, verify_webhook,
    property_listing_script, payee_trust_onboarding_script,
    social_content_script, cold_call_followup_script,
)
from youtube_upload_bot import synthesia_to_youtube
from admin_auth import require_admin_key

log = logging.getLogger("video_api")
router = APIRouter()

PRICING = {
    "property_listing": 75,
    "onboarding": 0,
    "social_content": 50,
    "cold_call_followup": 25,
}


class PropertyListingRequest(BaseModel):
    address: str
    price: str
    beds: str
    baths: str
    features: str
    client_email: Optional[str] = ""


class SocialContentRequest(BaseModel):
    business_name: str
    topic: str
    cta: str
    client_email: Optional[str] = ""


class ColdCallFollowupRequest(BaseModel):
    lead_name: str
    property_address: str
    lead_email: Optional[str] = ""


class OnboardingRequest(BaseModel):
    user_name: Optional[str] = "there"
    user_email: Optional[str] = ""


@router.post("/generate/property-listing")
async def generate_property_listing(payload: PropertyListingRequest):
    """$75/video"""
    script = property_listing_script(
        address=payload.address,
        price=payload.price,
        beds=payload.beds,
        baths=payload.baths,
        features=payload.features,
    )
    result = await create_video(
        script=script,
        use_case="real_estate",
        title=f"Listing: {payload.address}",
        callback_id=payload.client_email,
    )
    return {"video": result, "price": PRICING["property_listing"]}


@router.post("/generate/social-content")
async def generate_social_content(payload: SocialContentRequest):
    """$50/video"""
    script = social_content_script(
        business_name=payload.business_name,
        topic=payload.topic,
        cta=payload.cta,
    )
    result = await create_video(
        script=script,
        use_case="marketing",
        title=f"Social: {payload.business_name} - {payload.topic}",
        callback_id=payload.client_email,
    )
    return {"video": result, "price": PRICING["social_content"]}


@router.post("/generate/cold-call-followup")
async def generate_followup(payload: ColdCallFollowupRequest):
    """$25/video"""
    script = cold_call_followup_script(
        lead_name=payload.lead_name,
        property_address=payload.property_address,
    )
    result = await create_video(
        script=script,
        use_case="sales",
        title=f"Follow-up: {payload.lead_name}",
        callback_id=payload.lead_email,
    )
    return {"video": result, "price": PRICING["cold_call_followup"]}


@router.post("/generate/payee-trust-onboarding")
async def generate_onboarding(payload: OnboardingRequest):
    """internal use"""
    script = payee_trust_onboarding_script(user_name=payload.user_name)
    result = await create_video(
        script=script,
        use_case="finance",
        title=f"Onboarding: {payload.user_name}",
        callback_id=payload.user_email,
    )
    return {"video": result}


@router.get("/video/{video_id}")
async def video_status(video_id: str):
    return await get_video(video_id)


@router.post("/webhook/synthesia")
async def synthesia_webhook(request: Request):
    timestamp = request.headers.get("Synthesia-Timestamp", "")
    signature = request.headers.get("Synthesia-Signature", "")
    body = await request.body()
    body_str = body.decode("utf-8")

    if not verify_webhook(timestamp, body_str, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()
    event_type = payload.get("type")
    data = payload.get("data", {})

    if event_type == "video.completed":
        log.info(f"Video ready | ID: {data.get('id')} | Download: {data.get('download')}")
    elif event_type == "video.failed":
        log.error(f"Video failed | ID: {data.get('id')} | Reason: {data.get('message')}")

    return {"received": True}


class PublishPropertyRequest(BaseModel):
    video_url: str
    title: Optional[str] = "Property Listing"
    description: Optional[str] = ""
    tags: Optional[List[str]] = None


class PublishSocialRequest(BaseModel):
    video_url: str
    title: Optional[str] = "Social Content"
    description: Optional[str] = ""
    tags: Optional[List[str]] = None
    privacy: Optional[str] = "unlisted"


@router.post("/publish/youtube/property-listing")
async def publish_property_youtube(payload: PublishPropertyRequest):
    """publishes public"""
    result = await synthesia_to_youtube(
        synthesia_download_url=payload.video_url,
        title=payload.title,
        description=payload.description,
        tags=payload.tags,
        privacy="public",
    )
    return result


@router.post("/publish/youtube/social-content")
async def publish_social_youtube(payload: PublishSocialRequest):
    """defaults unlisted"""
    result = await synthesia_to_youtube(
        synthesia_download_url=payload.video_url,
        title=payload.title,
        description=payload.description,
        tags=payload.tags,
        privacy=payload.privacy,
    )
    return result


class BackfillSeoRequest(BaseModel):
    force: Optional[bool] = False
    limit: Optional[int] = None


class AddChannelRequest(BaseModel):
    label: str
    refresh_token: str
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    content_profile: Optional[dict] = None
    daily_upload_limit: Optional[int] = None


class PublishingRequest(BaseModel):
    enabled: bool


@router.get("/youtube/channels", dependencies=[Depends(require_admin_key)])
async def list_youtube_channels():
    """Every configured channel. Reports no credential VALUE, ever."""
    import youtube_channels
    return {"channels": await youtube_channels.list_channels()}


@router.post("/youtube/channels", dependencies=[Depends(require_admin_key)])
async def add_youtube_channel(payload: AddChannelRequest):
    """Register a channel that has already been authorised in Google.

    There is no API for CREATING a YouTube channel - the Data API's channels
    resource has list and update and no insert - so the channel itself is
    made by hand in YouTube (a Brand Account) and authorised once. This
    records the result of that.

    It is created with publishing OFF. Turning it on is a separate call,
    because adding a channel and letting it post publicly are two decisions.
    """
    import youtube_channels
    try:
        return await youtube_channels.add_channel(
            label=payload.label, refresh_token=payload.refresh_token,
            client_id=payload.client_id, client_secret=payload.client_secret,
            content_profile=payload.content_profile,
            daily_upload_limit=payload.daily_upload_limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/youtube/channels/{label}/publishing",
             dependencies=[Depends(require_admin_key)])
async def set_youtube_publishing(label: str, payload: PublishingRequest):
    """Turn automatic publishing on or off for one channel.

    Its own endpoint on purpose: this is the switch that decides whether
    anything reaches the public.
    """
    import youtube_channels
    try:
        return await youtube_channels.set_publishing(label, payload.enabled)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/youtube/health", dependencies=[Depends(require_admin_key)])
async def youtube_health_endpoint():
    """Why is nothing reaching YouTube? Read-only, uploads nothing.

    Admin-guarded like the backfill below, because it reports which
    credentials exist and relays Google's own error text. It returns no
    credential VALUE - presence and length only.
    """
    from youtube_health import youtube_health
    try:
        return await youtube_health()
    except Exception as e:
        # A diagnostic that 500s tells you nothing about the thing you came
        # to diagnose. Report its own failure as the answer.
        log.error(f"YouTube health check failed: {e}")
        return {"verdict": f"the check itself failed: {type(e).__name__}: {e}",
                "can_upload_right_now": False, "checks": []}


@router.post("/youtube/backfill-seo", dependencies=[Depends(require_admin_key)])
async def backfill_youtube_seo(payload: BackfillSeoRequest = BackfillSeoRequest()):
    """One-time (re-runnable) pass that rewrites title/description/tags
    for every video already on the channel via youtube_seo_optimizer.py.
    Not run automatically - call this explicitly when you're ready to
    edit everything that's already live. Already-optimized videos are
    skipped on repeat calls unless force=true."""
    from youtube_seo_optimizer import backfill_channel
    try:
        return await backfill_channel(force=payload.force, limit=payload.limit)
    except Exception as e:
        log.error(f"YouTube SEO backfill failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    from fastapi import FastAPI

    app = FastAPI(title="AI Video Revenue API")
    app.include_router(router)
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
