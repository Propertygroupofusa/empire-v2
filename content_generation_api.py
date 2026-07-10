"""
CONTENT GENERATION API — SaaS for AI Video Creation
====================================================
Monetize the video generation system via API access.
Developers/creators pay for video generation capacity.

PRICING TIERS:
  Starter: $99/month → 100 videos/month
  Pro: $299/month → 500 videos/month
  Enterprise: $999/month → unlimited + priority

REQUIRED ENV VARS:
  STRIPE_SECRET_KEY, STRIPE_PRICE_STARTER_ID, STRIPE_PRICE_PRO_ID, etc.
  STATE_DIR, GENERATION_API_PORT (default 8002)
"""

import os
import json
import logging
from datetime import datetime, timedelta
import secrets
from pathlib import Path
from typing import Dict, Any

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import JSONResponse
import stripe
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("content_generation_api")

# Stripe configuration
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_STARTER_ID = os.getenv("STRIPE_PRICE_STARTER_ID", "")
STRIPE_PRICE_PRO_ID = os.getenv("STRIPE_PRICE_PRO_ID", "")
STRIPE_PRICE_ENTERPRISE_ID = os.getenv("STRIPE_PRICE_ENTERPRISE_ID", "")

# Configuration
STATE_DIR = os.getenv("STATE_DIR", "/data/bot_state")
DEVELOPERS_FILE = os.path.join(STATE_DIR, "api_developers.json")
GENERATION_API_PORT = int(os.getenv("GENERATION_API_PORT", 8002))

app = FastAPI(title="Content Generation API")

TIER_LIMITS = {
    "starter": {"videos_per_month": 100, "price": "$99"},
    "pro": {"videos_per_month": 500, "price": "$299"},
    "enterprise": {"videos_per_month": 999999, "price": "$999"},
}

def load_developers():
    """Load developer/subscriber database"""
    try:
        with open(DEVELOPERS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"developers": {}, "api_keys": {}}

def save_developers(data):
    """Save developer database"""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(DEVELOPERS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def verify_api_key(x_api_key: str = Header(...)):
    """Verify developer API key and check quota"""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="API key required")

    developers = load_developers()

    if x_api_key not in developers["api_keys"]:
        raise HTTPException(status_code=401, detail="Invalid API key")

    dev_id = developers["api_keys"][x_api_key]
    developer = developers["developers"].get(dev_id)

    if not developer:
        raise HTTPException(status_code=401, detail="Developer not found")

    if developer.get("status") != "active":
        raise HTTPException(status_code=403, detail="Subscription not active")

    # Check monthly quota
    current_month = datetime.utcnow().strftime("%Y-%m")
    usage = developer.get("usage", {})
    current_usage = usage.get(current_month, 0)

    tier = developer.get("tier", "starter")
    limit = TIER_LIMITS[tier]["videos_per_month"]

    if current_usage >= limit:
        raise HTTPException(status_code=429, detail=f"Monthly quota exceeded ({current_usage}/{limit})")

    return developer

@app.get("/health")
def health():
    """Health check"""
    return {"status": "ok", "service": "content_generation_api"}

@app.post("/subscribe")
def create_subscription(email: str, developer_name: str, tier: str = "starter"):
    """Create API subscription via Stripe"""
    if tier not in TIER_LIMITS:
        raise HTTPException(status_code=400, detail="Invalid tier")

    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    price_id_map = {
        "starter": STRIPE_PRICE_STARTER_ID,
        "pro": STRIPE_PRICE_PRO_ID,
        "enterprise": STRIPE_PRICE_ENTERPRISE_ID,
    }

    price_id = price_id_map.get(tier)
    if not price_id:
        raise HTTPException(status_code=500, detail=f"Price not configured for {tier}")

    try:
        customer = stripe.Customer.create(email=email, name=developer_name)

        session = stripe.checkout.Session.create(
            customer=customer.id,
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url="https://your-domain.com/dev/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://your-domain.com/dev/cancel",
        )

        developers = load_developers()
        dev_id = secrets.token_hex(16)
        developers["developers"][dev_id] = {
            "email": email,
            "name": developer_name,
            "tier": tier,
            "stripe_customer_id": customer.id,
            "stripe_session_id": session.id,
            "status": "pending",
            "usage": {},
            "created_at": datetime.utcnow().isoformat(),
        }
        save_developers(developers)

        return {
            "checkout_url": session.url,
            "developer_id": dev_id,
            "tier": tier,
            "videos_per_month": TIER_LIMITS[tier]["videos_per_month"],
        }
    except stripe.error.StripeError as e:
        log.error(f"Stripe error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/webhook/stripe")
