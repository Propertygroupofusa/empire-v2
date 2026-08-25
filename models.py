"""SQLAlchemy models for all data entities."""
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, JSON, Float, ForeignKey, Enum
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base
import enum


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


class ReferralContact(Base):
    """Past customers enrolled in the post-delivery referral campaign
    (email_campaigns.py) - a different thing from CampaignContact above,
    which is a recipient row belonging to a Campaign.

    This used to be a second class named CampaignContact declared inside
    email_campaigns.py against the SAME "campaign_contacts" table and the
    same Base. Importing both raised

        InvalidRequestError: Table 'campaign_contacts' is already defined
        for this MetaData instance.

    which only stayed latent because main.py never imported
    email_campaigns. It lives here now so that it has its own table and
    so run_migrations() actually creates it - the migration walks
    Base.metadata.sorted_tables, and a model declared in a module the app
    never imports is not in that registry at all.
    """
    __tablename__ = "referral_contacts"

    id = Column(Integer, primary_key=True, index=True)
    customer_name = Column(String, nullable=True)
    customer_email = Column(String, index=True, unique=True)
    company = Column(String, nullable=True)
    referral_code = Column(String, unique=True, index=True)
    status = Column(String, default="new", index=True)  # new, sent, opened, clicked, converted
    email_sent_at = Column(DateTime, nullable=True)
    opened_at = Column(DateTime, nullable=True)
    clicked_at = Column(DateTime, nullable=True)
    converted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "customer_name": self.customer_name,
            "customer_email": self.customer_email,
            "company": self.company,
            "referral_code": self.referral_code,
            "status": self.status,
            "email_sent_at": self.email_sent_at.isoformat() if self.email_sent_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Worker(Base):
    """Worker/contractor profile. Also doubles as the identity the trading
    bots (prop_bot.py, alpaca_swing_bot.py) use to record their own real
    earnings via worker_id="bot@pgusa.local" - see main.py's
    initialize_bot(). Fields below (w9_*, credentials_*) mirror the raw
    ALTER TABLE columns main.py's run_migrations() adds to the real
    "workers" table - declared here too so the ORM can actually read/write
    them (previously these existed only as orphaned raw DB columns nothing
    in the code touched)."""
    __tablename__ = "workers"

    id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    email = Column(String, unique=True, index=True)
    name = Column(String)
    phone = Column(String, nullable=True)
    status = Column(String, default="active")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    custom_metadata = Column(JSON, nullable=True)

    # bcrypt hash for self-service worker login (see worker_auth.py) -
    # nullable since workers registered before this existed have none yet
    # (they'd need to go through a password-set flow to gain login access).
    # Deliberately never included in to_dict().
    password_hash = Column(String, nullable=True)

    w9_submitted = Column(Boolean, default=False)
    w9_legal_name = Column(String, nullable=True)
    w9_tax_classification = Column(String, nullable=True)
    w9_tin_last4 = Column(String, nullable=True)
    w9_address = Column(Text, nullable=True)

    credentials_submitted = Column(Boolean, default=False)
    credentials_verified = Column(Boolean, default=False)

    # Stripe Connect account for automated payouts
    stripe_account_id = Column(String, nullable=True)

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
            "w9_submitted": self.w9_submitted,
            "w9_legal_name": self.w9_legal_name,
            "w9_tax_classification": self.w9_tax_classification,
            "w9_tin_last4": self.w9_tin_last4,
            "credentials_submitted": self.credentials_submitted,
            "credentials_verified": self.credentials_verified,
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


class Job(Base):
    """A billable unit of work performed for a client - currently used by
    the video production flow (routers/orders.py creates one per paid
    VideoQuoteOrder, job_type="video_production", for bot-earnings
    tracking); the shape stays generic enough to reuse for another
    vertical without a separate table per service type."""
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    job_type = Column(String, index=True)  # "video_production", ...
    client_id = Column(Integer, ForeignKey("clients.id"), index=True)
    worker_id = Column(Integer, ForeignKey("workers.id"), nullable=True, index=True)
    state = Column(String, index=True)  # US state jurisdiction the job must be handled in, if relevant
    description = Column(Text, nullable=True)
    status = Column(String, default="requested", index=True)  # requested, matched, scheduled, completed, cancelled
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    matched_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    custom_metadata = Column(JSON, nullable=True)

    service_tier = Column(String, nullable=True)  # e.g. a pricing tier key, if the vertical uses one
    price = Column(Float, nullable=True)  # USD, snapshotted at request/creation time
    paid = Column(Boolean, default=False)
    stripe_session_id = Column(String, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "job_type": self.job_type,
            "client_id": self.client_id,
            "worker_id": self.worker_id,
            "state": self.state,
            "description": self.description,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "matched_at": self.matched_at.isoformat() if self.matched_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "custom_metadata": self.custom_metadata,
            "service_tier": self.service_tier,
            "price": self.price,
            "paid": self.paid,
        }


