"""
WHITE-LABEL PLATFORM
====================
Agencies/educators resell video generation under their own brand.
Revenue share model: They charge customers, we take 20%.

PARTNER TIERS:
  Agency: $999/month → License + resell rights + support
  Enterprise: $4999/month → Full white-label + API + priority support
  Revenue Share: 20% of all customer subscriptions we process

REQUIRED ENV VARS:
  STATE_DIR, WHITE_LABEL_API_PORT (8004), STRIPE_SECRET_KEY
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
log = logging.getLogger("white_label")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

STATE_DIR = os.getenv("STATE_DIR", "/data/bot_state")
PARTNERS_FILE = os.path.join(STATE_DIR, "white_label_partners.json")
WHITE_LABEL_API_PORT = int(os.getenv("WHITE_LABEL_API_PORT", 8004))

app = FastAPI(title="White-Label Platform")

PARTNER_TIERS = {
    "agency": {
        "price": "$999",
        "features": [
            "White-label branding",
            "Custom domain support",
            "Up to 5 sub-customers",
            "Email support",
            "20% revenue share",
        ],
    },
    "enterprise": {
        "price": "$4999",
        "features": [
            "Full white-label platform",
            "Unlimited sub-customers",
            "Private API access",
            "Priority 24/7 support",
            "20% revenue share",
            "Custom integrations",
        ],
    },
}

def load_partners():
    """Load white-label partners"""
    try:
        with open(PARTNERS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"partners": {}, "partner_keys": {}}

def save_partners(data):
    """Save partners"""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(PARTNERS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def verify_partner_key(x_partner_key: str = Header(...)):
    """Verify partner and check license"""
    if not x_partner_key:
        raise HTTPException(status_code=401, detail="Partner key required")

    partners = load_partners()

    if x_partner_key not in partners["partner_keys"]:
        raise HTTPException(status_code=401, detail="Invalid partner key")

    partner_id = partners["partner_keys"][x_partner_key]
    partner = partners["partners"].get(partner_id)

    if not partner or partner.get("status") != "active":
        raise HTTPException(status_code=403, detail="License not active")

    return partner

@app.get("/health")
def health():
    return {"status": "ok", "service": "white_label_platform"}

@app.post("/apply")
def apply_for_partnership(
    company_name: str,
    contact_email: str,
    website: str,
    tier: str = "agency",
):
    """Apply to become a white-label partner"""
    if tier not in PARTNER_TIERS:
        raise HTTPException(status_code=400, detail="Invalid tier")

    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    partners = load_partners()
    partner_id = secrets.token_hex(16)

    partners["partners"][partner_id] = {
        "company_name": company_name,
        "email": contact_email,
        "website": website,
        "tier": tier,
        "status": "pending_approval",
        "applied_at": datetime.utcnow().isoformat(),
        "customers": [],
        "monthly_revenue": 0.0,
        "partner_earnings": 0.0,
    }
    save_partners(partners)

    log.info(f"📋 Partnership application: {company_name} ({tier})")

    return {
        "partner_id": partner_id,
        "status": "pending_approval",
        "tier": tier,
        "message": "Your application has been received. We'll review and contact you within 48 hours.",
        "next_steps": [
            "Review partnership agreement",
            "Set up custom branding",
            "Configure your domain",
            "Start selling to your customers",
        ],
    }

@app.post("/setup-branding")
def setup_white_label_branding(
    logo_url: str,
    primary_color: str,
    secondary_color: str,
    custom_domain: str,
    partner=Depends(verify_partner_key),
):
    """Configure white-label branding"""
    log.info(f"🎨 Branding setup: {partner['company_name']}")

    return {
        "status": "configured",
        "company_name": partner["company_name"],
        "logo": logo_url,
        "colors": {
            "primary": primary_color,
            "secondary": secondary_color,
        },
        "landing_page": f"https://{custom_domain}/video-generation",
        "dashboard": f"https://{custom_domain}/dashboard",
        "message": "Your white-label platform is ready! Your customers can now sign up.",
    }

@app.post("/add-customer")
def add_customer(
    customer_email: str,
    customer_name: str,
    customer_tier: str,
    partner=Depends(verify_partner_key),
):
    """Add/invite a customer for the partner to resell to"""
    # Partner creates customer in their own system, syncs to ours

    log.info(f"➕ Customer added by {partner['company_name']}: {customer_email}")

    # Return provisioning info for partner's system
    return {
        "customer_id": secrets.token_hex(16),
        "api_key": secrets.token_urlsafe(32),
        "status": "provisioned",
        "tier": customer_tier,
        "message": "Customer provisioned. Give them this API key.",
    }

@app.get("/customers")
def list_partner_customers(partner=Depends(verify_partner_key)):
    """List all customers the partner has created"""
    return {
        "partner": partner["company_name"],
        "total_customers": len(partner.get("customers", [])),
        "customers": partner.get("customers", []),
    }

@app.get("/earnings")
def get_partner_earnings(partner=Depends(verify_partner_key)):
    """Get partner's monthly earnings (20% revenue share)"""
    current_month = datetime.utcnow().strftime("%Y-%m")

    # Calculate: partner charges customers, we track and pay 20%
    total_customer_subscriptions = sum(
        c.get("price", 0) for c in partner.get("customers", [])
    )
    partner_earnings = total_customer_subscriptions * 0.20

    return {
        "partner": partner["company_name"],
        "tier": partner.get("tier"),
        "current_month": current_month,
        "total_customer_subscriptions": total_customer_subscriptions,
        "partner_revenue_share": partner_earnings,
        "billing_next_date": "2026-08-01",
        "lifetime_earnings": partner.get("partner_earnings", 0.0),
    }

