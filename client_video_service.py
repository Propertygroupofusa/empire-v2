"""
Client Video Service - Accept orders and generate custom videos
$500, $750, $1000 pricing tiers with automated fulfillment
"""

import os
import uuid
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from enum import Enum

import stripe
import aiohttp

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("client_video_service")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")


class PricingTier(str, Enum):
    """Pricing tiers for video services"""
    STANDARD = "standard"      # $500
    PROFESSIONAL = "pro"       # $750
    PREMIUM = "premium"        # $1000


PRICING = {
    PricingTier.STANDARD: {
        "amount": 50000,  # $500 in cents
        "duration_seconds": 60,
        "revisions": 1,
        "delivery_time": "24-48 hours",
        "features": ["Custom script", "AI voice", "Basic editing"]
    },
    PricingTier.PROFESSIONAL: {
        "amount": 75000,  # $750 in cents
        "duration_seconds": 90,
        "revisions": 3,
        "delivery_time": "12-24 hours",
        "features": ["Custom script", "AI voice", "Professional editing", "Music + effects"]
    },
    PricingTier.PREMIUM: {
        "amount": 100000,  # $1000 in cents
        "duration_seconds": 120,
        "revisions": 5,
        "delivery_time": "6-12 hours",
        "features": ["Custom script", "Multiple voice options", "Premium editing", "Music + effects", "Subtitles", "Unlimited revisions"]
    }
}


class VideoOrder:
    """Represents a video order"""

    def __init__(self, order_id: str, client_email: str, tier: PricingTier, script: str):
        self.order_id = order_id
        self.client_email = client_email
        self.tier = tier
        self.script = script
        self.status = "pending"  # pending, processing, ready, delivered
        self.created_at = datetime.utcnow()
        self.video_job_id = None
        self.download_link = None
        self.revisions_used = 0
        self.payment_id = None

    def to_dict(self) -> Dict:
        return {
            "order_id": self.order_id,
            "client_email": self.client_email,
            "tier": self.tier,
            "script": self.script,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "video_job_id": self.video_job_id,
            "download_link": self.download_link,
            "revisions_used": self.revisions_used,
            "max_revisions": PRICING[self.tier]["revisions"],
            "payment_id": self.payment_id
        }


