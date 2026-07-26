"""Sales Agent Models — Leads, Outreach, Responses"""
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, Float, Enum, ForeignKey
from database import Base
from datetime import datetime
import enum


class LeadSource(str, enum.Enum):
    LINKEDIN = "linkedin"
    COMPANY_WEBSITE = "website"
    DIRECTORY = "directory"
    REFERRAL = "referral"
    MANUAL = "manual"


class LeadStatus(str, enum.Enum):
    NEW = "new"
    RESEARCHED = "researched"
    OUTREACH_SENT = "outreach_sent"
    RESPONDED = "responded"
    MEETING_BOOKED = "meeting_booked"
    QUALIFIED = "qualified"
    DISQUALIFIED = "disqualified"
    CLOSED = "closed"


class OutreachType(str, enum.Enum):
    INITIAL = "initial"
    FOLLOWUP_1 = "followup_1"
    FOLLOWUP_2 = "followup_2"
    FOLLOWUP_3 = "followup_3"


class Lead(Base):
    """Prospect/Lead record"""
    __tablename__ = "sales_leads"

    id = Column(Integer, primary_key=True)

    # Contact info
    first_name = Column(String(255))
    last_name = Column(String(255))
    email = Column(String(255), unique=True, index=True)
    phone = Column(String(20), nullable=True)
    linkedin_url = Column(String(500), nullable=True)

    # Company info
    company_name = Column(String(255), index=True)
    company_website = Column(String(500), nullable=True)
    company_size = Column(String(50), nullable=True)  # "50-100", "100-500", etc.
    industry = Column(String(255), nullable=True)

    # Lead qualification
    fit_score = Column(Integer, default=0)  # 1-100 scoring
    target_product = Column(String(100))  # "video_production", "property_group", "ai_course"
    pain_point = Column(Text, nullable=True)  # Why they need your service

    # Source & tracking
    source = Column(Enum(LeadSource), default=LeadSource.MANUAL)
    status = Column(Enum(LeadStatus), default=LeadStatus.NEW, index=True)

    # Research notes
    research_notes = Column(Text, nullable=True)
    recent_activity = Column(Text, nullable=True)  # Recent news, hiring, funding, etc.

    # Engagement
    times_contacted = Column(Integer, default=0)
    last_contact_date = Column(DateTime, nullable=True)
    last_response_date = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Outreach(Base):
    """Email/message sent to a lead"""
    __tablename__ = "sales_outreach"

    id = Column(Integer, primary_key=True)

    lead_id = Column(Integer, ForeignKey("sales_leads.id"), index=True)
    outreach_type = Column(Enum(OutreachType), default=OutreachType.INITIAL)

    # Message content
    subject = Column(String(500))
    body = Column(Text)

    # Delivery
    sent_at = Column(DateTime, default=datetime.utcnow, index=True)
    sent_via = Column(String(50))  # "email", "linkedin_dm"
    status = Column(String(50), default="sent")  # "sent", "bounced", "opened", "clicked"

    # Engagement tracking
    opened_at = Column(DateTime, nullable=True)
    clicked_at = Column(DateTime, nullable=True)
    replied_at = Column(DateTime, nullable=True)

    # Scheduling
    scheduled_followup_date = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Response(Base):
    """Reply from a lead"""
    __tablename__ = "sales_responses"

    id = Column(Integer, primary_key=True)

    lead_id = Column(Integer, ForeignKey("sales_leads.id"), index=True)
    outreach_id = Column(Integer, ForeignKey("sales_outreach.id"), nullable=True)

    # Response content
    message = Column(Text)
    sentiment = Column(String(50))  # "positive", "neutral", "negative", "neutral_inquiry"

    # Next action
    next_action = Column(String(255), nullable=True)  # "schedule_call", "send_proposal", "disqualify"

    received_at = Column(DateTime, default=datetime.utcnow, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
