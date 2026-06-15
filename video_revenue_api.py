"""
VIDEO REVENUE API
==================
REST endpoints for selling AI video services to clients.
Includes Synthesia generation + YouTube auto-publish.
"""

import os
import logging
from fastapi import FastAPI, Request, HTTPException

from synthesia_video_bot import (
    create_video, get_video, verify_webhook,
    property_listing_script, payee_trust_onboarding_script,
    social_content_script, cold_call_followup_script,
)
from youtube_upload_bot import (
    synthesia_to_youtube, property_listing_metadata, social_content_metadata,
)

log = logging.getLogger("video_api")
app = FastAPI(title="AI Video Revenue API")

PRICING = {
    "property_listing": 75,
    "onboarding": 0,
    "social_content": 50,
    "cold_call_followup": 25,
}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "video-revenue-api"}


@app.post("/generate/property-listing")
async def generate_property_listing(payload: dict):
    """payload: { address, price, beds, baths, features, client_email } | $75/video"""
    script = property_listing_script(
        address=payload["address"],
        price=payload["price"],
        beds=payload["beds"],
        baths=payload["baths"],
        features=payload["features"],
    )
    result = await create_video(
        script=script,
        use_case="real_estate",
        title=f"Listing: {payload['address']}",
        callback_id=payload.get("client_email", ""),
    )
    return {"video": result, "price": PRICING["property_listing"]}


@app.post("/generate/social-content")
async def generate_social_content(payload: dict):
    """payload: { business_name, topic, cta, client_email } | $50/video"""
    script = social_content_script(
        business_name=payload["business_name"],
        topic=payload["topic"],
        cta=payload["cta"],
    )
    result = await create_video(
        script=script,
        use_case="marketing",
        title=f"Social: {payload['business_name']} - {payload['topic']}",
        callback_id=payload.get("client_email", ""),
    )
    return {"video": result, "price": PRICING["social_content"]}


@app.post("/generate/cold-call-followup")
async def generate_followup(payload: dict):
    """payload: { lead_name, property_address, lead_email } | $25/video"""
    script = cold_call_followup_script(
        lead_name=payload["lead_name"],
        property_address=payload["property_address"],
    )
    result = await create_video(
        script=script,
        use_case="sales",
        title=f"Follow-up: {payload['lead_name']}",
        callback_id=payload.get("lead_email", ""),
    )
    return {"video": result, "price": PRICING["cold_call_followup"]}


@app.post("/generate/payee-trust-onboarding")
async def generate_onboarding(payload: dict):
    """payload: { user_name, user_email } | internal use"""
    script = payee_trust_onboarding_script(user_name=payload.get("user_name", "there"))
    result = await create_video(
        script=script,
        use_case="finance",
        title=f"Onboarding: {payload.get('user_name', 'New User')}",
        callback_id=payload.get("user_email", ""),
    )
    return {"video": result}


@app.get("/video/{video_id}")
async def video_status(video_id: str):
    return await get_video(video_id)


@app.post("/webhook/synthesia")
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


@app.post("/publish/youtube/property-listing")
async def publish_property_youtube(payload: dict):
    """payload: { synthesia_download_url, address, price, beds, baths } | publishes public"""
    meta = property_listing_metadata(
        address=payload["address"],
        price=payload["price"],
        beds=payload["beds"],
        baths=payload["baths"],
    )
    result = await synthesia_to_youtube(
        synthesia_download_url=payload["synthesia_download_url"],
        title=meta["title"],
        description=meta["description"],
        tags=meta["tags"],
        privacy="public",
    )
    return result


@app.post("/publish/youtube/social-content")
async def publish_social_youtube(payload: dict):
    """payload: { synthesia_download_url, business_name, topic } | publishes unlisted"""
    meta = social_content_metadata(
        business_name=payload["business_name"],
        topic=payload["topic"],
    )
    result = await synthesia_to_youtube(
        synthesia_download_url=payload["synthesia_download_url"],
        title=meta["title"],
        description=meta["description"],
        tags=meta["tags"],
        privacy="unlisted",
    )
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