async def stripe_webhook(request):
    """Handle Stripe subscription events"""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, os.getenv("STRIPE_WEBHOOK_SECRET", "")
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid webhook")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        log.info(f"✅ API subscription confirmed: {session.customer_email}")

        developers = load_developers()
        for dev_id, dev in developers["developers"].items():
            if dev.get("stripe_customer_id") == session.customer:
                api_key = secrets.token_urlsafe(32)
                dev["status"] = "active"
                dev["stripe_subscription_id"] = session.subscription
                dev["api_key"] = api_key
                dev["activated_at"] = datetime.utcnow().isoformat()
                developers["api_keys"][api_key] = dev_id
                save_developers(developers)
                log.info(f"🔑 API key issued to {dev['email']}: {api_key[:16]}...")
                break

    elif event["type"] == "customer.subscription.deleted":
        subscription = event["data"]["object"]
        log.warning(f"API subscription cancelled")

        developers = load_developers()
        for dev_id, dev in developers["developers"].items():
            if dev.get("stripe_customer_id") == subscription.customer:
                dev["status"] = "cancelled"
                if "api_key" in dev:
                    developers["api_keys"].pop(dev["api_key"], None)
                save_developers(developers)
                break

    return {"status": "ok"}

@app.post("/generate")
def generate_video(
    topic: str,
    style: str = "educational",
    developer=Depends(verify_api_key)
):
    """
    Generate AI video for developer

    Args:
        topic: Topic/prompt for video script
        style: "educational", "viral", "storytelling", "tips"

    Returns:
        Job ID to check status
    """
    # This would call content_bot.py's generation pipeline
    # For now, return job structure

    job_id = secrets.token_hex(16)

    # Track usage
    developers = load_developers()
    dev_id = None
    for did, dev in developers["developers"].items():
        if dev.get("api_key") == None:  # Find this developer
            dev_id = did
            break

    if dev_id:
        current_month = datetime.utcnow().strftime("%Y-%m")
        if "usage" not in developers["developers"][dev_id]:
            developers["developers"][dev_id]["usage"] = {}
        developers["developers"][dev_id]["usage"][current_month] = \
            developers["developers"][dev_id]["usage"].get(current_month, 0) + 1
        save_developers(developers)

    log.info(f"📹 Generation started: {topic} (Job {job_id[:8]}...)")

    return {
        "job_id": job_id,
        "topic": topic,
        "style": style,
        "status": "queued",
        "estimated_time_seconds": 120,
    }

@app.get("/job/{job_id}")
def get_job_status(job_id: str, developer=Depends(verify_api_key)):
    """Check video generation job status"""
    # Placeholder - would track actual job status
    return {
        "job_id": job_id,
        "status": "completed",  # or "processing", "queued", "failed"
        "video_url": "https://cdn.propertygroupusa.com/videos/abc123.mp4",
        "thumbnail_url": "https://cdn.propertygroupusa.com/thumbnails/abc123.png",
        "metadata": {
            "title": "The topic title here",
            "description": "Full description generated",
            "tags": ["tag1", "tag2", "tag3"],
            "duration_seconds": 45,
        }
    }

@app.get("/usage")
def get_usage(developer=Depends(verify_api_key)):
    """Get current month's usage for developer"""
    developers = load_developers()

    current_month = datetime.utcnow().strftime("%Y-%m")
    dev_id = None
    for did, dev in developers["developers"].items():
        if dev.get("status") == "active":  # Find current developer
            dev_id = did
            break

    if not dev_id:
        raise HTTPException(status_code=404, detail="Developer not found")

    dev = developers["developers"][dev_id]
    usage = dev.get("usage", {}).get(current_month, 0)
    tier = dev.get("tier", "starter")
    limit = TIER_LIMITS[tier]["videos_per_month"]

    return {
        "tier": tier,
        "current_month": current_month,
        "videos_used": usage,
        "videos_limit": limit,
        "videos_remaining": limit - usage,
        "reset_date": (datetime.utcnow().replace(day=1) + timedelta(days=32)).replace(day=1).isoformat(),
    }

@app.get("/developer/info")
def get_developer_info(developer=Depends(verify_api_key)):
    """Get developer account info"""
    tier = developer.get("tier", "starter")

    return {
        "email": developer["email"],
        "name": developer["name"],
        "tier": tier,
        "status": developer["status"],
        "joined": developer.get("created_at"),
        "videos_per_month": TIER_LIMITS[tier]["videos_per_month"],
        "api_key": developer.get("api_key", "")[:16] + "...",
    }

@app.get("/admin/developers")
def list_developers(admin_key: str):
    """Admin: List all API developers"""
    if admin_key != os.getenv("ADMIN_API_KEY", ""):
        raise HTTPException(status_code=401, detail="Invalid admin key")

    developers = load_developers()

    return {
        "total": len(developers["developers"]),
        "developers": [
            {
                "id": dev_id,
                "email": dev.get("email"),
                "tier": dev.get("tier"),
                "status": dev.get("status"),
                "joined": dev.get("created_at"),
                "current_month_usage": dev.get("usage", {}).get(datetime.utcnow().strftime("%Y-%m"), 0),
            }
            for dev_id, dev in developers["developers"].items()
        ],
    }

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("CONTENT GENERATION API — Developer SaaS")
    log.info(f"Listening on port {GENERATION_API_PORT}")
    log.info("=" * 60)

    uvicorn.run(app, host="0.0.0.0", port=GENERATION_API_PORT)
