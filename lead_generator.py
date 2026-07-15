"""
Lead Generator - Capture and manage leads from YouTube and video content
Automatically scores leads and tracks engagement
"""

import os
import uuid
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from enum import Enum

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lead_generator")


class LeadSource(str, Enum):
    """Where leads come from"""
    YOUTUBE_VIDEO = "youtube_video"
    YOUTUBE_DESCRIPTION = "youtube_description"
    WEBSITE_FORM = "website_form"
    EMAIL_SIGNUP = "email_signup"
    SOCIAL_MEDIA = "social_media"
    REFERRAL = "referral"
    COURSE_INTEREST = "course_interest"
    VIDEO_SERVICE = "video_service"


class LeadStatus(str, Enum):
    """Lead lifecycle status"""
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    NURTURING = "nurturing"
    CONVERTED = "converted"
    LOST = "lost"


class Lead:
    """Represents a lead"""

    def __init__(self, lead_id: str, name: str, email: str, source: LeadSource, source_detail: Optional[str] = None):
        self.lead_id = lead_id
        self.name = name
        self.email = email
        self.source = source
        self.source_detail = source_detail  # Video ID, form name, etc.
        self.status = LeadStatus.NEW
        self.created_at = datetime.utcnow()
        self.last_contacted = None
        self.phone = None
        self.company = None
        self.interest_level = 5  # 1-10 scale
        self.engagement_score = 0
        self.notes = []
        self.tags = []
        self.metadata = {}
        self.converted_value = 0.0

    def add_engagement(self, points: int, action: str):
        """Track engagement activity"""
        self.engagement_score += points
        self.notes.append({
            "timestamp": datetime.utcnow().isoformat(),
            "action": action,
            "points": points
        })

    def add_note(self, note: str):
        """Add a note to the lead"""
        self.notes.append({
            "timestamp": datetime.utcnow().isoformat(),
            "note": note
        })

    def add_tag(self, tag: str):
        """Add a tag for organization"""
        if tag not in self.tags:
            self.tags.append(tag)

    def to_dict(self) -> Dict:
        return {
            "lead_id": self.lead_id,
            "name": self.name,
            "email": self.email,
            "source": self.source,
            "source_detail": self.source_detail,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "last_contacted": self.last_contacted.isoformat() if self.last_contacted else None,
            "phone": self.phone,
            "company": self.company,
            "interest_level": self.interest_level,
            "engagement_score": self.engagement_score,
            "tags": self.tags,
            "converted_value": self.converted_value,
            "notes": self.notes
        }