class StudyUser(Base):
    """Study app user subscription."""
    __tablename__ = "study_users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    # bcrypt hash. Nullable because rows created under the old
    # no-authentication scheme have no password - those accounts cannot
    # log in until they sign up properly, which is the intended outcome:
    # they were never owned by anyone in the first place, since any caller
    # could conjure one by sending an arbitrary email as a bearer token.
    password_hash = Column(String, nullable=True)
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
    Profit shown on the dashboard is real equity minus this stored value.

    base_capital is the bucket's whole current tracked value (see
    routers/trading_dashboard.py's per-bot withdrawal design). starting_capital
    is a separate, never-updated snapshot of what the bucket started at, so
    each bucket's own profit = base_capital - starting_capital - used by the
    "withdraw all profit" bulk endpoint to know how much of each bucket is
    actually gain versus original principal, without touching the principal."""
    __tablename__ = "trading_bot_state"

    id = Column(Integer, primary_key=True, index=True)
    bot_name = Column(String, unique=True, index=True)  # e.g. "bare_metal_builders"
    base_capital = Column(Float)
    starting_capital = Column(Float, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "bot_name": self.bot_name,
            "base_capital": self.base_capital,
            "starting_capital": self.starting_capital,
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


class SupportAccount(Base):
    """A tenant of the AI customer-service product - one per paying
    business using it to handle their own customers' email support.
    api_key is the shared secret their inbound-parse webhook URL is
    scoped by (SendGrid doesn't sign inbound-parse requests the way
    Stripe signs webhooks, so this is the auth for that endpoint)."""
    __tablename__ = "support_accounts"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    business_name = Column(String)
    api_key = Column(String, unique=True, index=True)
    inbound_email = Column(String, nullable=True)  # the address customers email in to
    status = Column(String, default="active")
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "business_name": self.business_name,
            "inbound_email": self.inbound_email,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class KnowledgeBaseEntry(Base):
    """One Q&A/fact entry in a support account's knowledge base, used to
    ground the AI agent's replies instead of letting it improvise."""
    __tablename__ = "knowledge_base_entries"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("support_accounts.id"), index=True)
    topic = Column(String)
    content = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "account_id": self.account_id,
            "topic": self.topic,
            "content": self.content,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class SupportConversation(Base):
    """One customer's email thread with a support account. status covers
    what a separate 'ticket' table would otherwise track - there's no
    need for two objects when a conversation's lifecycle IS the ticket's
    lifecycle here."""
    __tablename__ = "support_conversations"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("support_accounts.id"), index=True)
    customer_email = Column(String, index=True)
    subject = Column(String, nullable=True)
    status = Column(String, default="open", index=True)  # open, escalated, resolved
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "account_id": self.account_id,
            "customer_email": self.customer_email,
            "subject": self.subject,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class SupportMessage(Base):
    """One message within a SupportConversation, from either side."""
    __tablename__ = "support_messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("support_conversations.id"), index=True)
    sender = Column(String)  # "customer" or "ai"
    body = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "sender": self.sender,
            "body": self.body,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class DailyBrief(Base):
    """One day's Daily Ventures Brief (see daily_brief.py) - persisted so
    the briefing history becomes searchable later ("show me every day
    revenue exceeded $X"), not just a one-off email that's gone once it
    leaves the inbox. Raw snapshots are stored alongside the generated
    summary so future querying isn't limited to whatever Claude happened
    to mention in the prose that day."""
    __tablename__ = "daily_briefs"

    id = Column(Integer, primary_key=True, index=True)
    summary = Column(Text)
    trading_snapshot = Column(JSON, nullable=True)
    notary_snapshot = Column(JSON, nullable=True)
    support_snapshot = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "summary": self.summary,
            "trading_snapshot": self.trading_snapshot,
            "notary_snapshot": self.notary_snapshot,
            "support_snapshot": self.support_snapshot,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ============================================================