@app.get("/api-keys")
def get_partner_api_keys(partner=Depends(verify_partner_key)):
    """Get API keys for partner's integration"""
    tier = partner.get("tier")

    if tier == "agency":
        raise HTTPException(status_code=403, detail="API access available in Enterprise tier")

    return {
        "tier": tier,
        "api_key": secrets.token_urlsafe(32),
        "api_endpoint": "https://api.propertygroupusa.com/white-label",
        "documentation": "https://docs.propertygroupusa.com/white-label-api",
        "rate_limit": "unlimited",
        "webhook_url": "https://your-domain.com/webhooks",
    }

@app.get("/dashboard")
def partner_dashboard(partner=Depends(verify_partner_key)):
    """Partner dashboard with KPIs"""
    tier = partner.get("tier")
    customers = partner.get("customers", [])

    total_customer_revenue = sum(c.get("price", 0) for c in customers)
    partner_revenue_20pct = total_customer_revenue * 0.20

    return {
        "company_name": partner["company_name"],
        "tier": tier,
        "status": partner["status"],
        "customers_count": len(customers),
        "total_customer_monthly_subscriptions": total_customer_revenue,
        "your_monthly_earnings_20pct": partner_revenue_20pct,
        "your_license_cost": PARTNER_TIERS[tier]["price"],
        "net_profit": partner_revenue_20pct - (999 if tier == "agency" else 4999),
        "features": PARTNER_TIERS[tier]["features"],
        "joined": partner.get("applied_at"),
    }

@app.get("/resources")
def get_partner_resources(partner=Depends(verify_partner_key)):
    """Marketing + support resources for partners"""
    return {
        "partner": partner["company_name"],
        "resources": {
            "landing_page_templates": [
                "minimal_white_label.html",
                "video_showcase.html",
                "pricing_comparison.html",
            ],
            "email_templates": [
                "welcome_email.txt",
                "feature_announcement.txt",
                "customer_success.txt",
            ],
            "documentation": [
                "API reference",
                "Setup guide",
                "FAQ",
                "Troubleshooting",
            ],
            "support": {
                "tier": partner.get("tier"),
                "response_time": "24 hours" if partner.get("tier") == "agency" else "1 hour",
                "slack_channel": "#partner-support",
                "email": "partners@propertygroupusa.com",
            },
        },
    }

@app.get("/admin/partners")
def list_all_partners(admin_key: str):
    """Admin: View all white-label partners"""
    if admin_key != os.getenv("ADMIN_API_KEY", ""):
        raise HTTPException(status_code=401, detail="Invalid admin key")

    partners = load_partners()

    total_partner_revenue = sum(
        sum(c.get("price", 0) for c in p.get("customers", [])) * 0.20
        for p in partners["partners"].values()
    )

    return {
        "total_partners": len(partners["partners"]),
        "total_partner_monthly_earnings": total_partner_revenue,
        "partners": [
            {
                "id": pid,
                "company": p.get("company_name"),
                "tier": p.get("tier"),
                "status": p.get("status"),
                "customers": len(p.get("customers", [])),
                "monthly_revenue": total_partner_revenue,
                "joined": p.get("applied_at"),
            }
            for pid, p in partners["partners"].items()
        ],
    }

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("WHITE-LABEL PLATFORM")
    log.info(f"Listening on port {WHITE_LABEL_API_PORT}")
    log.info("=" * 60)

    uvicorn.run(app, host="0.0.0.0", port=WHITE_LABEL_API_PORT)
