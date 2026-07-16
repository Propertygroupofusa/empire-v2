"""
Revenue Automation API - Unified endpoints for all revenue streams
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks, Request
from typing import Optional
import logging
import os
import stripe

log = logging.getLogger("revenue_automation")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

router = APIRouter()

# Import dependencies gracefully
get_publisher = None
start_daily_publisher = None
try:
    from daily_publisher import get_publisher, start_daily_publisher
except Exception as e:
    log.warning(f"Failed to import daily_publisher: {e}")

get_tracker = None
try:
    from youtube_monetization import get_tracker
except Exception as e:
    log.warning(f"Failed to import youtube_monetization: {e}")

get_video_service = None
PricingTier = None
try:
    from client_video_service import get_service as get_video_service, PricingTier
except Exception as e:
    log.warning(f"Failed to import client_video_service: {e}")

get_lead_generator = None
LeadSource = None
LeadStatus = None
try:
    from lead_generator import (
        get_generator as get_lead_generator,
        LeadSource, LeadStatus
    )
except Exception as e:
    log.warning(f"Failed to import lead_generator: {e}")

get_course_builder = None
CourseLevel = None
try:
    from course_builder import get_builder as get_course_builder, CourseLevel
except Exception as e:
    log.warning(f"Failed to import course_builder: {e}")

get_dashboard = None
try:
    from revenue_dashboard import get_dashboard
except Exception as e:
    log.warning(f"Failed to import revenue_dashboard: {e}")

get_autoposter = None
try:
    from social_media_autoposter import get_autoposter
except Exception as e:
    log.warning(f"Failed to import social_media_autoposter: {e}")


# ── DAILY PUBLISHING ENDPOINTS ────────────────────────────────────────

@router.get("/publishing/status")
async def get_publishing_status():
    """Get daily publishing schedule and status"""
    publisher = get_publisher()
    return publisher.get_schedule_status()


@router.post("/publishing/start")
async def start_publishing():
    """Start the daily publishing service"""
    if start_daily_publisher():
        return {"success": True, "message": "Daily publisher started"}
    return {"success": False, "message": "Daily publisher already running or disabled"}


@router.post("/publishing/generate-now")
async def generate_video_now(background_tasks: BackgroundTasks):
    """Generate a video immediately (not waiting for schedule)"""
    publisher = get_publisher()
    background_tasks.add_task(publisher.generate_daily_video)
    return {"success": True, "message": "Video generation queued"}


# ── YOUTUBE MONETIZATION ENDPOINTS ────────────────────────────────────

@router.get("/youtube/metrics/channel")
async def get_channel_metrics():
    """Get YouTube channel metrics"""
    tracker = get_tracker()
    return tracker.get_channel_metrics()


@router.get("/youtube/metrics/analytics")
async def get_youtube_analytics(days_back: int = 30):
    """Get YouTube analytics for specified period"""
    tracker = get_tracker()
    return tracker.get_daily_analytics(days_back=days_back)


@router.get("/youtube/metrics/projections")
async def get_earnings_projections():
    """Get monthly and annual earnings projections"""
    tracker = get_tracker()
    return tracker.calculate_monthly_projections()


@router.get("/youtube/videos/top")
async def get_top_videos(limit: int = 10):
    """Get top performing videos"""
    tracker = get_tracker()
    return tracker.get_top_videos(limit=limit)


@router.get("/youtube/summary")
async def get_youtube_summary():
    """Get complete YouTube revenue summary"""
    tracker = get_tracker()
    return tracker.get_revenue_summary()


# ── CLIENT VIDEO SERVICE ENDPOINTS ────────────────────────────────────

@router.get("/video-service/pricing")
async def get_video_pricing():
    """Get pricing for video service"""
    service = get_video_service()
    return service.get_pricing()


@router.post("/video-service/order")
async def create_video_order(
    client_email: str,
    script: str,
    tier: PricingTier = PricingTier.STANDARD
):
    """Create a custom video order"""
    service = get_video_service()
    return await service.create_order(client_email, tier, script)


@router.get("/video-service/order/{order_id}")
async def get_order_status(order_id: str):
    """Check order status"""
    service = get_video_service()
    return await service.check_order_status(order_id)


@router.post("/video-service/order/{order_id}/revision")
async def request_revision(order_id: str, revised_script: str):
    """Request revision to an order"""
    service = get_video_service()
    return await service.request_revision(order_id, revised_script)


@router.get("/video-service/orders")
async def get_orders(client_email: Optional[str] = None, status: Optional[str] = None):
    """Get orders, optionally filtered"""
    service = get_video_service()
    return service.get_orders(client_email=client_email, status=status)


@router.get("/video-service/stats")
async def get_service_stats():
    """Get video service statistics"""
    service = get_video_service()
    return service.get_statistics()


@router.post("/video-service/webhook/stripe")
async def video_service_stripe_webhook(request: Request):
    """Stripe webhook for client_video_service Checkout Sessions.
    Video generation only starts here, once payment is actually confirmed."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    # Falls back to the shared STRIPE_WEBHOOK_SECRET so this keeps working if
    # only one Stripe webhook endpoint is registered; set the dedicated var
    # once a separate endpoint (with its own signing secret) exists for this path.
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET_VIDEO_SERVICE") or os.getenv("STRIPE_WEBHOOK_SECRET")

    if not webhook_secret:
        log.warning("STRIPE_WEBHOOK_SECRET_VIDEO_SERVICE (or STRIPE_WEBHOOK_SECRET) not configured, cannot verify webhook")
        raise HTTPException(status_code=500, detail="Webhook not configured")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        order_id = session.get("metadata", {}).get("order_id")
        if order_id:
            service = get_video_service()
            await service.confirm_payment_and_fulfill(order_id, session["id"])

    return {"status": "success"}