# Sales Agent Models
# ============================================================

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
    """Prospect/Lead record for Sales Agent"""
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
    company_size = Column(String(50), nullable=True)
    industry = Column(String(255), nullable=True)

    # Lead qualification
    fit_score = Column(Integer, default=0)
    target_product = Column(String(100))
    pain_point = Column(Text, nullable=True)

    # Source & tracking
    source = Column(Enum(LeadSource), default=LeadSource.MANUAL)
    status = Column(Enum(LeadStatus), default=LeadStatus.NEW, index=True)

    # Research notes
    research_notes = Column(Text, nullable=True)
    recent_activity = Column(Text, nullable=True)

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
    sent_via = Column(String(50))
    status = Column(String(50), default="sent")

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
    sentiment = Column(String(50), nullable=True)

    # Next action
    next_action = Column(String(255), nullable=True)

    received_at = Column(DateTime, default=datetime.utcnow, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class PayoutStatus(str, enum.Enum):
    """Status of a worker payout."""
    pending = "pending"
    processing = "processing"
    paid = "paid"
    failed = "failed"


class Payment(Base):
    """Payment record for completed jobs - tracks earnings for workers."""
    __tablename__ = "payments"

    id = Column(String, primary_key=True, index=True)
    job_id = Column(String, nullable=True, index=True)
    worker_id = Column(String, nullable=True, index=True)
    client_id = Column(String, nullable=True, index=True)
    gross_amount = Column(Float)  # Total amount from client
    worker_amount = Column(Float)  # Worker's earnings
    platform_amount = Column(Float)  # Platform fee
    payout_status = Column(String, default="pending", index=True)  # pending, processing, paid, failed
    stripe_payout_id = Column(String, nullable=True, unique=True)  # Stripe payout ID when transferred
    stripe_transfer_id = Column(String, nullable=True)  # Stripe Transfer ID if using Connect
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    paid_at = Column(DateTime, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "job_id": self.job_id,
            "worker_id": self.worker_id,
            "worker_amount": self.worker_amount,
            "platform_amount": self.platform_amount,
            "payout_status": self.payout_status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "paid_at": self.paid_at.isoformat() if self.paid_at else None,
        }


class BotPosition(Base):
    """A trading bot's currently-open position, persisted so a Railway
    restart can't silently orphan a real open position. Both prop_bot.py
    and crypto_coinbase_bot.py track open positions in an in-memory dict
    for fast per-cycle access - that dict is the source of truth for
    trading decisions, but it used to live only in process memory, so
    every redeploy wiped it while the position stayed open for real on
    the broker. Each bot now mirrors its dict here (insert on open,
    delete on close) and reloads from this table into the dict once on
    startup, before its first cycle runs."""
    __tablename__ = "bot_positions"

    id = Column(Integer, primary_key=True, index=True)
    bot = Column(String, index=True)  # "crypto_coinbase" or "prop_apex"
    symbol = Column(String, index=True)  # e.g. "BTC/USD" or a futures contract code
    side = Column(String)  # "long" or "short"
    entry_price = Column(Float)
    qty = Column(Float)
    opened_at = Column(DateTime, default=datetime.utcnow)

    # Best unrealized return this position has reached, as a fraction
    # (0.023 = it was up 2.3% at some point). Needed by prop_bot's
    # trailing stop, and persisted rather than kept in a module-level dict
    # for the same reason the rest of this row is: a Railway redeploy
    # wipes process memory while the position stays open on the broker.
    # An in-memory peak would silently reset to zero on every restart,
    # disarming the trailing stop on exactly the positions that had run up
    # the most - and a dict lookup would KeyError against a position
    # reloaded from this table. Nullable so existing rows migrate cleanly;
    # readers treat None as "no peak recorded yet".
    peak_pct = Column(Float, nullable=True)

    # Fixed at entry time by crypto_btc_compound_bot.py so a restart doesn't
    # let a re-measured, possibly-shifted volatility silently move a
    # position's exit points. Nullable/unused by every other bot's rows.
    target_price = Column(Float, nullable=True)
    stop_price = Column(Float, nullable=True)

    # ── Signal snapshot at the moment of entry ───────────────────────────
    #
    # Both bots already compute these to DECIDE the trade and then throw
    # them away - they reach a log line and nothing else. That makes it
    # impossible to ask afterwards "did entries below RSI 35 do better?"
    # or "did high-volatility entries lose more?", because the conditions
    # that caused each trade no longer exist anywhere by the time its
    # outcome is known.
    #
    # Captured here so that when the position closes, ClosedTrade can pair
    # these inputs with the realised result and every trade becomes one
    # labelled row. Nullable throughout: positions already open at deploy
    # time, and any adopted by reconcile_positions_with_broker (which only
    # learns of a position after the fact and never sees its entry
    # signals), legitimately have none.
    entry_rsi = Column(Float, nullable=True)
    entry_trend = Column(String, nullable=True)      # "bullish" / "bearish"
    entry_atr_pct = Column(Float, nullable=True)     # ATR as a fraction of price


class CryptoTreeBranch(Base):
    """One branch of crypto_family_tree_bot.py's compounding tree - BTC is
    the root (parent_bot_name NULL), and each child is a single-position
    engine (same one crypto_btc_compound_bot.py runs) trading its own coin.

    All branches share ONE real Coinbase account/USD wallet - there is no
    such thing as a real per-branch sub-account. allocated_usd is this
    branch's VIRTUAL slice of that one real pool: what it's currently
    holding, whether that's sitting in cash or marked-to-market in an open
    position (see BotPosition, keyed by this row's bot_name). A branch only
    ever spends up to its own allocated_usd (capped again by whatever the
    real account balance actually allows, as a hard safety backstop) - so
    two branches can never both try to deploy the same real dollars, the
    way they would if each one sized its buys off the whole real balance.

    Spawning a child is a pure bookkeeping transfer, not a trade: the
    parent's allocated_usd drops by the seed amount, a new row is inserted
    for the child with that seed amount, and both numbers still sum to the
    same real dollars sitting in the one real wallet - nothing needs to be
    bought or sold to make that split real, since real money never moved
    accounts to begin with."""
    __tablename__ = "crypto_tree_branches"

    id = Column(Integer, primary_key=True, index=True)
    bot_name = Column(String, unique=True, index=True)  # e.g. "crypto_tree_btc", "crypto_tree_litecoin_usd"
    product_id = Column(String)  # Coinbase product id, e.g. "BTC-USD"
    parent_bot_name = Column(String, nullable=True, index=True)  # NULL for the root (BTC)

    allocated_usd = Column(Float)  # this branch's current virtual slice, cash-equivalent
    next_unlock_tier = Column(Float)  # the next allocated_usd milestone that spawns a child (starts at 1000, then 2000, ...)

    # Mirrors BotPosition/crypto_btc_compound_bot.py's equity floor ratchet,
    # but scoped to this branch's own allocated_usd rather than the whole
    # account - each branch protects its own banked progress independently.
    equity_floor = Column(Float, default=0.0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ClosedTrade(Base):
    """One completed round trip, with the conditions that caused it.

    Nothing durable records a finished trade today. prop_bot and
    crypto_coinbase_bot log the close and DELETE the BotPosition row, so
    the outcome survives only in Railway stdout (which rotates) and in
    ml_trades.json, which is gitignored AND on Railway's ephemeral disk -
    wiped on every redeploy. That is why market_brain's ML filter never
    accumulated a training set: its dataset was being deleted continuously.

    This table is the missing half. Paired with the entry snapshot copied
    off BotPosition, each row is a supervised training example - features
    known BEFORE the trade, label known after - which is the minimum needed
    to answer "which conditions predict wins" from real money rather than
    from a backtest.

    Deliberately append-only and never deleted. Rows are cheap and the
    value is entirely in the accumulated history.
    """
    __tablename__ = "closed_trades"

    id = Column(Integer, primary_key=True, index=True)
    bot = Column(String, index=True)
    symbol = Column(String, index=True)
    side = Column(String)

    # features - all known at entry, before the outcome exists
    entry_price = Column(Float)
    entry_rsi = Column(Float, nullable=True)
    entry_trend = Column(String, nullable=True)
    entry_atr_pct = Column(Float, nullable=True)
    qty = Column(Float)

    # label - known only at exit
    exit_price = Column(Float)
    exit_reason = Column(String, index=True)   # PROFIT TARGET / STOP LOSS / RSI / TRAIL / TIME STOP
    pnl = Column(Float, index=True)            # realised dollars, gross of fees
    pnl_pct = Column(Float)
    peak_pct = Column(Float, nullable=True)    # best it ever reached, for giveback analysis
    hold_hours = Column(Float)

    opened_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "bot": self.bot,
            "symbol": self.symbol,
            "side": self.side,
            "entry_price": self.entry_price,
            "entry_rsi": self.entry_rsi,
            "entry_trend": self.entry_trend,
            "entry_atr_pct": self.entry_atr_pct,
            "qty": self.qty,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "pnl": self.pnl,
            "pnl_pct": self.pnl_pct,
            "peak_pct": self.peak_pct,
            "hold_hours": self.hold_hours,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
        }


class SweepProposal(Base):
    """Proposed transfer of platform profit into Alpaca trading capital.

    No API deposit is called. User funds in Alpaca UI / bank, then marks funded.
    Workflow: proposed → approved → funded → applied
    """
    __tablename__ = "sweep_proposals"

    id = Column(Integer, primary_key=True, index=True)
    amount = Column(Float)  # USD to transfer
    status = Column(String, default="proposed", index=True)
    # proposed | approved | funded | applied | cancelled | rejected

    # Snapshot of calculator inputs at proposal time (for audit)
    gross_platform = Column(Float)  # Sum of platform_amount before reserves
    tax_reserve = Column(Float)  # Amount held for taxes
    business_reserve = Column(Float)  # Amount held for operations buffer
    already_swept = Column(Float)  # Σ funded SweepProposals to subtract
    free_cash = Column(Float)  # free_cash = gross - tax - already_swept - biz_reserve
    rules_snapshot = Column(JSON)  # The rule set (min_transfer, max_pct, etc.) used

    # Status timestamps
    proposed_at = Column(DateTime, default=datetime.utcnow, index=True)
    approved_at = Column(DateTime, nullable=True)
    funded_at = Column(DateTime, nullable=True)  # User confirms ACH landed
    applied_at = Column(DateTime, nullable=True)  # base_capital updated
    cancelled_at = Column(DateTime, nullable=True)

    notes = Column(Text, nullable=True)
    target_bot_name = Column(String, nullable=True)  # e.g. "prop_bot"

    def to_dict(self):
        return {
            "id": self.id,
            "amount": self.amount,
            "status": self.status,
            "gross_platform": self.gross_platform,
            "tax_reserve": self.tax_reserve,
            "business_reserve": self.business_reserve,
            "already_swept": self.already_swept,
            "free_cash": self.free_cash,
            "rules_snapshot": self.rules_snapshot,
            "proposed_at": self.proposed_at.isoformat() if self.proposed_at else None,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "funded_at": self.funded_at.isoformat() if self.funded_at else None,
            "applied_at": self.applied_at.isoformat() if self.applied_at else None,
            "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
            "notes": self.notes,
            "target_bot_name": self.target_bot_name,
        }


class SweepAuditLog(Base):
    """Append-only log of every calculator run and status change."""
    __tablename__ = "sweep_audit_log"

    id = Column(Integer, primary_key=True, index=True)
    event = Column(String, index=True)  # calculated | proposed | approved | funded | applied | rejected | cancelled
    proposal_id = Column(Integer, ForeignKey("sweep_proposals.id"), nullable=True, index=True)
    detail = Column(JSON)  # Full context of the event
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "event": self.event,
            "proposal_id": self.proposal_id,
            "detail": self.detail,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CryptoRSIState(Base):
    """Track RSI state machine per symbol for tiered entry discipline.

    Replaces static RSI thresholds with event-driven entry: only enter when RSI
    has ENTERED the oversold zone (10-30), then bounces upward.

    States:
    - entered_oversold=False, armed_rsi=None: WATCH (RSI > 30 or never entered)
    - entered_oversold=True, armed_rsi!=None: ARM (in 10-30, waiting for bounce)
    - Entry triggers when: entered_oversold=True + RSI > armed_rsi + vol + candle
    - Reset when: RSI > 50 (forces fresh cycle)
    """
    __tablename__ = "crypto_rsi_state"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, unique=True, index=True)  # "BTC/USD", "ETH/USD", etc.
    entered_oversold = Column(Boolean, default=False)  # Has RSI dipped to 10-30?
    armed_rsi = Column(Float, nullable=True)  # RSI value when entered oversold
    last_rsi = Column(Float, nullable=True)  # Previous cycle RSI (for recovery check)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "symbol": self.symbol,
            "entered_oversold": self.entered_oversold,
            "armed_rsi": self.armed_rsi,
            "last_rsi": self.last_rsi,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CryptoSupplementalCapital(Base):
    """Earnings transferred from prop_bot to crypto bot for compounded trading.

    Tracks capital flow from prop_bot's profit-taking to crypto_bot's trading pool.
    Crypto bot reads total = (Coinbase balance + supplemental pool) and trades with full amount.
    """
    __tablename__ = "crypto_supplemental_capital"

    id = Column(Integer, primary_key=True, index=True)
    amount_usd = Column(Float)  # USD amount transferred
    source = Column(String, default="prop_bot_earnings", index=True)  # tracking origin
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CryptoTradeLog(Base):
    """Comprehensive trade instrumentation for validating state machine effectiveness.

    Captures full lifecycle: ARM → ENTER → EXIT with all metrics needed to measure
    if the new event-driven entry logic improves expectancy vs. old threshold-based approach.

    Strategy version protection: every entry is tagged with strategy_version to prevent
    accidentally mixing old threshold-based data with new RSI state machine data.

    Metrics tracked:
    - ARM state: RSI progression, entered_oversold flag
    - ENTRY: RSI, volume ratio, candle confirmation, position size
    - EXIT: reason, P&L (gross/fees/net), time held
    - Risk metrics: MAE (max adverse excursion), MFE (max favorable excursion)
    - Exit performance: partial-exit fills, trailing-stop effectiveness
    - Non-trades: WATCH, ARM with no recovery, rejected entries (for state machine validation)
    """
    __tablename__ = "crypto_trade_log"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True)  # "BTC/USD", "ETH/USD", etc.
    strategy_version = Column(String, default="RSI_STATE_MACHINE_V1", index=True)  # Prevents data mixing
    trade_id = Column(String, unique=True, index=True)  # UUID for deduplication
    event_type = Column(String, index=True)  # "WATCH", "ARM", "RECOVERY_REJECTED", "ENTER", "EXIT"

    # Timeline
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    armed_at = Column(DateTime, nullable=True, index=True)  # When RSI entered 10-30
    entered_at = Column(DateTime, nullable=True, index=True)  # When trade opened
    exit_at = Column(DateTime, nullable=True, index=True)  # When position closed

    # RSI progression (critical for state machine validation)
    rsi_before_arm = Column(Float, nullable=True)  # RSI before oversold zone
    rsi_arm = Column(Float, nullable=True)  # RSI when armed (entered 10-30)
    rsi_low = Column(Float, nullable=True)  # Lowest RSI during hold
    rsi_at_recovery = Column(Float, nullable=True)  # RSI when recovery started
    rsi_at_entry = Column(Float, nullable=True)  # RSI at actual entry
    entered_oversold = Column(Boolean, default=False)  # Did RSI genuinely enter 10-30?

    # Entry confirmation metrics
    volume_ratio_at_entry = Column(Float, nullable=True)  # Volume spike ratio
    candle_confirmation = Column(Float, nullable=True)  # Close position in upper half (0-1)

    # Position details
    entry_price = Column(Float, nullable=True)
    position_size = Column(Float, nullable=True)
    atr_at_entry = Column(Float, nullable=True)  # Volatility at entry
    atr_stop = Column(Float, nullable=True)  # Stop price derived from ATR
    atr_target_1 = Column(Float, nullable=True)  # 1st target (ATR-based)
    atr_target_2 = Column(Float, nullable=True)  # 2nd target (ATR-based)
    atr_target_3 = Column(Float, nullable=True)  # 3rd target (ATR-based)

    # Exit details
    exit_price = Column(Float, nullable=True)
    exit_reason = Column(String, nullable=True)  # "TIER1", "TIER2", "TIER3", "STOP_LOSS", "RSI_EXIT", "POSITION_ALREADY_EXISTS", "VOLUME_INSUFFICIENT", "CANDLE_FAILED", etc.
    rejection_reason = Column(String, nullable=True)  # For non-entry events: why was entry rejected

    # P&L accounting (gross → fees → net)
    gross_pnl = Column(Float, nullable=True)  # Price difference × quantity
    fees_usd = Column(Float, nullable=True)  # Transaction fees
    net_pnl = Column(Float, nullable=True)  # Gross P&L - fees
    net_pnl_pct = Column(Float, nullable=True)  # Net P&L as percentage

    # Risk metrics
    max_adverse_excursion = Column(Float, nullable=True)  # Worst drawdown during hold
    max_favorable_excursion = Column(Float, nullable=True)  # Best profit during hold
    time_held_minutes = Column(Integer, nullable=True)

    # Exit performance details
    partial_exit_count = Column(Integer, default=0)
    partial_exit_prices = Column(JSON, nullable=True)  # List of partial exit prices
    partial_exit_quantities = Column(JSON, nullable=True)  # Quantities exited at each tier
    trailing_stop_triggered = Column(Boolean, default=False)
    trailing_stop_price = Column(Float, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "strategy_version": self.strategy_version,
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "rsi_progression": {
                "before_arm": self.rsi_before_arm,
                "at_arm": self.rsi_arm,
                "low": self.rsi_low,
                "at_recovery": self.rsi_at_recovery,
                "at_entry": self.rsi_at_entry,
                "entered_oversold": self.entered_oversold,
            },
            "entry_confirmation": {
                "volume_ratio": self.volume_ratio_at_entry,
                "candle_position": self.candle_confirmation,
            },
            "position": {
                "entry_price": self.entry_price,
                "size": self.position_size,
                "atr_at_entry": self.atr_at_entry,
                "stop": self.atr_stop,
                "targets": [self.atr_target_1, self.atr_target_2, self.atr_target_3],
            },
            "exit": {
                "price": self.exit_price,
                "reason": self.exit_reason,
                "timestamp": self.exit_at.isoformat() if self.exit_at else None,
            },
            "pnl": {
                "gross": self.gross_pnl,
                "fees": self.fees_usd,
                "net": self.net_pnl,
                "net_pct": self.net_pnl_pct,
            },
            "risk_metrics": {
                "max_adverse_excursion": self.max_adverse_excursion,
                "max_favorable_excursion": self.max_favorable_excursion,
                "time_held_minutes": self.time_held_minutes,
            },
            "rejection_reason": self.rejection_reason,
        }


