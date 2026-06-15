"""
SYNTHESIA AI VIDEO REVENUE BOT
================================
Auto-generates AI avatar videos for clients across all verticals.
Revenue streams: Property listings, onboarding videos, social content packages.
"""

import os
import logging
import httpx
import hmac
import hashlib

log = logging.getLogger("synthesia")

SYNTHESIA_KEY = os.getenv("SYNTHESIA_API_KEY", "")
WEBHOOK_SECRET = os.getenv("SYNTHESIA_WEBHOOK_SECRET", "")

# Avatar presets by use case
AVATARS = {
    "real_estate": "11af1a93-e679-41a6-9b21-4cd41d73c940",  # Hudson
    "finance":     "e49ecfaf-1d39-4561-8355-29ebf8b71a4f",  # Olivia
    "marketing":   "cf0eda7e-8f3c-43de-ae08-712e242ead61",  # Alisha
    "sales":       "72da6c7c-36b6-4824-816b-380ac2058d86",  # Mason
}


async def create_video(script: str, use_case: str = "marketing",
                        title: str = "Generated Video",
                        callback_id: str = "") -> dict:
    """
    Generate an AI avatar video from a text script.
    use_case options: real_estate, finance, marketing, sales
    """
    avatar_id = AVATARS.get(use_case, AVATARS["marketing"])

    payload = {
        "test": False,
        "title": title,
        "description": f"Auto-generated {use_case} video",
        "visibility": "private",
        "callbackId": callback_id,
        "input": [
            {
                "scriptText": script,
                "avatar": avatar_id,
                "background": "off_white",
            }
        ],
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.synthesia.io/v2/videos",
            headers={
                "Authorization": SYNTHESIA_KEY,
                "Content-Type": "application/json",
            },
            json=payload,
        )
        data = r.json()
        log.info(f"Video created | ID: {data.get('id')} | Use case: {use_case}")
        return data


async def get_video(video_id: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"https://api.synthesia.io/v2/videos/{video_id}",
            headers={"Authorization": SYNTHESIA_KEY},
        )
        return r.json()


def verify_webhook(timestamp: str, body: str, signature: str) -> bool:
    message = f"{timestamp}.{body}"
    computed = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, signature)


# ── Revenue-generating script templates ─────────────────────

def property_listing_script(address: str, price: str, beds: int,
                              baths: int, features: str) -> str:
    return (
        f"Welcome to {address}! This stunning property is listed at {price}. "
        f"With {beds} bedrooms and {baths} bathrooms, this home offers {features}. "
        f"Don't miss out - schedule your private tour today by contacting "
        f"Property Group of USA."
    )


def payee_trust_onboarding_script(user_name: str = "there") -> str:
    return (
        f"Hi {user_name}, welcome to Payee Trust! Where we trust you to do "
        f"the right thing with your money. Let's walk through your account "
        f"setup - from your spending power dashboard to your rewards program. "
        f"You're in good hands."
    )


def social_content_script(business_name: str, topic: str, cta: str) -> str:
    return (
        f"Hey everyone, it's your friends at {business_name}! Today we're "
        f"talking about {topic}. {cta} Follow us for more tips like this!"
    )


def cold_call_followup_script(lead_name: str, property_address: str) -> str:
    return (
        f"Hi {lead_name}, thanks for speaking with us about {property_address}. "
        f"We wanted to follow up with a personalized video. We're ready to make "
        f"this process as smooth as possible for you. Reach out anytime - "
        f"we're here to help."
    )


if __name__ == "__main__":
    script = property_listing_script(
        address="1234 Main St, San Antonio TX",
        price="$285,000",
        beds=3,
        baths=2,
        features="a renovated kitchen, large backyard, and two-car garage"
    )
    print("Sample script:")
    print(script)
    print("\nReady to call create_video() with SYNTHESIA_API_KEY set.")