# ── LEAD GENERATION ENDPOINTS ─────────────────────────────────────────

@router.post("/leads/create")
async def create_lead(
    name: str,
    email: str,
    source: LeadSource,
    source_detail: Optional[str] = None,
    phone: Optional[str] = None,
    company: Optional[str] = None,
    tags: Optional[list] = None
):
    """Create a new lead"""
    generator = get_lead_generator()
    return generator.create_lead(
        name, email, source, source_detail,
        phone=phone, company=company, tags=tags
    )


@router.get("/leads/{lead_id}")
async def get_lead(lead_id: str):
    """Get a single lead"""
    generator = get_lead_generator()
    lead = generator.get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


@router.get("/leads")
async def get_leads(
    status: Optional[LeadStatus] = None,
    source: Optional[LeadSource] = None,
    min_score: int = 0
):
    """Get leads with optional filtering"""
    generator = get_lead_generator()
    return generator.get_leads(status=status, source=source, min_score=min_score)


@router.put("/leads/{lead_id}/status")
async def update_lead_status(
    lead_id: str,
    status: LeadStatus,
    notes: Optional[str] = None
):
    """Update lead status"""
    generator = get_lead_generator()
    return generator.update_lead_status(lead_id, status, notes)


@router.post("/leads/{lead_id}/engagement")
async def log_engagement(
    lead_id: str,
    action: str,
    points: int = 10
):
    """Log engagement activity"""
    generator = get_lead_generator()
    return generator.log_engagement(lead_id, action, points)


@router.post("/leads/{lead_id}/convert")
async def convert_lead(
    lead_id: str,
    value: float,
    conversion_type: str
):
    """Mark lead as converted"""
    generator = get_lead_generator()
    return generator.convert_lead(lead_id, value, conversion_type)


@router.get("/leads/metrics/summary")
async def get_lead_metrics():
    """Get lead generation metrics"""
    generator = get_lead_generator()
    return generator.get_lead_generation_metrics()


@router.get("/leads/hot")
async def get_hot_leads(limit: int = 10):
    """Get highest-scoring leads ready for outreach"""
    generator = get_lead_generator()
    return generator.get_hot_leads(limit=limit)


@router.get("/leads/cta/youtube-description")
async def get_youtube_cta(video_title: str):
    """Generate YouTube description CTA for lead capture"""
    generator = get_lead_generator()
    return {"description": generator.create_youtube_description_cta(video_title)}


@router.get("/leads/forms/landing-page")
async def get_lead_form():
    """Get lead capture form template"""
    generator = get_lead_generator()
    return generator.create_landing_page_form()


# ── COURSE BUILDER ENDPOINTS ──────────────────────────────────────────

@router.post("/courses/create")
async def create_course(
    title: str,
    description: str,
    level: CourseLevel,
    price: float
):
    """Create a new course"""
    builder = get_course_builder()
    return builder.create_course(title, description, level, price)


@router.get("/courses")
async def get_courses(published_only: bool = True):
    """Get all courses"""
    builder = get_course_builder()
    return builder.get_courses(published_only=published_only)


@router.get("/courses/catalog")
async def get_course_catalog():
    """Get public course catalog"""
    builder = get_course_builder()
    return builder.get_course_catalog()