class BankTransferLog(Base):
    """Audit trail for automated bank transfers between Alpaca and Coinbase."""
    __tablename__ = "bank_transfer_logs"

    id = Column(Integer, primary_key=True, index=True)
    transfer_id = Column(String, index=True)  # Unique ID for tracking related steps
    step = Column(String, index=True)  # "alpaca_withdrawal_initiated", "coinbase_deposit_initiated", etc.
    amount_usd = Column(Float)
    external_id = Column(String, nullable=True)  # ID from Alpaca or Coinbase API
    status = Column(String, default="pending")  # pending, processing, completed, failed
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    error_message = Column(Text, nullable=True)


class HermesSession(Base):
    """Tracks Hermes Agent sessions for autonomy management."""
    __tablename__ = "hermes_sessions"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, unique=True, index=True)
    status = Column(String, default="active")  # active, idle, error, closed
    started_at = Column(DateTime, default=datetime.utcnow, index=True)
    last_activity = Column(DateTime, default=datetime.utcnow, index=True)
    message_count = Column(Integer, default=0)
    extra_data = Column(JSON, nullable=True)
    closed_at = Column(DateTime, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_activity": self.last_activity.isoformat() if self.last_activity else None,
            "message_count": self.message_count,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
        }


class TelegramMessage(Base):
    """Audit trail for Telegram bot messages."""
    __tablename__ = "telegram_messages"

    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(String, index=True)
    message_id = Column(String, nullable=True, index=True)
    text = Column(Text)
    message_type = Column(String, index=True)  # status, alert, command, report
    status = Column(String, default="sent")  # sent, failed, pending
    sent_at = Column(DateTime, default=datetime.utcnow, index=True)
    extra_data = Column(JSON, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "message_type": self.message_type,
            "status": self.status,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
        }


