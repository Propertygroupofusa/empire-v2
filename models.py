"""SQLAlchemy models for all data entities."""
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, JSON, Float, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base


class User(Base):
    """Customer/user account for authentication."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    password_hash = Column(String)
    name = Column(String, nullable=True)
    is_active = Column(Boolean, default=True, index=True)
    is_verified = Column(Boolean, default=False)
    verification_code = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "is_active": self.is_active,
            "is_verified": self.is_verified,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


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


class StudyUser(Base):
    """Study app user subscription."""
    __tablename__ = "study_users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    tier = Column(String, default="free")  # free, paid
    materials_generated_month = Column(Integer, default=0)
    stripe_customer_id = Column(String, nullable=True)
    stripe_subscription_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "email": self.email,
            "tier": self.tier,
            "materials_generated_month": self.materials_generated_month,
        }


class VideoQuoteOrder(Base):
    """Customer video quote/order from the /orders/request-quote flow."""
    __tablename__ = "video_quote_orders"

    id = Column(Integer, primary_key=True, index=True)
    status = Column(String, default="quote_requested")
    customer_name = Column(String)
    customer_email = Column(String, index=True)
    customer_company = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    video_type = Column(String)
    script_or_topic = Column(Text)
    target_audience = Column(String, nullable=True)
    avatar = Column(String)
    language = Column(String)
    delivery_days = Column(Integer, default=2)
    reference_url = Column(String, nullable=True)
    requested_at = Column(DateTime, default=datetime.utcnow)
    quote_price = Column(Integer, nullable=True)
    paid = Column(Boolean, default=False)
    stripe_session_id = Column(String, nullable=True)
    transaction_id = Column(String, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    video_url = Column(String, nullable=True)
    video_download_link = Column(String, nullable=True)
    video_generation_status = Column(String, default="pending")
    refunded = Column(Boolean, default=False)
    refund_amount = Column(Integer, nullable=True)
    refund_status = Column(String, nullable=True)
    refund_transaction_id = Column(String, nullable=True)
    refunded_at = Column(DateTime, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "status": self.status,
            "customer_name": self.customer_name,
            "customer_email": self.customer_email,
            "customer_company": self.customer_company,
            "phone": self.phone,
            "video_type": self.video_type,
            "script_or_topic": self.script_or_topic,
            "target_audience": self.target_audience,
            "avatar": self.avatar,
            "language": self.language,
            "delivery_days": self.delivery_days,
            "reference_url": self.reference_url,
            "requested_at": self.requested_at.isoformat() if self.requested_at else None,
            "quote_price": self.quote_price,
            "paid": self.paid,
            "stripe_session_id": self.stripe_session_id,
            "transaction_id": self.transaction_id,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "video_url": self.video_url,
            "video_download_link": self.video_download_link,
            "video_generation_status": self.video_generation_status,
        }


class ClientVideoOrder(Base):
    """Tiered ($500/$750/$1000) video order from client_video_service.py."""
    __tablename__ = "client_video_orders"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(String, unique=True, index=True)
    client_email = Column(String, index=True)
    tier = Column(String)
    script = Column(Text)
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)
    video_job_id = Column(String, nullable=True)
    download_link = Column(String, nullable=True)
    revisions_used = Column(Integer, default=0)
    payment_id = Column(String, nullable=True)

    def to_dict(self, max_revisions: int = 0):
        return {
            "order_id": self.order_id,
            "client_email": self.client_email,
            "tier": self.tier,
            "script": self.script,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "video_job_id": self.video_job_id,
            "download_link": self.download_link,
            "revisions_used": self.revisions_used,
            "max_revisions": max_revisions,
            "payment_id": self.payment_id,
        }


class CustomerSubscription(Base):
    """Video-subscription-tier record from subscription_tiers.py."""
    __tablename__ = "customer_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    customer_email = Column(String, unique=True, index=True)
    tier_id = Column(String)
    start_date = Column(DateTime, default=datetime.utcnow)
    current_period_start = Column(DateTime, default=datetime.utcnow)
    current_period_end = Column(DateTime)
    videos_used_this_month = Column(Integer, default=0)
    active = Column(Boolean, default=True)
    stripe_subscription_id = Column(String, nullable=True)
    stripe_customer_id = Column(String, nullable=True)
    status = Column(String, nullable=True)
    payment_status = Column(String, nullable=True)

    def to_dict(self):
        return {
            "customer_email": self.customer_email,
            "tier_id": self.tier_id,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "current_period_start": self.current_period_start.isoformat() if self.current_period_start else None,
            "current_period_end": self.current_period_end.isoformat() if self.current_period_end else None,
            "videos_used_this_month": self.videos_used_this_month,
            "active": self.active,
            "stripe_subscription_id": self.stripe_subscription_id,
            "stripe_customer_id": self.stripe_customer_id,
            "status": self.status,
            "payment_status": self.payment_status,
        }


class TradingBotState(Base):
    """Per-bot tracked state for the trading dashboard - Alpaca itself has no
    concept of 'base capital' vs 'profit', so we track our own baseline here.
    Profit shown on the dashboard is real equity minus this stored value."""
    __tablename__ = "trading_bot_state"

    id = Column(Integer, primary_key=True, index=True)
    bot_name = Column(String, unique=True, index=True)  # e.g. "bare_metal_builders"
    base_capital = Column(Float)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "bot_name": self.bot_name,
            "base_capital": self.base_capital,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class WithdrawalRequest(Base):
    """A real record of a requested profit withdrawal. No transfer API is
    called - Alpaca's standard trading API doesn't expose one for a
    self-directed account. The actual bank transfer is done manually in
    Alpaca's own app; this just tracks that it was requested and lets it be
    marked completed once you've done that."""
    __tablename__ = "withdrawal_requests"

    id = Column(Integer, primary_key=True, index=True)
    bot_name = Column(String, index=True)
    amount = Column(Float)
    status = Column(String, default="requested")  # requested, completed
    requested_at = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at = Column(DateTime, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "bot_name": self.bot_name,
            "amount": self.amount,
            "status": self.status,
            "requested_at": self.requested_at.isoformat() if self.requested_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class StudyMaterial(Base):
    """Generated study materials from textbook images."""
    __tablename__ = "study_materials"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, index=True)  # email
    material_type = Column(String)  # guide, quiz, flashcards
    original_image_url = Column(String, nullable=True)
    source_text = Column(Text)  # OCR'd text from image
    generated_content = Column(JSON)  # The actual study material
    title = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    page_count = Column(Integer, nullable=True)
    topic = Column(String, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "material_type": self.material_type,
            "title": self.title,
            "created_at": self.created_at.isoformat(),
            "generated_content": self.generated_content,
        }
