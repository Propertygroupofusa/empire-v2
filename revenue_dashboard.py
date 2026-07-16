"""
Revenue Dashboard - Unified metrics across all revenue streams
Real-time earnings, growth, and performance tracking
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from daily_publisher import get_publisher
from youtube_monetization import get_tracker
from client_video_service import get_service as get_video_service
from lead_generator import get_generator as get_lead_generator
from course_builder import get_builder as get_course_builder

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("revenue_dashboard")


class RevenueDashboard:
    """Unified dashboard across all revenue streams"""

    def __init__(self):
        self.publisher = get_publisher()
        self.youtube_tracker = get_tracker()
        self.video_service = get_video_service()
        self.lead_generator = get_lead_generator()
        self.course_builder = get_course_builder()

    async def get_all_metrics(self, db: AsyncSession) -> Dict:
        """Get comprehensive metrics across all revenue streams"""
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "revenue_summary": await self.get_revenue_summary(db),
            "youtube_metrics": self.get_youtube_metrics(),
            "client_services": await self.get_client_services_metrics(db),
            "courses": self.get_course_metrics(),
            "leads": self.get_lead_metrics(),
            "publishing": self.get_publishing_metrics()
        }

    async def get_revenue_summary(self, db: AsyncSession) -> Dict:
        """Get unified revenue summary"""
        try:
            # YouTube revenue
            analytics = self.youtube_tracker.get_daily_analytics(days_back=30)
            youtube_revenue = analytics.get("totals", {}).get("revenue", 0)

            # Client video service revenue
            video_orders = await self.video_service.get_orders(db, status="delivered")
            client_revenue = sum(
                self.video_service.PRICING[order["tier"]]["amount"] / 100
                for order in video_orders
            )

            # Course revenue
            courses = self.course_builder.get_course_catalog()
            course_revenue = courses.get("total_revenue", 0)

            # Lead conversion revenue (estimated based on conversions)
            lead_metrics = self.lead_generator.get_lead_generation_metrics()
            lead_revenue = lead_metrics.get("total_lead_value", 0)

            total_revenue = youtube_revenue + client_revenue + course_revenue + lead_revenue

            return {
                "total_revenue_30d": total_revenue,
                "projected_monthly": total_revenue,
                "projected_annual": total_revenue * 12,
                "by_source": {
                    "youtube_ads": youtube_revenue,
                    "client_video_services": client_revenue,
                    "course_sales": course_revenue,
                    "lead_conversions": lead_revenue
                },
                "currency": "USD"
            }
        except Exception as e:
            log.error(f"Error calculating revenue summary: {e}")
            return {"error": str(e)}

    def get_youtube_metrics(self) -> Dict:
        """Get YouTube-specific metrics"""
        try:
            metrics = self.youtube_tracker.get_channel_metrics()
            analytics = self.youtube_tracker.get_daily_analytics(days_back=7)
            projections = self.youtube_tracker.calculate_monthly_projections()

            return {
                "channel": metrics,
                "last_7_days": analytics.get("totals", {}),
                "monthly_projection": projections.get("monthly_projection", {}),
                "top_videos": self.youtube_tracker.get_top_videos(limit=5)
            }
        except Exception as e:
            log.error(f"Error getting YouTube metrics: {e}")
            return {"error": str(e)}

    async def get_client_services_metrics(self, db: AsyncSession) -> Dict:
        """Get client video service metrics"""
        try:
            stats = await self.video_service.get_statistics(db)
            pricing = self.video_service.get_pricing()

            orders = await self.video_service.get_orders(db)
            by_tier = {
                tier: sum(1 for o in orders if o["tier"] == tier)
                for tier in pricing["tiers"].keys()
            }

            return {
                "total_orders": stats["total_orders"],
                "completed_orders": stats["completed_orders"],
                "processing_orders": stats["processing_orders"],
                "total_revenue": stats["total_revenue"],
                "avg_order_value": stats["avg_order_value"],
                "orders_by_tier": by_tier,
                "pricing": pricing
            }
        except Exception as e:
            log.error(f"Error getting client services metrics: {e}")
            return {"error": str(e)}

    def get_course_metrics(self) -> Dict:
        """Get course business metrics"""
        try:
            catalog = self.course_builder.get_course_catalog()

            return {
                "total_courses": catalog["total_courses"],
                "total_enrolled": catalog["total_students"],
                "total_revenue": catalog["total_revenue"],
                "avg_revenue_per_course": (
                    catalog["total_revenue"] / catalog["total_courses"]
                    if catalog["total_courses"] > 0 else 0
                ),
                "courses": catalog["courses"]
            }
        except Exception as e:
            log.error(f"Error getting course metrics: {e}")
            return {"error": str(e)}

    def get_lead_metrics(self) -> Dict:
        """Get lead generation metrics"""
        try:
            metrics = self.lead_generator.get_lead_generation_metrics()

            return {
                "total_leads": metrics["total_leads"],
                "qualified_leads": metrics["qualified_leads"],
                "converted_leads": metrics["converted_leads"],
                "total_lead_value": metrics["total_lead_value"],
                "avg_value_per_lead": metrics["avg_value_per_lead"],
                "conversion_rate_percent": metrics["conversion_rate"],
                "email_subscribers": metrics["email_subscribers"],
                "by_source": metrics["by_source"],
                "hot_leads": self.lead_generator.get_hot_leads(limit=5)
            }
        except Exception as e:
            log.error(f"Error getting lead metrics: {e}")
            return {"error": str(e)}

    def get_publishing_metrics(self) -> Dict:
        """Get daily publishing metrics"""
        try:
            schedule = self.publisher.get_schedule_status()

            return {
                "publishing_enabled": schedule["enabled"],
                "schedule": schedule["schedule"],
                "next_jobs": schedule["next_jobs"]
            }
        except Exception as e:
            log.error(f"Error getting publishing metrics: {e}")
            return {"error": str(e)}

    async def get_roi_analysis(self, db: AsyncSession) -> Dict:
        """Analyze return on investment"""
        try:
            revenue_summary = await self.get_revenue_summary(db)
            youtube_data = self.get_youtube_metrics()

            if "error" in revenue_summary or "error" in youtube_data:
                return {"error": "Unable to calculate ROI"}

            total_revenue = revenue_summary["total_revenue_30d"]

            # Estimate content costs
            # Free video generation + hosting = minimal cost
            # Estimated monthly platform cost: $50-150 (from RAILWAY_DEPLOYMENT)
            estimated_monthly_costs = 100  # Conservative estimate

            roi = {
                "monthly_revenue": total_revenue,
                "estimated_monthly_costs": estimated_monthly_costs,
                "net_profit": total_revenue - estimated_monthly_costs,
                "roi_percent": ((total_revenue - estimated_monthly_costs) / estimated_monthly_costs * 100)
                    if estimated_monthly_costs > 0 else 0,
                "break_even_point": "Already profitable" if total_revenue > estimated_monthly_costs else "Not yet profitable",
                "cost_breakdown": {
                    "railway_platform": 75,
                    "domain_hosting": 15,
                    "misc": 10
                }
            }

            return roi
        except Exception as e:
            log.error(f"Error calculating ROI: {e}")
            return {"error": str(e)}

    def get_growth_forecast(self) -> Dict:
        """Forecast growth for next 90 days"""
        try:
            analytics = self.youtube_tracker.get_daily_analytics(days_back=30)

            if "error" in analytics:
                return {"error": "Unable to forecast growth"}

            totals = analytics.get("totals", {})
            current_views_per_day = totals.get("views", 0) / 30

            # Assume 20% monthly growth with daily publishing
            forecast = {
                "current_daily_views": current_views_per_day,
                "30_day_projection": current_views_per_day * 30 * 1.20,
                "60_day_projection": current_views_per_day * 30 * (1.20 ** 2),
                "90_day_projection": current_views_per_day * 30 * (1.20 ** 3),
                "estimated_subscribers_90d": max(0, int(current_views_per_day * 0.01 * 90)),
                "revenue_90d": {
                    "conservative": totals.get("revenue", 0) * 3,
                    "moderate": totals.get("revenue", 0) * 3 * 1.5,
                    "optimistic": totals.get("revenue", 0) * 3 * 2
                }
            }

            return forecast
        except Exception as e:
            log.error(f"Error forecasting growth: {e}")
            return {"error": str(e)}

    async def get_executive_summary(self, db: AsyncSession) -> Dict:
        """Get quick executive summary"""
        try:
            revenue = await self.get_revenue_summary(db)
            leads = self.lead_generator.get_lead_generation_metrics()
            youtube = self.get_youtube_metrics()
            roi = await self.get_roi_analysis(db)

            if any("error" in data for data in [revenue, leads, youtube, roi]):
                return {"error": "Unable to generate summary"}

            return {
                "total_revenue_30d": revenue.get("total_revenue_30d", 0),
                "projected_annual_revenue": revenue.get("projected_annual", 0),
                "profit_this_month": roi.get("net_profit", 0),
                "roi_percent": roi.get("roi_percent", 0),
                "youtube_subscribers": youtube.get("channel", {}).get("subscribers", 0),
                "youtube_views_30d": youtube.get("last_7_days", {}).get("views", 0) * 4,
                "total_leads": leads.get("total_leads", 0),
                "lead_conversion_rate": leads.get("conversion_rate_percent", 0),
                "active_students": self.get_course_metrics().get("total_enrolled", 0),
                "key_insight": self._generate_insight(revenue, leads, youtube)
            }
        except Exception as e:
            log.error(f"Error generating executive summary: {e}")
            return {"error": str(e)}

    def _generate_insight(self, revenue: Dict, leads: Dict, youtube: Dict) -> str:
        """Generate key insight based on current metrics"""
        total_revenue = revenue.get("total_revenue_30d", 0)
        lead_rate = leads.get("conversion_rate_percent", 0)
        views = youtube.get("last_7_days", {}).get("views", 0)

        insights = []

        if total_revenue > 5000:
            insights.append("Strong revenue growth - maintain daily publishing schedule")
        elif total_revenue > 1000:
            insights.append("Solid foundation - focus on YouTube audience growth")
        else:
            insights.append("Build audience - scale daily publishing to 4+ videos/day")

        if lead_rate > 5:
            insights.append("Excellent lead conversion - activate nurture sequences")
        elif lead_rate > 1:
            insights.append("Good lead capture - optimize email campaigns")

        if views > 10000:
            insights.append("Strong audience engagement - monetize with courses")

        return " | ".join(insights) if insights else "Focus on consistent content publishing"


# Global instance
dashboard = RevenueDashboard()


def get_dashboard():
    """Get dashboard instance"""
    return dashboard
