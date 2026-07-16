"""
Daily Video Publisher - Automated YouTube publishing system
Generates and publishes videos on schedule to build audience
"""

import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional
from enum import Enum

import aiohttp
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("daily_publisher")


class VideoTemplate(str, Enum):
    """Different video types for daily publishing"""
    TRADING_ANALYSIS = "trading"
    MARKET_UPDATE = "market"
    CRYPTO_NEWS = "crypto"
    STOCK_TIPS = "stocks"
    ECONOMIC_NEWS = "economy"
    EARNINGS_RECAP = "earnings"


class ContentGenerator:
    """Generate video content based on daily market data"""

    def __init__(self):
        self.alpaca_api_key = os.getenv("ALPACA_API_KEY")
        self.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")

    async def generate_daily_content(self, template: VideoTemplate) -> dict:
        """Generate daily video content based on market conditions"""

        content_prompts = {
            VideoTemplate.TRADING_ANALYSIS: "Create a compelling 60-second trading analysis video script about today's market action. Include: Market summary, top gainers, key levels, and action points. Make it energetic and professional.",
            VideoTemplate.MARKET_UPDATE: "Write a 60-second video script covering today's market updates. Include stocks, crypto, economic news. Keep it concise and impactful.",
            VideoTemplate.CRYPTO_NEWS: "Generate a 60-second crypto market update script covering: Bitcoin, Ethereum, emerging coins, and market sentiment. Professional tone.",
            VideoTemplate.STOCK_TIPS: "Create a 60-second stock market tip video script. Cover: Best performing stocks, sectors, and investment insights. Keep it actionable.",
            VideoTemplate.ECONOMIC_NEWS: "Write a 60-second economic news video script. Cover: Latest economic indicators, Fed policy, employment data, inflation. Professional delivery.",
            VideoTemplate.EARNINGS_RECAP: "Generate a 60-second earnings recap video script. Cover: Major company earnings, earnings surprises, market reactions. Engaging tone.",
        }

        try:
            # Use Anthropic to generate content
            from anthropic import Anthropic
            client = Anthropic()

            response = client.messages.create(
                model="claude-opus-4-8",
                max_tokens=500,
                messages=[
                    {
                        "role": "user",
                        "content": content_prompts.get(template, content_prompts[VideoTemplate.TRADING_ANALYSIS])
                    }
                ]
            )

            script = response.content[0].text

            return {
                "script": script,
                "template": template,
                "generated_at": datetime.utcnow().isoformat(),
                "title": f"Market Update - {datetime.now().strftime('%B %d, %Y')}",
                "description": f"Daily {template.value} analysis and market update. Subscribe for daily content.",
                "tags": ["trading", "market", "stocks", "analysis", "daily"]
            }
        except Exception as e:
            log.error(f"Failed to generate content: {e}")
            raise


class DailyPublisher:
    """Manages daily video generation and publishing"""

    def __init__(self):
        self.video_generator_url = os.getenv("VIDEO_GENERATOR_URL", "http://localhost:5003")
        self.youtube_auto_publish = os.getenv("YOUTUBE_AUTO_PUBLISH", "true").lower() == "true"
        self.publishing_schedule = {
            "morning": "08:00",      # Market open
            "midday": "12:00",       # Lunch analysis
            "close": "16:30",        # Market close
            "evening": "18:00",      # Evening recap
        }
        self.templates = [
            VideoTemplate.TRADING_ANALYSIS,
            VideoTemplate.MARKET_UPDATE,
            VideoTemplate.CRYPTO_NEWS,
            VideoTemplate.STOCK_TIPS,
            VideoTemplate.ECONOMIC_NEWS,
            VideoTemplate.EARNINGS_RECAP,
        ]
        self.template_index = 0
        self.content_generator = ContentGenerator()
        self.scheduler = BackgroundScheduler()

    async def generate_daily_video(self) -> dict:
        """Generate a video for daily publishing"""
        try:
            # Select template (rotate through different types)
            template = self.templates[self.template_index % len(self.templates)]
            self.template_index += 1

            # Generate content
            content = await self.content_generator.generate_daily_content(template)

            # Send to video generator
            async with aiohttp.ClientSession() as session:
                payload = {
                    "text": content["script"],
                    "videoType": template.value,
                    "autoPublish": self.youtube_auto_publish,
                    "youtubeSettings": {
                        "title": content["title"],
                        "description": content["description"],
                        "tags": content["tags"],
                        "privacy": "public",
                        "category": "22"  # Finance category
                    }
                }

                async with session.post(
                    f"{self.video_generator_url}/api/video-gen/generate",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=300)
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        log.info(f"Video generated: {result.get('jobId')}")
                        return {
                            "success": True,
                            "jobId": result.get("jobId"),
                            "template": template.value,
                            "generated_at": datetime.utcnow().isoformat()
                        }
                    else:
                        log.error(f"Video generation failed: {response.status}")
                        return {"success": False, "error": f"HTTP {response.status}"}
        except Exception as e:
            log.error(f"Daily video generation failed: {e}")
            return {"success": False, "error": str(e)}

    def schedule_daily_publishing(self):
        """Set up scheduled publishing"""
        self.scheduler.add_job(
            self._async_job,
            CronTrigger(hour=8, minute=0),  # 8 AM
            id="morning_video",
            name="Morning market analysis",
            replace_existing=True
        )
        self.scheduler.add_job(
            self._async_job,
            CronTrigger(hour=12, minute=0),  # Noon
            id="midday_video",
            name="Midday market update",
            replace_existing=True
        )
        self.scheduler.add_job(
            self._async_job,
            CronTrigger(hour=16, minute=30),  # 4:30 PM
            id="close_video",
            name="Market close recap",
            replace_existing=True
        )
        self.scheduler.add_job(
            self._async_job,
            CronTrigger(hour=18, minute=0),  # 6 PM
            id="evening_video",
            name="Evening market analysis",
            replace_existing=True
        )

        if not self.scheduler.running:
            self.scheduler.start()
            log.info("Daily publishing scheduled (8am, 12pm, 4:30pm, 6pm)")

    def _async_job(self):
        """Wrapper to run async function in scheduler"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.generate_daily_video())
        finally:
            loop.close()

    def get_schedule_status(self) -> dict:
        """Get current publishing schedule status"""
        return {
            "enabled": self.youtube_auto_publish,
            "schedule": self.publishing_schedule,
            "next_jobs": [
                {
                    "name": job.name,
                    "trigger": str(job.trigger),
                    "next_run": job.next_run_time.isoformat() if job.next_run_time else None
                }
                for job in self.scheduler.get_jobs()
            ]
        }

    def stop(self):
        """Stop the scheduler"""
        if self.scheduler.running:
            self.scheduler.shutdown()


# Global instance
publisher = DailyPublisher()


def start_daily_publisher():
    """Start the daily publishing service"""
    if os.getenv("DAILY_PUBLISHER_ENABLED", "true").lower() == "true":
        publisher.schedule_daily_publishing()
        log.info("Daily publisher started")
        return True
    return False


def get_publisher():
    """Get publisher instance for API use"""
    return publisher
