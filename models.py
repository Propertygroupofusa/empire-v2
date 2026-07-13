"""SQLAlchemy models for all data entities."""
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, JSON, Float, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base


class Campaign(Base):
    """Outreach campaign with persistent storage."""
    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    description = Column(Text, nullable=True)
    status = Column(String, default="draft")  # draft, active, paused, completed
    outreach_type = Column(String)  # email, sms, call, social
    target_audience = Column(JSON)  # store audience criteria
    message_template = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    scheduled_for = Column(DateTime, nullable=True, index=True)
    completed_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True, index=True)
    custom_metadata = Column(JSON, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "outreach_type": self.outreach_type,
            "target_audience": self.target_audience,
            "message_template": self.message_template,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "scheduled_for": self.scheduled_for.isoformat() if self.scheduled_for else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "is_active": self.is_active,
            "custom_metadata": self.custom_metadata,
        }


class CampaignContact(Base):
    """Individual contact records for campaigns."""
    __tablename__ = "campaign_contacts"

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), index=True)
    email = Column(String, nullable=True, index=True)
    phone = Column(String, nullable=True)
    name = Column(String, nullable=True)
    contact_data = Column(JSON)  # store additional contact info
    status = Column(String, default="pending")  # pending, sent, opened, clicked, replied
    sent_at = Column(DateTime, nullable=True)
    opened_at = Column(DateTime, nullable=True)
    clicked_at = Column(DateTime, nullable=True)
    replied_at = Column(DateTime, nullable=True)
    custom_metadata = Column(JSON, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "campaign_id": self.campaign_id,
            "email": self.email,
            "phone": self.phone,
            "name": self.name,
            "contact_data": self.contact_data,
            "status": self.status,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "clicked_at": self.clicked_at.isoformat() if self.clicked_at else None,
            "replied_at": self.replied_at.isoformat() if self.replied_at else None,
            "custom_metadata": self.custom_metadata,
        }


class Worker(Base):
    """Worker/contractor profile."""
    __tablename__ = "workers"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    name = Column(String)
    phone = Column(String, nullable=True)
    status = Column(String, default="active")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    custom_metadata = Column(JSON, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "phone": self.phone,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "custom_metadata": self.custom_metadata,
        }


class Client(Base):
    """Client profile."""
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    name = Column(String)
    company = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    status = Column(String, default="active")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    custom_metadata = Column(JSON, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "company": self.company,
            "phone": self.phone,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "custom_metadata": self.custom_metadata,
        }