class CryptoBacktestRun(Base):
    """One coin's result from one run of crypto_selection_backtest.py -
    persisted so the automatic coin-exclusion rule (see
    crypto_family_tree_bot.py's EXCLUDED_COINS / auto-exclusion check) can
    look at a coin's ROI across multiple recent runs rather than reacting
    to a single, possibly noisy, 30-day window. Also backs the manual
    /crypto-selection-backtest-view page's history."""
    __tablename__ = "crypto_backtest_runs"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(String, index=True)
    run_at = Column(DateTime, default=datetime.utcnow, index=True)
    num_trades = Column(Integer)
    win_rate = Column(Float)
    roi_pct_of_spend = Column(Float)


class AlpacaBacktestRun(Base):
    """The Alpaca/stock-side counterpart to CryptoBacktestRun above - one
    symbol's result from one run of alpaca_selection_backtest.py,
    persisted so prop_bot.py's automatic symbol-exclusion rule can look at
    a symbol's ROI across multiple recent runs rather than reacting to a
    single, possibly noisy, 30-day window. product_id here holds the real
    ticker (e.g. "USO"), matching alpaca_selection_backtest.py's own
    field name, not a futures contract code."""
    __tablename__ = "alpaca_backtest_runs"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(String, index=True)
    run_at = Column(DateTime, default=datetime.utcnow, index=True)
    num_trades = Column(Integer)
    win_rate = Column(Float)
    roi_pct_of_spend = Column(Float)


