"""
Client Video Service - Accept orders and generate custom videos
$500, $750, $1000 pricing tiers with automated fulfillment
"""

import os
import uuid
import logging
from typing import Optional, Dict, List
from enum import Enum

import stripe
import aiohttp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import ClientVideoOrder
from payments_pause import payments_paused, PAUSE_MESSAGE

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


class ClientVideoService:
    """Manage client video orders and fulfillment"""

    def __init__(self):
        self.video_generator_url = os.getenv("VIDEO_GENERATOR_URL", "http://localhost:5003")
        self.stripe_enabled = bool(os.getenv("STRIPE_SECRET_KEY"))

    async def create_order(self, db: AsyncSession, client_email: str, tier: PricingTier, script: str) -> Dict:
        """Create a new video order. Video generation is NOT queued here —
        it only starts once Stripe confirms payment via webhook
        (see confirm_payment_and_fulfill), otherwise anyone could call this
        endpoint and get a paid video for free."""
        try:
            if not self.stripe_enabled:
                return {"success": False, "error": "Payments are not configured for this service"}

            order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"

            checkout = await self._create_checkout_session(client_email, order_id, tier)
            if not checkout.get("success"):
                return checkout

            order = ClientVideoOrder(
                order_id=order_id,
                client_email=client_email,
                tier=tier.value,
                script=script,
                status="awaiting_payment",
            )
            db.add(order)
            await db.commit()

            log.info(f"Order created: {order_id} ({tier}) for {client_email}, awaiting payment")

            return {
                "success": True,
                "order_id": order_id,
                "status": "awaiting_payment",
                "checkout_url": checkout.get("checkout_url"),
                "message": f"Complete payment to start your {tier.value} video"
            }
        except Exception as e:
            log.error(f"Failed to create order: {e}")
            return {"success": False, "error": str(e)}

    async def _create_checkout_session(self, client_email: str, order_id: str, tier: PricingTier) -> Dict:
        """Create a Stripe-hosted Checkout Session for the order"""
        if payments_paused():
            log.warning(f"Payments paused (PAYMENTS_PAUSED=true) - refusing checkout for order {order_id}")
            return {"success": False, "error": PAUSE_MESSAGE}

        try:
            pricing = PRICING[tier]
            base_url = os.getenv("PUBLIC_BASE_URL", "https://empire-v2-production.up.railway.app")

            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "product_data": {"name": f"Custom video service - {tier} tier"},
                        "unit_amount": pricing["amount"],
                    },
                    "quantity": 1,
                }],
                mode="payment",
                customer_email=client_email,
                success_url=f"{base_url}/revenue/video-service/order/{order_id}",
                cancel_url=f"{base_url}/revenue/video-service/pricing",
                metadata={"order_id": order_id, "tier": tier, "client_email": client_email},
            )

            return {"success": True, "checkout_url": session.url, "session_id": session.id}
        except stripe.error.StripeError as e:
            log.error(f"Stripe checkout session creation failed: {e}")
            return {"success": False, "error": f"Payment setup failed: {str(e)}"}

    async def _get_order(self, db: AsyncSession, order_id: str) -> Optional[ClientVideoOrder]:
        result = await db.execute(select(ClientVideoOrder).where(ClientVideoOrder.order_id == order_id))
        return result.scalar_one_or_none()

    async def confirm_payment_and_fulfill(self, db: AsyncSession, order_id: str, session_id: str) -> Dict:
        """Called by the Stripe webhook once checkout.session.completed fires.
        This is the only path that triggers video generation."""
        order = await self._get_order(db, order_id)
        if not order:
            log.warning(f"Stripe webhook confirmed unknown order_id: {order_id}")
            return {"success": False, "error": "Order not found"}

        if order.payment_id:
            log.info(f"Order {order_id} already fulfilled, ignoring duplicate webhook")
            return {"success": True, "order_id": order_id, "status": order.status}

        order.payment_id = session_id

        result = await self._queue_video_generation(order)
        if not result.get("success"):
            order.status = "payment_received_generation_failed"
            await db.commit()
            log.error(f"Video generation failed after payment for order {order_id}: {result.get('error')}")
            return result

        order.video_job_id = result.get("job_id")
        order.status = "processing"
        await db.commit()
        log.info(f"Payment confirmed for order {order_id}, video generation queued")
        return {"success": True, "order_id": order_id, "status": "processing"}

    async def _queue_video_generation(self, order: ClientVideoOrder) -> Dict:
        """Send video to generator"""
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "text": order.script,
                    "videoType": "client_custom",
                    "quality": "1080p" if order.tier in [PricingTier.PROFESSIONAL.value, PricingTier.PREMIUM.value] else "720p",
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

    async def check_order_status(self, db: AsyncSession, order_id: str) -> Dict:
        """Check status of an order"""
        order = await self._get_order(db, order_id)
        if not order:
            return {"error": "Order not found"}

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
                            await db.commit()
            except Exception as e:
                log.warning(f"Failed to check job status: {e}")

        pricing = PRICING[PricingTier(order.tier)]
        return {
            "order_id": order.order_id,
            "status": order.status,
            "tier": order.tier,
            "created_at": order.created_at.isoformat() if order.created_at else None,
            "video_job_id": order.video_job_id,
            "download_link": order.download_link,
            "revisions_used": order.revisions_used,
            "max_revisions": pricing["revisions"],
            "message": f"Your video is {order.status}"
        }

    async def request_revision(self, db: AsyncSession, order_id: str, revision_script: str) -> Dict:
        """Request revision to video"""
        order = await self._get_order(db, order_id)
        if not order:
            return {"error": "Order not found"}

        pricing = PRICING[PricingTier(order.tier)]

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
            await db.commit()

            return {
                "success": True,
                "order_id": order_id,
                "revisions_used": order.revisions_used,
                "revisions_remaining": pricing["revisions"] - order.revisions_used,
                "message": f"Revision queued. {pricing['revisions'] - order.revisions_used} revisions remaining"
            }
        else:
            return {"success": False, "error": result.get("error")}

    async def get_orders(self, db: AsyncSession, client_email: Optional[str] = None, status: Optional[str] = None) -> List[Dict]:
        """Get orders, optionally filtered"""
        query = select(ClientVideoOrder)
        if client_email:
            query = query.where(ClientVideoOrder.client_email == client_email)
        if status:
            query = query.where(ClientVideoOrder.status == status)

        result = await db.execute(query)
        orders = result.scalars().all()

        return [
            o.to_dict(max_revisions=PRICING[PricingTier(o.tier)]["revisions"])
            for o in orders
        ]

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

    async def get_statistics(self, db: AsyncSession) -> Dict:
        """Get service statistics"""
        result = await db.execute(select(ClientVideoOrder))
        orders = result.scalars().all()

        total_orders = len(orders)
        completed = sum(1 for o in orders if o.status == "delivered")
        processing = sum(1 for o in orders if o.status == "processing")

        total_revenue = sum(
            PRICING[PricingTier(o.tier)]["amount"] / 100
            for o in orders
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
