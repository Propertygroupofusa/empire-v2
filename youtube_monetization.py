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

# The scopes the YOUTUBE_REFRESH_TOKEN must have been granted. This list
# is documentation, not configuration - the token is minted out of band
# (OAuth Playground / a consent flow) and carries whatever scopes were
# ticked there. Getting this list wrong therefore does not fail loudly at
# startup; it fails as an opaque 403 the first time analytics is queried.
#
# It WAS wrong. get_daily_analytics calls the youtubeAnalytics v2 API with
# metrics='views,estimatedMinutesWatched,estimatedRevenue,
# monetizedPlaybacks,impressions', and neither youtube.readonly nor
# youtube grants access to that API at all:
#
#   views, estimatedMinutesWatched, impressions
#       -> yt-analytics.readonly
#   estimatedRevenue, monetizedPlaybacks
#       -> yt-analytics-monetary.readonly
#
# A token minted with only the first two scopes gets
# 403 insufficientPermissions on every analytics call, forever, no matter
# how many times it is refreshed.
SCOPES = [
    'https://www.googleapis.com/auth/youtube.readonly',
    'https://www.googleapis.com/auth/youtube',
    # required by get_daily_analytics - views / watch time / impressions
    'https://www.googleapis.com/auth/yt-analytics.readonly',
    # required by get_daily_analytics - estimatedRevenue / monetizedPlaybacks
    'https://www.googleapis.com/auth/yt-analytics-monetary.readonly',
]


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

        # Circuit breaker for 403 insufficientPermissions.
        #
        # That 403 is permanent: the refresh token was granted without the
        # scopes these calls need, and no amount of retrying or refreshing
        # changes it. But revenue_dashboard polls three of these methods
        # on a loop, so a single misconfiguration produced an endless
        # stream of identical ERROR lines that drowned out everything else
        # in the Railway logs.
        #
        # Once one call comes back with insufficientPermissions, every
        # subsequent call short-circuits and returns the same explanation
        # without touching the network.
        #
        # Deliberately an in-memory latch rather than an env flag: it
        # clears on process restart, so regenerating YOUTUBE_REFRESH_TOKEN
        # and redeploying resumes analytics automatically. An env flag
        # would be one more thing to remember to switch back on, and
        # forgetting it looks exactly like the bug it was hiding.
        self._scope_error = None

        self._init_services()

    def _scope_blocked(self):
        """The cached refusal, if a permanent scope 403 has been seen."""
        return self._scope_error

    def _latch_scope_error(self, e):
        """Return a cached-refusal dict if `e` is a permanent scope 403,
        else None so the caller reports it as an ordinary error."""
        detail = str(e)
        if "insufficientPermissions" not in detail and "403" not in detail:
            return None
        if self._scope_error is None:
            log.error(
                "YouTube returned 403 insufficientPermissions. The "
                "YOUTUBE_REFRESH_TOKEN was granted without the required "
                "scopes, so this will fail on every call until the token is "
                "regenerated with: %s . PAUSING all YouTube API calls until "
                "restart - further attempts would fail identically and only "
                "fill the logs. Note estimatedRevenue and monetizedPlaybacks "
                "additionally require the channel to be in the YouTube "
                "Partner Program.",
                " ".join(SCOPES[2:]),
            )
            self._scope_error = {
                "error": "insufficientPermissions",
                "detail": "YOUTUBE_REFRESH_TOKEN lacks the required scopes; "
                          "regenerate it and redeploy. YouTube API calls are "
                          "paused until then.",
                "required_scopes": SCOPES[2:],
                "paused": True,
            }
        return self._scope_error

    def _init_services(self):
        """Initialize YouTube API services"""
        try:
            if self.youtube_refresh_token:
                credentials = Credentials(
                    token=None,
                    refresh_token=self.youtube_refresh_token,
                    token_uri='https://oauth2.googleapis.com/token',
                    client_id=self.youtube_client_id,
                    client_secret=self.youtube_client_secret
                )
                # channels().list(mine=True) and search().list(forMine=True)
                # (used by get_channel_metrics/get_top_videos) need an
                # authenticated user, not just an API key — a developerKey-only
                # client can't resolve "mine" and falls back to probing for
                # Application Default Credentials, which aren't configured
                # here, then stalls until it times out. Build with the same
                # OAuth credentials used for Analytics instead.
                self.youtube_service = build('youtube', 'v3', credentials=credentials)
                self.youtube_analytics_service = build(
                    'youtubeAnalytics', 'v2', credentials=credentials
                )
                log.info("YouTube Analytics service initialized")
            else:
                # No refresh token: fall back to API-key-only access, which
                # only supports public (non-"mine") lookups.
                self.youtube_service = build('youtube', 'v3', developerKey=self.youtube_api_key)
                log.warning("No YOUTUBE_REFRESH_TOKEN — 'mine' queries (channel/top videos) will fail")
        except Exception as e:
            log.warning(f"YouTube service initialization: {e}")

    def get_channel_metrics(self) -> Dict:
        """Get current channel metrics (views, subscribers, videos)"""
        if self._scope_blocked():
            return self._scope_blocked()
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
            blocked = self._latch_scope_error(e)
            if blocked:
                return blocked
            log.error(f"Failed to fetch channel metrics: {e}")
            return {"error": str(e)}

    def get_daily_analytics(self, days_back: int = 7) -> Dict:
        """Get analytics for last N days"""
        if self._scope_blocked():
            return self._scope_blocked()
        try:
            if not self.youtube_analytics_service:
                return {"error": "YouTube Analytics service not initialized"}

            end_date = datetime.utcnow().date()
            start_date = end_date - timedelta(days=days_back)

            # Request analytics metrics
            response = self.youtube_analytics_service.reports().query(
                ids='channel==MINE',
                startDate=str(start_date),
                endDate=str(end_date),
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
            # A permanent scope 403 latches the circuit breaker so the
            # polling callers stop hammering an endpoint that cannot
            # succeed; anything else is reported normally.
            blocked = self._latch_scope_error(e)
            if blocked:
                return blocked
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
        if self._scope_blocked():
            return self._scope_blocked()
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
            blocked = self._latch_scope_error(e)
            if blocked:
                return blocked
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