@router.post("/courses/{course_id}/module")
async def add_module(
    course_id: str,
    title: str,
    description: str,
    order: Optional[int] = None
):
    """Add a module to a course"""
    builder = get_course_builder()
    if course_id not in builder.courses:
        raise HTTPException(status_code=404, detail="Course not found")

    course = builder.courses[course_id]
    module_id = course.add_module(title, description, order)

    return {"success": True, "module_id": module_id}


@router.post("/courses/{course_id}/module/{module_id}/lesson")
async def add_lesson(
    course_id: str,
    module_id: str,
    video_id: str,
    title: str,
    duration: int,
    content: str
):
    """Add a lesson to a module"""
    builder = get_course_builder()
    if course_id not in builder.courses:
        raise HTTPException(status_code=404, detail="Course not found")

    course = builder.courses[course_id]
    if module_id not in course.modules:
        raise HTTPException(status_code=404, detail="Module not found")

    module = course.modules[module_id]
    lesson_id = module.add_lesson(video_id, title, duration, content)

    return {"success": True, "lesson_id": lesson_id}


@router.post("/courses/{course_id}/publish")
async def publish_course(course_id: str):
    """Publish a course"""
    builder = get_course_builder()
    if course_id not in builder.courses:
        raise HTTPException(status_code=404, detail="Course not found")

    course = builder.courses[course_id]
    if course.publish():
        return {"success": True, "message": f"Course '{course.title}' published"}
    else:
        return {"success": False, "error": "Course has missing modules or lessons"}


@router.post("/courses/{course_id}/enroll")
async def enroll_student(
    course_id: str,
    student_id: str,
    student_email: str
):
    """Enroll a student in a course"""
    builder = get_course_builder()
    return await builder.enroll_student(student_id, student_email, course_id)


@router.get("/courses/{course_id}/progress/{student_id}")
async def get_course_progress(course_id: str, student_id: str):
    """Get student progress in course"""
    builder = get_course_builder()
    progress = builder.get_student_progress(student_id, course_id)
    if not progress:
        raise HTTPException(status_code=404, detail="Student not enrolled or course not found")
    return progress


@router.post("/courses/{course_id}/progress/{student_id}/lesson/{lesson_id}")
async def mark_lesson_complete(
    course_id: str,
    student_id: str,
    lesson_id: str
):
    """Mark a lesson as completed"""
    builder = get_course_builder()
    return await builder.mark_lesson_complete(student_id, course_id, lesson_id)


@router.get("/courses/{course_id}/stats")
async def get_course_stats(course_id: str):
    """Get course statistics"""
    builder = get_course_builder()
    return builder.get_course_stats(course_id)


# ── REVENUE DASHBOARD ENDPOINTS ───────────────────────────────────────

@router.get("/dashboard/all-metrics")
async def get_all_metrics():
    """Get all metrics across all revenue streams"""
    dashboard = get_dashboard()
    return dashboard.get_all_metrics()


@router.get("/dashboard/revenue-summary")
async def get_revenue_summary():
    """Get unified revenue summary"""
    dashboard = get_dashboard()
    return dashboard.get_revenue_summary()


@router.get("/dashboard/executive-summary")
async def get_executive_summary():
    """Get executive summary with key metrics"""
    dashboard = get_dashboard()
    return dashboard.get_executive_summary()


@router.get("/dashboard/roi-analysis")
async def get_roi_analysis():
    """Get ROI analysis"""
    dashboard = get_dashboard()
    return dashboard.get_roi_analysis()


@router.get("/dashboard/growth-forecast")
async def get_growth_forecast():
    """Get 90-day growth forecast"""
    dashboard = get_dashboard()
    return dashboard.get_growth_forecast()


# ── SOCIAL MEDIA AUTOPOSTER ENDPOINTS ─────────────────────────────────

@router.get("/social/status")
async def get_social_status():
    """Get social media autoposter status"""
    autoposter = get_autoposter()
    return autoposter.get_status()


@router.get("/social/platforms")
async def get_enabled_platforms():
    """Get list of enabled social platforms"""
    autoposter = get_autoposter()
    return {
        "enabled_platforms": autoposter.get_enabled_platforms(),
        "total": len(autoposter.get_enabled_platforms())
    }


@router.post("/social/autopost")
async def autopost_video(
    video_url: str,
    title: str,
    video_type: str = "trading"
):
    """Auto-post video to all enabled social media platforms"""
    autoposter = get_autoposter()
    return await autoposter.autoposts_video(video_url, title, video_type)