class LeadGenerator:
    """Manage lead capture and nurturing"""

    def __init__(self):
        self.leads: Dict[str, Lead] = {}
        self.campaigns = {}  # Track lead generation campaigns
        self.email_list = []  # For email marketing

    def create_lead(self, name: str, email: str, source: LeadSource, source_detail: Optional[str] = None, **kwargs) -> Dict:
        """Create a new lead"""
        try:
            # Check if lead already exists
            existing = next((l for l in self.leads.values() if l.email == email), None)
            if existing:
                existing.add_engagement(5, "duplicate_capture")
                return {
                    "success": True,
                    "lead_id": existing.lead_id,
                    "is_new": False,
                    "message": f"Lead already exists: {existing.lead_id}"
                }

            lead_id = f"LEAD-{uuid.uuid4().hex[:8].upper()}"
            lead = Lead(lead_id, name, email, source, source_detail)

            # Set optional fields
            if "phone" in kwargs:
                lead.phone = kwargs["phone"]
            if "company" in kwargs:
                lead.company = kwargs["company"]
            if "interest_level" in kwargs:
                lead.interest_level = kwargs["interest_level"]
            if "tags" in kwargs:
                for tag in kwargs["tags"]:
                    lead.add_tag(tag)

            # Add initial engagement
            lead.add_engagement(10, "new_lead")

            self.leads[lead_id] = lead

            # Add to email list
            if email not in self.email_list:
                self.email_list.append(email)

            log.info(f"Lead created: {lead_id} ({source})")

            return {
                "success": True,
                "lead_id": lead_id,
                "is_new": True,
                "message": f"Lead {lead_id} created from {source}"
            }
        except Exception as e:
            log.error(f"Failed to create lead: {e}")
            return {"success": False, "error": str(e)}

    def update_lead_status(self, lead_id: str, status: LeadStatus, notes: Optional[str] = None) -> Dict:
        """Update lead status"""
        if lead_id not in self.leads:
            return {"error": "Lead not found"}

        lead = self.leads[lead_id]
        old_status = lead.status
        lead.status = status
        lead.last_contacted = datetime.utcnow()

        if notes:
            lead.add_note(notes)

        # Award points based on status progression
        status_points = {
            LeadStatus.CONTACTED: 15,
            LeadStatus.QUALIFIED: 25,
            LeadStatus.NURTURING: 20,
            LeadStatus.CONVERTED: 100,
            LeadStatus.LOST: -10
        }

        if status in status_points:
            lead.add_engagement(status_points[status], f"status_changed: {old_status} -> {status}")

        log.info(f"Lead {lead_id} status updated: {old_status} -> {status}")

        return {
            "success": True,
            "lead_id": lead_id,
            "status": status,
            "engagement_score": lead.engagement_score
        }

    def log_engagement(self, lead_id: str, action: str, points: int = 10) -> Dict:
        """Log lead engagement activity"""
        if lead_id not in self.leads:
            return {"error": "Lead not found"}

        lead = self.leads[lead_id]
        lead.add_engagement(points, action)

        return {
            "success": True,
            "lead_id": lead_id,
            "action": action,
            "engagement_score": lead.engagement_score
        }

    def convert_lead(self, lead_id: str, value: float, conversion_type: str) -> Dict:
        """Convert a lead to customer"""
        if lead_id not in self.leads:
            return {"error": "Lead not found"}

        lead = self.leads[lead_id]
        lead.status = LeadStatus.CONVERTED
        lead.converted_value = value
        lead.add_engagement(100, f"converted: {conversion_type} (${value})")

        log.info(f"Lead {lead_id} converted: ${value} ({conversion_type})")

        return {
            "success": True,
            "lead_id": lead_id,
            "converted_value": value,
            "engagement_score": lead.engagement_score
        }

    def get_lead(self, lead_id: str) -> Optional[Dict]:
        """Get a single lead"""
        if lead_id in self.leads:
            return self.leads[lead_id].to_dict()
        return None

    def get_leads(self, status: Optional[LeadStatus] = None, source: Optional[LeadSource] = None, min_score: int = 0) -> List[Dict]:
        """Get leads with optional filtering"""
        leads = list(self.leads.values())

        if status:
            leads = [l for l in leads if l.status == status]

        if source:
            leads = [l for l in leads if l.source == source]

        leads = [l for l in leads if l.engagement_score >= min_score]

        # Sort by engagement score
        leads.sort(key=lambda x: x.engagement_score, reverse=True)

        return [l.to_dict() for l in leads]

    def create_youtube_description_cta(self, video_title: str) -> str:
        """Generate YouTube description with lead capture CTA"""
        return f"""
📊 {video_title}

👉 **Get Your FREE Video Strategy**
Join {len(self.email_list)} marketers getting daily insights
→ https://your-domain.com/video-strategy (landing page)

📧 **Email Updates**
New trading, market & investment videos daily
→ https://your-domain.com/subscribe

💼 **Custom Video Services**
Professional videos for your business ($500-1000)
→ https://your-domain.com/video-services

🎓 **Video Mastery Course**
Learn to create viral videos that convert
→ https://your-domain.com/course

---
Tags: #trading #videos #marketing #investing
        """

    def create_landing_page_form(self) -> Dict:
        """Return form template for lead capture"""
        return {
            "form_id": f"FORM-{uuid.uuid4().hex[:8].upper()}",
            "fields": [
                {"name": "name", "type": "text", "required": True, "placeholder": "Your Name"},
                {"name": "email", "type": "email", "required": True, "placeholder": "your@email.com"},
                {"name": "company", "type": "text", "required": False, "placeholder": "Company (optional)"},
                {"name": "interest", "type": "select", "required": True, "options": [
                    "Video Services",
                    "Online Course",
                    "YouTube Growth",
                    "Video Marketing"
                ]}
            ],
            "submit_button": "Get Free Video Strategy",
            "success_message": "Check your email for your free video strategy guide!"
        }

    def get_lead_generation_metrics(self) -> Dict:
        """Get lead generation performance metrics"""
        total_leads = len(self.leads)
        qualified = sum(1 for l in self.leads.values() if l.engagement_score >= 30)
        converted = sum(1 for l in self.leads.values() if l.status == LeadStatus.CONVERTED)
        total_value = sum(l.converted_value for l in self.leads.values())

        # Group by source
        by_source = {}
        for lead in self.leads.values():
            source_name = lead.source.value
            if source_name not in by_source:
                by_source[source_name] = {"count": 0, "converted": 0, "value": 0}
            by_source[source_name]["count"] += 1
            if lead.status == LeadStatus.CONVERTED:
                by_source[source_name]["converted"] += 1
                by_source[source_name]["value"] += lead.converted_value

        return {
            "total_leads": total_leads,
            "qualified_leads": qualified,
            "converted_leads": converted,
            "total_lead_value": total_value,
            "avg_value_per_lead": total_value / converted if converted > 0 else 0,
            "conversion_rate": (converted / total_leads * 100) if total_leads > 0 else 0,
            "by_source": by_source,
            "email_subscribers": len(self.email_list)
        }

    def get_hot_leads(self, limit: int = 10) -> List[Dict]:
        """Get highest-scoring qualified leads ready for outreach"""
        qualified = [
            l for l in self.leads.values()
            if l.status in [LeadStatus.NEW, LeadStatus.CONTACTED, LeadStatus.QUALIFIED]
            and l.engagement_score >= 20
        ]

        qualified.sort(key=lambda x: x.engagement_score, reverse=True)
        return [l.to_dict() for l in qualified[:limit]]


# Global instance
generator = LeadGenerator()


def get_generator():
    """Get generator instance"""
    return generator