class BotStatus(Base):
    """Snapshots of trading bot performance metrics."""
    __tablename__ = "bot_statuses"

    id = Column(Integer, primary_key=True, index=True)
    bot_name = Column(String, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    cash_available = Column(Float)
    daily_pnl = Column(Float)
    weekly_pnl = Column(Float, default=0.0)
    monthly_pnl = Column(Float, default=0.0)
    win_rate = Column(Float, default=0.0)
    trade_count = Column(Integer, default=0)
    win_count = Column(Integer, default=0)
    loss_count = Column(Integer, default=0)
    avg_win = Column(Float, default=0.0)
    avg_loss = Column(Float, default=0.0)
    open_positions_count = Column(Integer, default=0)
    errors = Column(JSON, nullable=True)  # List of error messages
    extra_data = Column(JSON, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "bot_name": self.bot_name,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "cash_available": self.cash_available,
            "daily_pnl": self.daily_pnl,
            "weekly_pnl": self.weekly_pnl,
            "monthly_pnl": self.monthly_pnl,
            "win_rate": self.win_rate,
            "trade_count": self.trade_count,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
        }


class CryptoCoinTradeHistory(Base):
    """One row per completed round-trip trade in the crypto family tree
    (crypto_family_tree_bot.py) - a real sell, with what it made or lost.

    Per the account owner's explicit request: since branches switch coins
    over time and different branches can independently trade the SAME
    coin at different points, this is scoped by product_id (not by
    branch), so buying SOL back after having sold it before picks up
    right where its history left off - "the third time he bought Sol he
    sold it for this price, and so far the profit has been X" - rather
    than starting a fresh, disconnected count each time some branch
    happens to hold it. Deliberately append-only and never deleted, same
    reasoning as ClosedTrade above - the value is in the accumulated
    history, and it's cheap to keep all of it.
    """
    __tablename__ = "crypto_coin_trade_history"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(String, index=True)
    bot_name = Column(String, index=True)
    entry_price = Column(Float)
    exit_price = Column(Float)
    qty = Column(Float)
    pnl = Column(Float, index=True)
    exit_reason = Column(String)
    opened_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "product_id": self.product_id,
            "bot_name": self.bot_name,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "qty": self.qty,
            "pnl": self.pnl,
            "exit_reason": self.exit_reason,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
        }


class CryptoActivityEvent(Base):
    """One row per real, visible thing the family-tree bot just did - a
    buy, a sell, a new branch spawning, a reinforcement seed landing
    somewhere. Built per the account owner's explicit request: the
    dashboard showed static balances and positions, but nothing that let
    them actually SEE the bot working in real time the way Railway's own
    logs do, without digging through Railway. Deliberately a separate,
    dedicated table from CryptoCoinTradeHistory (which only ever records
    a completed SELL's P&L) - this one is a real, append-only activity
    log covering every visible event type, not just closed trades.
    `message` is the same human-readable text already written to the
    real Railway logs at the moment each event happens, so the dashboard
    feed can never say something different from what the logs say."""
    __tablename__ = "crypto_activity_events"

    id = Column(Integer, primary_key=True, index=True)
    bot_name = Column(String, index=True)
    product_id = Column(String, index=True)
    event_type = Column(String, index=True)  # BUY | SELL | SPAWN | REINFORCE
    message = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "bot_name": self.bot_name,
            "product_id": self.product_id,
            "event_type": self.event_type,
            "message": self.message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
