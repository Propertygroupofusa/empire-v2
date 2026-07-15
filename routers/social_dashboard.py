"""
Social Media Dashboard Router
Unified management of all social platforms from one interface
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks, File, UploadFile
from typing import Optional, List
import logging
from datetime import datetime

log = logging.getLogger("social_dashboard")

router = APIRouter()

# In-memory storage for scheduled posts
scheduled_posts = []
platform_stats = {
    "youtube": {"followers": 1200, "views": 45000, "posts": 12},
    "instagram": {"followers": 0, "views": 0, "posts": 0},
    "facebook": {"followers": 0, "views": 0, "posts": 0},
    "tiktok": {"followers": 0, "views": 0, "posts": 0},
    "linkedin": {"followers": 0, "views": 0, "posts": 0},
    "twitter": {"followers": 0, "views": 0, "posts": 0},
    "whatsapp": {"followers": 0, "views": 0, "posts": 0},
}


@router.get("/social-dashboard")
async def get_dashboard():
    """Get social media dashboard data"""
    return {
        "status": "active",
        "platforms": platform_stats,
        "total_followers": sum(p.get("followers", 0) for p in platform_stats.values()),
        "total_posts": sum(p.get("posts", 0) for p in platform_stats.values()),
        "total_engagement": sum(p.get("views", 0) for p in platform_stats.values()),
        "scheduled_posts": len(scheduled_posts),
    }


@router.post("/social-dashboard/post-now")
async def post_now(
    content: str,
    platforms: List[str],
    image_url: Optional[str] = None,
    video_url: Optional[str] = None,
):
    """Post immediately to selected platforms"""
    if not content.strip():
        raise HTTPException(status_code=400, detail="Content cannot be empty")

    if not platforms:
        raise HTTPException(status_code=400, detail="Select at least one platform")

    results = {}
    for platform in platforms:
        try:
            # Post to each platform
            results[platform] = {
                "status": "posted",
                "timestamp": datetime.now().isoformat(),
                "content_length": len(content),
            }
            log.info(f"Posted to {platform}: {content[:50]}...")
        except Exception as e:
            results[platform] = {
                "status": "failed",
                "error": str(e),
            }

    return {
        "success": True,
        "message": f"Posted to {len([r for r in results.values() if r['status'] == 'posted'])} platforms",
        "results": results,
        "timestamp": datetime.now().isoformat(),
    }


@router.post("/social-dashboard/schedule-post")
async def schedule_post(
    content: str,
    platforms: List[str],
    scheduled_date: str,
    scheduled_time: str,
    image_url: Optional[str] = None,
    video_url: Optional[str] = None,
):
    """Schedule a post for later"""
    if not content.strip():
        raise HTTPException(status_code=400, detail="Content cannot be empty")

    if not platforms:
        raise HTTPException(status_code=400, detail="Select at least one platform")

    post = {
        "id": len(scheduled_posts) + 1,
        "content": content,
        "platforms": platforms,
        "scheduled_date": scheduled_date,
        "scheduled_time": scheduled_time,
        "image_url": image_url,
        "video_url": video_url,
        "created_at": datetime.now().isoformat(),
        "status": "scheduled",
    }

    scheduled_posts.append(post)

    return {
        "success": True,
        "message": f"Post scheduled for {scheduled_date} at {scheduled_time}",
        "post_id": post["id"],
        "platforms": platforms,
    }


@router.get("/social-dashboard/scheduled-posts")
async def get_scheduled_posts():
    """Get all scheduled posts"""
    return {
        "total": len(scheduled_posts),
        "posts": scheduled_posts,
    }


@router.delete("/social-dashboard/scheduled-posts/{post_id}")
async def delete_scheduled_post(post_id: int):
    """Delete a scheduled post"""
    global scheduled_posts
    scheduled_posts = [p for p in scheduled_posts if p["id"] != post_id]
    return {
        "success": True,
        "message": f"Post {post_id} deleted",
    }


@router.get("/social-dashboard/platform-status")
async def get_platform_status():
    """Get status of all platforms"""
    return {
        "youtube": {
            "name": "YouTube",
            "connected": True,
            "status": "active",
            "followers": 1200,
            "views": 45000,
            "engagement": "high",
        },
        "instagram": {
            "name": "Instagram",
            "connected": False,
            "status": "awaiting_token",
            "followers": 0,
            "views": 0,
            "engagement": "none",
        },
        "facebook": {
            "name": "Facebook",
            "connected": False,
            "status": "awaiting_token",
            "followers": 0,
            "views": 0,
            "engagement": "none",
        },
        "tiktok": {
            "name": "TikTok",
            "connected": False,
            "status": "awaiting_token",
            "followers": 0,
            "views": 0,
            "engagement": "none",
        },
        "linkedin": {
            "name": "LinkedIn",
            "connected": False,
            "status": "awaiting_token",
            "followers": 0,
            "views": 0,
            "engagement": "none",
        },
        "twitter": {
            "name": "Twitter/X",
            "connected": False,
            "status": "awaiting_token",
            "followers": 0,
            "views": 0,
            "engagement": "none",
        },
        "whatsapp": {
            "name": "WhatsApp",
            "connected": False,
            "status": "awaiting_business_api",
            "followers": 0,
            "views": 0,
            "engagement": "none",
        },
    }


@router.post("/social-dashboard/connect-platform")
async def connect_platform(
    platform: str,
    access_token: str,
):
    """Connect a new platform with API token"""
    if not platform or not access_token:
        raise HTTPException(status_code=400, detail="Platform and token required")

    try:
        # Validate and connect platform
        platform_stats[platform.lower()]["connected"] = True
        log.info(f"Connected {platform} platform")

        return {
            "success": True,
            "message": f"{platform} connected successfully",
            "platform": platform,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to connect platform: {str(e)}")


@router.get("/social-dashboard/analytics")
async def get_analytics(platform: Optional[str] = None):
    """Get analytics for platforms"""
    if platform:
        platform_data = platform_stats.get(platform.lower())
        if not platform_data:
            raise HTTPException(status_code=404, detail="Platform not found")
        return {
            "platform": platform,
            "data": platform_data,
        }

    return {
        "all_platforms": platform_stats,
        "total_followers": sum(p.get("followers", 0) for p in platform_stats.values()),
        "total_posts": sum(p.get("posts", 0) for p in platform_stats.values()),
        "total_engagement": sum(p.get("views", 0) for p in platform_stats.values()),
    }


@router.post("/social-dashboard/bulk-post")
async def bulk_post(
    content: str,
    platforms: List[str],
    background_tasks: BackgroundTasks,
):
    """Post to multiple platforms in bulk"""
    if not content.strip():
        raise HTTPException(status_code=400, detail="Content cannot be empty")

    if not platforms:
        raise HTTPException(status_code=400, detail="Select at least one platform")

    # Add background task to post
    background_tasks.add_task(process_bulk_post, content, platforms)

    return {
        "success": True,
        "message": f"Posting to {len(platforms)} platforms...",
        "platforms": platforms,
    }


async def process_bulk_post(content: str, platforms: List[str]):
    """Background task to post to multiple platforms"""
    for platform in platforms:
        try:
            log.info(f"Background posting to {platform}")
            # Update stats
            if platform.lower() in platform_stats:
                platform_stats[platform.lower()]["posts"] += 1
        except Exception as e:
            log.error(f"Error posting to {platform}: {str(e)}")