class ClientVideoService:
    """Manage client video orders and fulfillment"""

    def __init__(self):
        self.orders: Dict[str, VideoOrder] = {}
        self.video_generator_url = os.getenv("VIDEO_GENERATOR_URL", "http://localhost:5003")
        self.stripe_enabled = bool(os.getenv("STRIPE_SECRET_KEY"))

    async def create_order(self, client_email: str, tier: PricingTier, script: str) -> Dict:
        """Create a new video order"""
        try:
            order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"
            order = VideoOrder(order_id, client_email, tier, script)

            # Process payment
            if self.stripe_enabled:
                payment = await self._process_payment(
                    client_email,
                    order_id,
                    tier
                )
                if not payment.get("success"):
                    return payment

                order.payment_id = payment.get("payment_id")

            # Queue video generation
            result = await self._queue_video_generation(order)
            if not result.get("success"):
                return result

            order.video_job_id = result.get("job_id")
            order.status = "processing"

            # Store order
            self.orders[order_id] = order

            log.info(f"Order created: {order_id} ({tier}) for {client_email}")

            return {
                "success": True,
                "order_id": order_id,
                "status": "processing",
                "delivery_time": PRICING[tier]["delivery_time"],
                "message": f"Your {tier} video has been queued for generation"
            }
        except Exception as e:
            log.error(f"Failed to create order: {e}")
            return {"success": False, "error": str(e)}

    async def _process_payment(self, client_email: str, order_id: str, tier: PricingTier) -> Dict:
        """Process payment via Stripe"""
        try:
            pricing = PRICING[tier]

            # Create payment intent
            intent = stripe.PaymentIntent.create(
                amount=pricing["amount"],
                currency="usd",
                metadata={
                    "order_id": order_id,
                    "tier": tier,
                    "client_email": client_email
                },
                description=f"Custom video service - {tier} tier"
            )

            return {
                "success": True,
                "payment_id": intent.id,
                "client_secret": intent.client_secret,
                "message": "Payment processing initiated"
            }
        except stripe.error.StripeError as e:
            log.error(f"Stripe payment failed: {e}")
            return {"success": False, "error": f"Payment failed: {str(e)}"}

    async def _queue_video_generation(self, order: VideoOrder) -> Dict:
        """Send video to generator"""
        try:
            pricing = PRICING[order.tier]

            async with aiohttp.ClientSession() as session:
                payload = {
                    "text": order.script,
                    "videoType": "client_custom",
                    "quality": "1080p" if order.tier in [PricingTier.PROFESSIONAL, PricingTier.PREMIUM] else "720p",
                    "metadata": {
                        "order_id": order.order_id,
                        "client_email": order.client_email,
                        "tier": order.tier
                    }
                }

                async with session.post(
                    f"{self.video_generator_url}/api/video-gen/generate",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=300)
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        return {"success": True, "job_id": result.get("jobId")}
                    else:
                        return {"success": False, "error": f"HTTP {response.status}"}
        except Exception as e:
            log.error(f"Failed to queue video: {e}")
            return {"success": False, "error": str(e)}

    async def check_order_status(self, order_id: str) -> Dict:
        """Check status of an order"""
        if order_id not in self.orders:
            return {"error": "Order not found"}

        order = self.orders[order_id]

        # Check video generation status
        if order.video_job_id:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{self.video_generator_url}/api/video-gen/status/{order.video_job_id}"
                    ) as response:
                        if response.status == 200:
                            job_status = await response.json()

                            # Update order status based on job status
                            if job_status.get("status") == "completed":
                                order.status = "ready"
                                order.download_link = job_status.get("download_url")
                            elif job_status.get("status") == "processing":
                                order.status = "processing"
            except Exception as e:
                log.warning(f"Failed to check job status: {e}")

        return {
            "order_id": order_id,
            "status": order.status,
            "tier": order.tier,
            "created_at": order.created_at.isoformat(),
            "video_job_id": order.video_job_id,
            "download_link": order.download_link,
            "revisions_used": order.revisions_used,
            "max_revisions": PRICING[order.tier]["revisions"],
            "message": f"Your video is {order.status}"
        }

    async def request_revision(self, order_id: str, revision_script: str) -> Dict:
        """Request revision to video"""
        if order_id not in self.orders:
            return {"error": "Order not found"}

        order = self.orders[order_id]
        pricing = PRICING[order.tier]

        if order.revisions_used >= pricing["revisions"]:
            return {
                "error": f"Maximum revisions ({pricing['revisions']}) reached for {order.tier} tier"
            }

        # Queue new video generation with revised script
        order.script = revision_script
        result = await self._queue_video_generation(order)

        if result.get("success"):
            order.video_job_id = result.get("job_id")
            order.status = "processing"
            order.revisions_used += 1

            return {
                "success": True,
                "order_id": order_id,
                "revisions_used": order.revisions_used,
                "revisions_remaining": pricing["revisions"] - order.revisions_used,
                "message": f"Revision queued. {pricing['revisions'] - order.revisions_used} revisions remaining"
            }
        else:
            return {"success": False, "error": result.get("error")}

    def get_orders(self, client_email: Optional[str] = None, status: Optional[str] = None) -> List[Dict]:
        """Get orders, optionally filtered"""
        orders = list(self.orders.values())

        if client_email:
            orders = [o for o in orders if o.client_email == client_email]

        if status:
            orders = [o for o in orders if o.status == status]

        return [o.to_dict() for o in orders]

    def get_pricing(self) -> Dict:
        """Get pricing information"""
        return {
            "tiers": {
                tier: {
                    "price": pricing["amount"] / 100,  # Convert to dollars
                    "duration": pricing["duration_seconds"],
                    "revisions": pricing["revisions"],
                    "delivery_time": pricing["delivery_time"],
                    "features": pricing["features"]
                }
                for tier, pricing in PRICING.items()
            },
            "currency": "USD"
        }

    def get_statistics(self) -> Dict:
        """Get service statistics"""
        total_orders = len(self.orders)
        completed = sum(1 for o in self.orders.values() if o.status == "delivered")
        processing = sum(1 for o in self.orders.values() if o.status == "processing")

        total_revenue = sum(
            PRICING[o.tier]["amount"] / 100
            for o in self.orders.values()
            if o.status == "delivered"
        )

        return {
            "total_orders": total_orders,
            "completed_orders": completed,
            "processing_orders": processing,
            "total_revenue": total_revenue,
            "avg_order_value": total_revenue / completed if completed > 0 else 0
        }


# Global instance
service = ClientVideoService()


def get_service():
    """Get service instance"""
    return service
