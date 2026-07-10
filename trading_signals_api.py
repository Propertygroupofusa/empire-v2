"""
TRADING SIGNALS SUBSCRIPTION API
=================================
Delivers real-time trading signals to paid subscribers.
Stripe integration for subscription management + revenue tracking.

REQUIRED ENV VARS:
  STRIPE_SECRET_KEY        - Stripe API secret key
  STRIPE_PRICE_ID          - Stripe price ID for monthly subscription
  SIGNALS_API_PORT         - Port to run on (default 8001)
  STATE_DIR                - State storage (default /data/bot_state)
"""

import os
import json
import logging
from datetime import datetime
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import JSONResponse
import stripe
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("signals_api")

# Stripe configuration
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

# Configuration
STATE_DIR = os.getenv("STATE_DIR", "/data/bot_state")
SUBSCRIBERS_FILE = os.path.join(STATE_DIR, "trading_signals_subscribers.json")
SIGNALS_FILE = os.path.join(STATE_DIR, "prop_bot_state.json")
PORT = int(os.getenv("SIGNALS_API_PORT", 8001))

app = FastAPI(title="Trading Signals API")

def load_subscribers():
    """Load subscription database"""
    try:
        with open(SUBSCRIBERS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"subscribers": {}, "api_keys": {}}

def save_subscribers(data):
    """Save subscription database"""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_signals():
    """Load current trading signals from prop_bot"""
    try:
        with open(SIGNALS_FILE) as f:
            state = json.load(f)
            return {
                "profitable_days": state.get("profitable_days", []),
                "daily_pnl": state.get("daily_pnl", 0.0),
            }
    except Exception:
        return {"profitable_days": [], "daily_pnl": 0.0}

def verify_api_key(x_api_key: str = Header(...)):
    """Verify subscriber API key"""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="API key required")

    subscribers = load_subscribers()

    if x_api_key not in subscribers["api_keys"]:
        raise HTTPException(status_code=401, detail="Invalid API key")

    subscriber_id = subscribers["api_keys"][x_api_key]
    subscriber = subscribers["subscribers"].get(subscriber_id)

    if not subscriber:
        raise HTTPException(status_code=401, detail="Subscriber not found")

    # Check if subscription is active
    if subscriber.get("status") != "active":
        raise HTTPException(status_code=403, detail="Subscription not active")

    return subscriber

@app.get("/health")
def health():
    """Health check"""
    return {"status": "ok", "service": "trading_signals_api"}

@app.post("/subscribe")
def create_subscription(email: str, customer_name: str):
    """
    Initiate subscription: create Stripe checkout session
    Returns checkout URL for user
    """
    if not stripe.api_key or not STRIPE_PRICE_ID:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    try:
        # Create Stripe customer
        customer = stripe.Customer.create(email=email, name=customer_name)

        # Create checkout session
        session = stripe.checkout.Session.create(
            customer=customer.id,
            payment_method_types=["card"],
            line_items=[
                {
                    "price": STRIPE_PRICE_ID,
                    "quantity": 1,
                }
            ],
            mode="subscription",
            success_url="https://your-domain.com/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://your-domain.com/cancel",
        )

        # Store pending subscription
        subscribers = load_subscribers()
        subscriber_id = secrets.token_hex(16)
        subscribers["subscribers"][subscriber_id] = {
            "email": email,
            "name": customer_name,
            "stripe_customer_id": customer.id,
            "stripe_session_id": session.id,
            "status": "pending",
            "created_at": datetime.utcnow().isoformat(),
        }
        save_subscribers(subscribers)

        return {
            "checkout_url": session.url,
            "subscriber_id": subscriber_id,
            "session_id": session.id,
        }
    except stripe.error.StripeError as e:
        log.error(f"Stripe error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/webhook/stripe")
