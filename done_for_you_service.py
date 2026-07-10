"""
DONE-FOR-YOU VIDEO SERVICE
==========================
We generate videos FOR clients on monthly retainer.
Full content management: strategy + creation + delivery.

PRICING:
  Basic: $297/month → 10 videos/month + strategy
  Pro: $897/month → 30 videos/month + analytics
  Premium: $1997/month → 100 videos/month + full management

REQUIRED ENV VARS:
  STRIPE_SECRET_KEY, STATE_DIR, DFY_API_PORT (8003)
"""

import os
import json
import logging
from datetime import datetime
import secrets

from fastapi import FastAPI, HTTPException, Header, Depends
import stripe
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dfy_service")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

STATE_DIR = os.getenv("STATE_DIR", "/data/bot_state")
CLIENTS_FILE = os.path.join(STATE_DIR, "dfy_clients.json")
DFY_API_PORT = int(os.getenv("DFY_API_PORT", 8003))

app = FastAPI(title="Done-For-You Video Service")

DFY_TIERS = {
    "basic": {
        "videos_per_month": 10,
        "price": "$297",
        "includes": ["Custom strategy", "Weekly updates", "Basic analytics"],
    },
    "pro": {
        "videos_per_month": 30,
        "price": "$897",
        "includes": ["Custom strategy", "Weekly updates", "Advanced analytics", "A/B testing"],
    },
    "premium": {
        "videos_per_month": 100,
        "price": "$1997",
        "includes": ["Full management", "24hr turnaround", "Advanced analytics", "Conversion optimization"],
    },
}

def load_clients():
    """Load DFY clients"""
    try:
        with open(CLIENTS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"clients": {}, "client_keys": {}}

