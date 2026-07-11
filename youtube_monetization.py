"""
YouTube Monetization Tracker - Real-time earnings and metrics
Tracks views, watch time, RPM, CPM, and ad revenue
"""

import os
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("youtube_monetization")

SCOPES = ['https://www.googleapis.com/auth/youtube.readonly',
          'https://www.googleapis.com/auth/youtube']


class YouTubeMonetizationTracker:
    """Track YouTube channel earnings and metrics"""

    def __init__(self):
        self.youtube_api_key = os.getenv("YOUTUBE_API_KEY")
        self.youtube_client_id = os.getenv("YOUTUBE_CLIENT_ID")
        self.youtube_client_secret = os.getenv("YOUTUBE_CLIENT_SECRET")
        self.youtube_refresh_token = os.getenv("YOUTUBE_REFRESH_TOKEN")

        self.youtube_service = None
        self.youtube_analytics_service = None
        self.metrics_cache = {}
        self.earnings_cache = {}

        self._init_services()

    def _init_services(self):
        """Initialize YouTube API services"""
        try:
            # Create YouTube service
            self.youtube_service = build('youtube', 'v3', developerKey=self.youtube_api_key)

            # Create YouTube Analytics service with refresh token
            if self.youtube_refresh_token:
                credentials = Credentials(
                    token=None,
                    refresh_token=self.youtube_refresh_token,
                    token_uri='https://oauth2.googleapis.com/token',
                    client_id=self.youtube_client_id,
                    client_secret=self.youtube_client_secret
                )
                self.youtube_analytics_service = build(
                    'youtubeAnalytics', 'v2', credentials=credentials
                )
                log.info("YouTube Analytics service initialized")
        except Exception as e:
            log.warning(f"YouTube service initialization: {e}")

    def get_channel_metrics(self) -> Dict:
        """Get current channel metrics (views, subscribers, videos)"""
        try:
            if not self.youtube_service:
                return {"error": "YouTube service not initialized"}

            # Get channel info
            channels = self.youtube_service.channels().list(
                part='statistics,snippet,contentDetails',
                mine=True
            ).execute()

            if not channels.get('items'):
                return {"error": "Channel not found"}

            channel = channels['items'][0]
            stats = channel['statistics']

            metrics = {
                "channel_id": channel['id'],
                "channel_name": channel['snippet']['title'],
                "subscribers": int(stats.get('subscriberCount', 0)),
                "total_views": int(stats.get('viewCount', 0)),
                "total_videos": int(stats.get('videoCount', 0)),
                "fetched_at": datetime.utcnow().isoformat()
            }

            self.metrics_cache = metrics
            return metrics
        except Exception as e:
            log.error(f"Failed to fetch channel metrics: {e}")
            return {"error": str(e)}

    def get_daily_analytics(self, days_back: int = 7) -> Dict:
        """Get analytics for last N days"""
        try:
            if not self.youtube_analytics_service:
                return {"error": "YouTube Analytics service not initialized"}

            end_date = datetime.utcnow().date()
            start_date = end_date - timedelta(days=days_back)

            # Request analytics metrics
            response = self.youtube_analytics_service.reports().query(
                ids='channel==MINE',
                start_date=str(start_date),
                end_date=str(end_date),
                metrics='views,estimatedMinutesWatched,estimatedRevenue,monetizedPlaybacks,impressions',
                dimensions='day'
            ).execute()

            analytics = {
                "period": f"Last {days_back} days",
                "start_date": str(start_date),
                "end_date": str(end_date),
                "daily_data": [],
                "totals": {}
            }

            if response.get('rows'):
                totals = {
                    "views": 0,
                    "watch_time_minutes": 0,
                    "revenue": 0,
                    "monetized_playbacks": 0,
                    "impressions": 0
                }

                for row in response['rows']:
                    day = row[0]
                    views = int(row[1]) if len(row) > 1 else 0
                    watch_time = int(row[2]) if len(row) > 2 else 0
                    revenue = float(row[3]) if len(row) > 3 else 0
                    monetized = int(row[4]) if len(row) > 4 else 0
                    impressions = int(row[5]) if len(row) > 5 else 0

                    analytics["daily_data"].append({
                        "date": day,
                        "views": views,
                        "watch_time_minutes": watch_time,
                        "estimated_revenue": revenue,
                        "monetized_playbacks": monetized,
                        "impressions": impressions,
                        "rpm": (revenue / (views / 1000)) if views > 0 else 0,
                        "cpm": (revenue / (impressions / 1000)) if impressions > 0 else 0
                    })

                    totals["views"] += views
                    totals["watch_time_minutes"] += watch_time
                    totals["revenue"] += revenue
                    totals["monetized_playbacks"] += monetized
                    totals["impressions"] += impressions

                analytics["totals"] = {
                    **totals,
                    "avg_rpm": (totals["revenue"] / (totals["views"] / 1000)) if totals["views"] > 0 else 0,
                    "avg_cpm": (totals["revenue"] / (totals["impressions"] / 1000)) if totals["impressions"] > 0 else 0,
                }

            return analytics
        except Exception as e:
            log.error(f"Failed to fetch analytics: {e}")
            return {"error": str(e)}

    def calculate_monthly_projections(self) -> Dict:
        """Project monthly earnings based on recent performance"""
        try:
            analytics = self.get_daily_analytics(days_back=30)

            if "error" in analytics:
                return analytics

            totals = analytics.get("totals", {})

            # Calculate daily average
            days_of_data = len(analytics.get("daily_data", []))
            if days_of_data == 0:
                return {"error": "Insufficient data"}

            daily_revenue = totals.get("revenue", 0) / days_of_data
            daily_views = totals.get("views", 0) / days_of_data
            daily_watch_time = totals.get("watch_time_minutes", 0) / days_of_data

            projections = {
                "period": "Monthly projection (based on last 30 days)",
                "daily_average": {
                    "revenue": daily_revenue,
                    "views": daily_views,
                    "watch_time_minutes": daily_watch_time,
                    "rpm": totals.get("avg_rpm", 0)
                },
                "monthly_projection": {
                    "revenue": daily_revenue * 30,
                    "views": daily_views * 30,
                    "watch_time_hours": (daily_watch_time / 60) * 30,
                    "estimated_subscribers_gained": max(0, int(daily_views * 0.01) * 30)  # 1% conversion
                },
                "annual_projection": {
                    "revenue": daily_revenue * 365,
                    "views": daily_views * 365,
                    "watch_time_hours": (daily_watch_time / 60) * 365,
                }
            }

            return projections
        except Exception as e:
            log.error(f"Failed to calculate projections: {e}")
            return {"error": str(e)}

    def get_top_videos(self, limit: int = 10) -> Dict:
        """Get top performing videos"""
        try:
            if not self.youtube_service:
                return {"error": "YouTube service not initialized"}

            search = self.youtube_service.search().list(
                part='id,snippet',
                forMine=True,
                order='viewCount',
                maxResults=limit,
                type='video'
            ).execute()

            videos = []
            for item in search.get('items', []):
                video_id = item['id']['videoId']
                title = item['snippet']['title']

                # Get video stats
                stats = self.youtube_service.videos().list(
                    part='statistics,snippet',
                    id=video_id
                ).execute()

                if stats['items']:
                    video_stats = stats['items'][0]['statistics']
                    videos.append({
                        "video_id": video_id,
                        "title": title,
                        "views": int(video_stats.get('viewCount', 0)),
                        "likes": int(video_stats.get('likeCount', 0)),
                        "comments": int(video_stats.get('commentCount', 0)),
                        "shares": int(video_stats.get('shareCount', 0))
                    })

            return {
                "top_videos": videos,
                "total": len(videos),
                "fetched_at": datetime.utcnow().isoformat()
            }
        except Exception as e:
            log.error(f"Failed to get top videos: {e}")
            return {"error": str(e)}

    def get_revenue_summary(self) -> Dict:
        """Get complete revenue summary"""
        return {
            "channel_metrics": self.get_channel_metrics(),
            "daily_analytics": self.get_daily_analytics(days_back=7),
            "monthly_projection": self.calculate_monthly_projections(),
            "top_videos": self.get_top_videos(limit=5)
        }


# Global instance
tracker = YouTubeMonetizationTracker()


def get_tracker():
    """Get tracker instance"""
    return tracker