async def stripe_webhook(request):
    """Handle Stripe webhook events (subscription confirmation)"""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    if not STRIPE_WEBHOOK_SECRET:
        log.warning("Stripe webhook secret not configured")
        return {"status": "ok"}

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError as e:
        log.error(f"Invalid webhook: {e}")
        raise HTTPException(status_code=400, detail="Invalid webhook")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        log.info(f"✅ Subscription confirmed for {session.customer_email}")

        # Activate subscription
        subscribers = load_subscribers()
        for sub_id, sub in subscribers["subscribers"].items():
            if sub.get("stripe_customer_id") == session.customer:
                api_key = secrets.token_urlsafe(32)
                sub["status"] = "active"
                sub["stripe_subscription_id"] = session.subscription
                sub["api_key"] = api_key
                sub["activated_at"] = datetime.utcnow().isoformat()
                subscribers["api_keys"][api_key] = sub_id
                save_subscribers(subscribers)
                log.info(f"🔑 API key issued: {api_key[:16]}...")
                break

    elif event["type"] == "customer.subscription.deleted":
        subscription = event["data"]["object"]
        log.warning(f"Subscription cancelled: {subscription.customer}")

        # Deactivate subscription
        subscribers = load_subscribers()
        for sub_id, sub in subscribers["subscribers"].items():
            if sub.get("stripe_customer_id") == subscription.customer:
                sub["status"] = "cancelled"
                sub["cancelled_at"] = datetime.utcnow().isoformat()
                if "api_key" in sub:
                    subscribers["api_keys"].pop(sub["api_key"], None)
                save_subscribers(subscribers)
                break

    return {"status": "ok"}

@app.get("/signals")
def get_signals(subscriber = Depends(verify_api_key)):
    """Get current trading signals (real-time from prop_bot)"""
    signals = load_signals()

    return {
        "subscriber": subscriber["email"],
        "signals": {
            "profitable_days": signals["profitable_days"],
            "consecutive_profitable": len(signals["profitable_days"]),
            "daily_pnl": signals["daily_pnl"],
            "status": "ready_to_trade" if len(signals["profitable_days"]) >= 7 else "still_evaluating",
        },
        "timestamp": datetime.utcnow().isoformat(),
    }

@app.get("/subscriber/info")
def get_subscriber_info(subscriber = Depends(verify_api_key)):
    """Get current subscription info"""
    return {
        "email": subscriber["email"],
        "name": subscriber["name"],
        "status": subscriber["status"],
        "joined": subscriber.get("created_at"),
        "api_key": subscriber.get("api_key", "")[:16] + "...",
    }

@app.post("/admin/activate-subscriber")
def admin_activate(subscriber_id: str, admin_key: str):
    """Admin endpoint to manually activate subscriber (for testing/manual approvals)"""
    if admin_key != os.getenv("ADMIN_API_KEY", ""):
        raise HTTPException(status_code=401, detail="Invalid admin key")

    subscribers = load_subscribers()
    if subscriber_id not in subscribers["subscribers"]:
        raise HTTPException(status_code=404, detail="Subscriber not found")

    subscriber = subscribers["subscribers"][subscriber_id]
    api_key = secrets.token_urlsafe(32)
    subscriber["status"] = "active"
    subscriber["api_key"] = api_key
    subscriber["activated_at"] = datetime.utcnow().isoformat()
    subscribers["api_keys"][api_key] = subscriber_id
    save_subscribers(subscribers)

    log.info(f"✅ Manual activation: {subscriber['email']}")

    return {
        "subscriber_id": subscriber_id,
        "email": subscriber["email"],
        "api_key": api_key,
        "status": "active",
    }

@app.get("/admin/subscribers")
def list_subscribers(admin_key: str):
    """Admin endpoint to list all subscribers"""
    if admin_key != os.getenv("ADMIN_API_KEY", ""):
        raise HTTPException(status_code=401, detail="Invalid admin key")

    subscribers = load_subscribers()

    return {
        "total": len(subscribers["subscribers"]),
        "subscribers": [
            {
                "id": sub_id,
                "email": sub.get("email"),
                "status": sub.get("status"),
                "joined": sub.get("created_at"),
            }
            for sub_id, sub in subscribers["subscribers"].items()
        ],
    }

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("TRADING SIGNALS SUBSCRIPTION API")
    log.info(f"Stripe configured: {bool(stripe.api_key)}")
    log.info(f"Listening on port {PORT}")
    log.info("=" * 60)

    uvicorn.run(app, host="0.0.0.0", port=PORT)