def save_clients(data):
    """Save DFY clients"""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(CLIENTS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def verify_client_key(x_client_key: str = Header(...)):
    """Verify client and check subscription"""
    if not x_client_key:
        raise HTTPException(status_code=401, detail="Client key required")

    clients = load_clients()

    if x_client_key not in clients["client_keys"]:
        raise HTTPException(status_code=401, detail="Invalid client key")

    client_id = clients["client_keys"][x_client_key]
    client = clients["clients"].get(client_id)

    if not client or client.get("status") != "active":
        raise HTTPException(status_code=403, detail="Subscription not active")

    return client

@app.get("/health")
def health():
    return {"status": "ok", "service": "done_for_you_service"}

@app.post("/onboard")
def onboard_client(
    email: str,
    business_name: str,
    niche: str,
    tier: str = "basic",
):
    """Onboard new DFY client"""
    if tier not in DFY_TIERS:
        raise HTTPException(status_code=400, detail="Invalid tier")

    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    # In production, create Stripe checkout
    # For now, return onboarding info

    clients = load_clients()
    client_id = secrets.token_hex(16)
    client_key = secrets.token_urlsafe(32)

    clients["clients"][client_id] = {
        "email": email,
        "business_name": business_name,
        "niche": niche,
        "tier": tier,
        "status": "pending",
        "onboarded_at": datetime.utcnow().isoformat(),
        "videos_delivered": 0,
        "client_key": client_key,
        "content_strategy": None,
        "delivery_schedule": [],
    }
    clients["client_keys"][client_key] = client_id
    save_clients(clients)

    log.info(f"📝 Onboarding: {business_name} ({niche}) - Tier: {tier}")

    return {
        "client_id": client_id,
        "client_key": client_key,
        "tier": tier,
        "videos_per_month": DFY_TIERS[tier]["videos_per_month"],
        "onboarding_steps": [
            "1. Content strategy call (scheduled)",
            "2. Brand guidelines collection",
            "3. First batch of 3 videos",
            "4. Feedback & optimization",
            "5. Regular delivery schedule starts",
        ],
    }

@app.post("/strategy-call")
def schedule_strategy_call(
    topics: list,
    style_preferences: str,
    target_audience: str,
    client=Depends(verify_client_key),
):
    """Submit content strategy for review"""
    clients = load_clients()

    # Find this client
    client_id = None
    for cid, c in clients["clients"].items():
        if c.get("email") == client["email"]:
            client_id = cid
            break

    if client_id:
        clients["clients"][client_id]["content_strategy"] = {
            "topics": topics,
            "style": style_preferences,
            "audience": target_audience,
            "approved": False,
            "submitted_at": datetime.utcnow().isoformat(),
        }
        save_clients(clients)

    log.info(f"📋 Strategy submitted: {client['business_name']}")

    return {
        "status": "submitted",
        "message": "Your content strategy has been received. Our team will review and contact you within 24 hours.",
        "next_step": "Wait for strategy call confirmation email",
    }

@app.get("/delivery-schedule")
def get_delivery_schedule(client=Depends(verify_client_key)):
    """Get client's video delivery schedule"""
    tier = client.get("tier", "basic")
    videos_per_month = DFY_TIERS[tier]["videos_per_month"]

    return {
        "tier": tier,
        "videos_per_month": videos_per_month,
        "delivery_frequency": f"~{videos_per_month // 4} per week",
        "turnaround_time": "5-7 business days" if tier == "premium" else "10-14 business days",
        "format_options": ["YouTube Shorts", "TikTok", "Instagram Reels", "Facebook"],
        "next_delivery": "Monday",
        "videos_delivered_this_month": client.get("videos_delivered", 0),
    }

@app.post("/request-video")
def request_custom_video(
    topic: str,
    description: str,
    format: str = "YouTube Shorts",
    client=Depends(verify_client_key),
):
    """Submit custom video request"""
    log.info(f"🎬 Video requested by {client['business_name']}: {topic}")

    return {
        "request_id": secrets.token_hex(8),
        "topic": topic,
        "format": format,
        "status": "queued",
        "estimated_delivery": "10 business days",
        "position_in_queue": 3,
    }

@app.get("/my-videos")
def list_delivered_videos(client=Depends(verify_client_key)):
    """List all videos delivered to this client"""
    return {
        "client": client["business_name"],
        "total_delivered": client.get("videos_delivered", 0),
        "this_month": 0,
        "videos": [
            {
                "id": "vid_001",
                "title": "Example Video Title",
                "format": "YouTube Shorts",
                "views": 1250,
                "engagement_rate": "8.5%",
                "delivered_at": "2026-07-05",
                "download_url": "https://cdn.propertygroupusa.com/...",
            }
        ],
    }

@app.get("/analytics")
def get_analytics(client=Depends(verify_client_key)):
    """Analytics dashboard for DFY client"""
    tier = client.get("tier", "basic")
    has_analytics = tier in ["pro", "premium"]

    if not has_analytics and tier == "basic":
        raise HTTPException(status_code=403, detail="Analytics available in Pro+ tiers")

    return {
        "tier": tier,
        "total_videos": client.get("videos_delivered", 0),
        "total_views": 15430,
        "total_engagement": 1280,
        "average_ctr": "8.2%",
        "top_performing": {
            "title": "Top Video Title",
            "views": 3250,
            "engagement_rate": "12.5%",
        },
        "trends": {
            "week_over_week_growth": "+24%",
            "best_posting_time": "Tuesday 2PM EST",
            "best_format": "YouTube Shorts (65% of views)",
        },
    }

@app.get("/client/info")
def get_client_info(client=Depends(verify_client_key)):
    """Get client account info"""
    tier = client.get("tier", "basic")

    return {
        "business_name": client["business_name"],
        "niche": client["niche"],
        "tier": tier,
        "status": client["status"],
        "videos_per_month": DFY_TIERS[tier]["videos_per_month"],
        "videos_delivered": client.get("videos_delivered", 0),
        "joined": client.get("onboarded_at"),
        "next_billing_date": "2026-08-10",
        "includes": DFY_TIERS[tier]["includes"],
    }

@app.get("/admin/clients")
def list_dfy_clients(admin_key: str):
    """Admin: List all DFY clients"""
    if admin_key != os.getenv("ADMIN_API_KEY", ""):
        raise HTTPException(status_code=401, detail="Invalid admin key")

    clients = load_clients()

    return {
        "total": len(clients["clients"]),
        "monthly_revenue": len(clients["clients"]) * 297,  # Rough avg
        "clients": [
            {
                "id": cid,
                "business": c.get("business_name"),
                "niche": c.get("niche"),
                "tier": c.get("tier"),
                "status": c.get("status"),
                "videos_delivered": c.get("videos_delivered", 0),
                "joined": c.get("onboarded_at"),
            }
            for cid, c in clients["clients"].items()
        ],
    }

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("DONE-FOR-YOU VIDEO SERVICE")
    log.info(f"Listening on port {DFY_API_PORT}")
    log.info("=" * 60)

    uvicorn.run(app, host="0.0.0.0", port=DFY_API_PORT)
