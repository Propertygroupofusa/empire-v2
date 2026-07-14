"""
Social Media Autoposter - Cross-post videos to all platforms automatically
YouTube → Instagram → Facebook → TikTok → LinkedIn → Twitter
"""

import os
import logging
from datetime import datetime
from typing import Optional, Dict, List
from enum import Enum

import aiohttp
import requests

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("social_media_autoposter")


class SocialPlatform(str, Enum):
    """Supported social media platforms"""
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    TIKTOK = "tiktok"
    LINKEDIN = "linkedin"
    TWITTER = "twitter"


class SocialMediaAutoposter:
    """Auto-post videos to multiple social platforms"""

    def __init__(self):
        # YouTube (already configured)
        self.youtube_enabled = True

        # Instagram
        self.instagram_enabled = bool(os.getenv("INSTAGRAM_ACCESS_TOKEN"))
        self.instagram_token = os.getenv("INSTAGRAM_ACCESS_TOKEN")
        self.instagram_account_id = os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID")

        # Facebook
        self.facebook_enabled = bool(os.getenv("FACEBOOK_ACCESS_TOKEN"))
        self.facebook_token = os.getenv("FACEBOOK_ACCESS_TOKEN")
        self.facebook_page_id = os.getenv("FACEBOOK_PAGE_ID")

        # TikTok
        self.tiktok_enabled = bool(os.getenv("TIKTOK_ACCESS_TOKEN"))
        self.tiktok_token = os.getenv("TIKTOK_ACCESS_TOKEN")

        # LinkedIn
        self.linkedin_enabled = bool(os.getenv("LINKEDIN_ACCESS_TOKEN"))
        self.linkedin_token = os.getenv("LINKEDIN_ACCESS_TOKEN")
        self.linkedin_org_id = os.getenv("LINKEDIN_ORG_ID")

        # Twitter/X
        self.twitter_enabled = bool(os.getenv("TWITTER_API_KEY"))
        self.twitter_api_key = os.getenv("TWITTER_API_KEY")
        self.twitter_api_secret = os.getenv("TWITTER_API_SECRET")
        self.twitter_bearer_token = os.getenv("TWITTER_BEARER_TOKEN")

        self.posting_history = {}

    def generate_captions(self, video_title: str, video_type: str) -> Dict[str, str]:
        """Generate platform-specific captions"""

        base_caption = f"🎬 {video_title}\n\n"

        return {
            "youtube": f"{base_caption}Subscribe for daily videos 👉 [link in bio]",

            "instagram": f"{base_caption}📈 Daily market & trading insights\n\n#trading #stocks #marketanalysis #investing #financialeducation #daytrading #marketplace #videocontent",

            "facebook": f"{base_caption}Check out this new video! 🚀\n\nSubscribe to our channel for more daily content on trading, markets, and investing.",

            "tiktok": f"{base_caption}#trading #stocks #marketanalysis #invest #daytrader #finance #makemoney #business #entrepreneur #stockmarket",

            "linkedin": f"{base_caption}Professional insights on {video_type}. Follow for daily market analysis and trading strategies.\n\n#trading #stocks #finance #investing #markets",

            "twitter": f"🎯 {video_title}\n\nNew video posted! Check it out → [link]\n\n#trading #stocks #investing #markets #financialeducation"
        }

    async def post_to_instagram(self, video_url: str, caption: str, title: str) -> Dict:
        """Post video to Instagram"""
        if not self.instagram_enabled:
            return {"success": False, "error": "Instagram not configured"}

        try:
            # Instagram requires video upload to Media Library first
            async with aiohttp.ClientSession() as session:
                # Step 1: Create media object
                media_payload = {
                    "media_type": "VIDEO",
                    "video_data": video_url,
                    "caption": caption
                }

                async with session.post(
                    f"https://graph.instagram.com/v18.0/{self.instagram_account_id}/media",
                    json=media_payload,
                    params={"access_token": self.instagram_token}
                ) as response:
                    if response.status == 200:
                        media = await response.json()
                        media_id = media.get("id")

                        # Step 2: Publish the media
                        async with session.post(
                            f"https://graph.instagram.com/v18.0/{self.instagram_account_id}/media_publish",
                            json={"creation_id": media_id},
                            params={"access_token": self.instagram_token}
                        ) as publish_response:
                            if publish_response.status == 200:
                                return {
                                    "success": True,
                                    "platform": "instagram",
                                    "post_id": media_id,
                                    "message": f"Posted to Instagram"
                                }

                    return {"success": False, "error": f"Instagram API error: {response.status}"}
        except Exception as e:
            log.error(f"Instagram post failed: {e}")
            return {"success": False, "error": str(e)}

    async def post_to_facebook(self, video_url: str, caption: str, title: str) -> Dict:
        """Post video to Facebook"""
        if not self.facebook_enabled:
            return {"success": False, "error": "Facebook not configured"}

        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "video": video_url,
                    "description": caption,
                    "title": title
                }

                async with session.post(
                    f"https://graph.facebook.com/v18.0/{self.facebook_page_id}/videos",
                    data=payload,
                    params={"access_token": self.facebook_token}
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        return {
                            "success": True,
                            "platform": "facebook",
                            "post_id": result.get("id"),
                            "message": "Posted to Facebook"
                        }
                    else:
                        return {"success": False, "error": f"Facebook API error: {response.status}"}
        except Exception as e:
            log.error(f"Facebook post failed: {e}")
            return {"success": False, "error": str(e)}

    async def post_to_tiktok(self, video_url: str, caption: str, title: str) -> Dict:
        """Post video to TikTok"""
        if not self.tiktok_enabled:
            return {"success": False, "error": "TikTok not configured"}

        try:
            async with aiohttp.ClientSession() as session:
                # TikTok API requires video upload
                payload = {
                    "video_url": video_url,
                    "caption": caption[:150]  # TikTok caption limit
                }

                async with session.post(
                    "https://open.tiktok.com/v1/post/publish/action/upload/",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.tiktok_token}"}
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        return {
                            "success": True,
                            "platform": "tiktok",
                            "post_id": result.get("data", {}).get("video_id"),
                            "message": "Posted to TikTok"
                        }
                    else:
                        return {"success": False, "error": f"TikTok API error: {response.status}"}
        except Exception as e:
            log.error(f"TikTok post failed: {e}")
            return {"success": False, "error": str(e)}

    async def post_to_linkedin(self, caption: str, title: str, video_url: Optional[str] = None) -> Dict:
        """Post to LinkedIn"""
        if not self.linkedin_enabled:
            return {"success": False, "error": "LinkedIn not configured"}

        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "content": {
                        "contentEntity": video_url,
                        "title": title,
                        "description": caption
                    },
                    "distribution": {
                        "feedDistribution": "MAIN_FEED",
                        "targetEntities": [self.linkedin_org_id]
                    }
                }

                async with session.post(
                    "https://api.linkedin.com/v2/ugcPosts",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.linkedin_token}"}
                ) as response:
                    if response.status == 201:
                        result = await response.json()
                        return {
                            "success": True,
                            "platform": "linkedin",
                            "post_id": result.get("id"),
                            "message": "Posted to LinkedIn"
                        }
                    else:
                        return {"success": False, "error": f"LinkedIn API error: {response.status}"}
        except Exception as e:
            log.error(f"LinkedIn post failed: {e}")
            return {"success": False, "error": str(e)}

    async def post_to_twitter(self, caption: str, video_url: str) -> Dict:
        """Post to Twitter/X"""
        if not self.twitter_enabled:
            return {"success": False, "error": "Twitter not configured"}

        try:
            # Twitter API v2 requires different approach
            # This is simplified - full implementation requires media upload
            headers = {
                "Authorization": f"Bearer {self.twitter_bearer_token}",
                "Content-Type": "application/json"
            }

            payload = {
                "text": caption[:280]  # Twitter char limit
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.twitter.com/2/tweets",
                    json=payload,
                    headers=headers
                ) as response:
                    if response.status == 201:
                        result = await response.json()
                        return {
                            "success": True,
                            "platform": "twitter",
                            "post_id": result.get("data", {}).get("id"),
                            "message": "Posted to Twitter"
                        }
                    else:
                        return {"success": False, "error": f"Twitter API error: {response.status}"}
        except Exception as e:
            log.error(f"Twitter post failed: {e}")
            return {"success": False, "error": str(e)}

    async def autoposts_video(self, video_url: str, title: str, video_type: str = "trading") -> Dict:
        """Auto-post video to all enabled platforms"""

        results = {
            "timestamp": datetime.utcnow().isoformat(),
            "video_url": video_url,
            "title": title,
            "platforms": {}
        }

        captions = self.generate_captions(title, video_type)

        # Post to all platforms in parallel
        tasks = []

        if self.instagram_enabled:
            tasks.append(self.post_to_instagram(video_url, captions["instagram"], title))

        if self.facebook_enabled:
            tasks.append(self.post_to_facebook(video_url, captions["facebook"], title))

        if self.tiktok_enabled:
            tasks.append(self.post_to_tiktok(video_url, captions["tiktok"], title))

        if self.linkedin_enabled:
            tasks.append(self.post_to_linkedin(captions["linkedin"], title, video_url))

        if self.twitter_enabled:
            tasks.append(self.post_to_twitter(captions["twitter"], video_url))

        # Execute all posts concurrently
        import asyncio
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        for response in responses:
            if isinstance(response, dict) and "platform" in response:
                platform = response["platform"]
                results["platforms"][platform] = response

        # Log success
        log.info(f"Video posted to {len([r for r in responses if isinstance(r, dict) and r.get('success')])} platforms")

        return results

    def get_enabled_platforms(self) -> List[str]:
        """Get list of enabled platforms"""
        platforms = ["youtube"]  # YouTube always enabled

        if self.instagram_enabled:
            platforms.append("instagram")
        if self.facebook_enabled:
            platforms.append("facebook")
        if self.tiktok_enabled:
            platforms.append("tiktok")
        if self.linkedin_enabled:
            platforms.append("linkedin")
        if self.twitter_enabled:
            platforms.append("twitter")

        return platforms

    def get_status(self) -> Dict:
        """Get autoposter status"""
        return {
            "enabled_platforms": self.get_enabled_platforms(),
            "youtube": {"enabled": self.youtube_enabled},
            "instagram": {"enabled": self.instagram_enabled, "account_id": self.instagram_account_id},
            "facebook": {"enabled": self.facebook_enabled, "page_id": self.facebook_page_id},
            "tiktok": {"enabled": self.tiktok_enabled},
            "linkedin": {"enabled": self.linkedin_enabled, "org_id": self.linkedin_org_id},
            "twitter": {"enabled": self.twitter_enabled}
        }


# Global instance
autoposter = SocialMediaAutoposter()


def get_autoposter():
    """Get autoposter instance"""
    return autoposter
