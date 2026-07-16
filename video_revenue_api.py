"""
VIDEO REVENUE API
==================
REST endpoints for selling AI video services to clients.
Includes Synthesia generation + YouTube auto-publish.
"""

import os
import logging
from typing import List, Optional
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from synthesia_video_bot import (
    create_video, get_video, verify_webhook,
    property_listing_script, payee_trust_onboarding_script,
    social_content_script, cold_call_followup_script,
)
from youtube_upload_bot import synthesia_to_youtube

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


if __name__ == "__main__":
    import uvicorn
    from fastapi import FastAPI

    app = FastAPI(title="AI Video Revenue API")
    app.include_router(router)
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
