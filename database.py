"""Database configuration and models"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, String, Integer, Float, Boolean, DateTime, JSON, Text
from datetime import datetime
import os

# Database URL - using SQLite for simplicity, or PostgreSQL if DATABASE_URL is set
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./empire.db")

# Create async engine
engine = create_async_engine(DATABASE_URL, echo=False, future=True)

# Create session factory
AsyncSessionLocal = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

# Declarative base for models
Base = declarative_base()


class Order(Base):
    """Video order"""
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    order_id = Column(String, unique=True, nullable=False)
    customer_email = Column(String, nullable=False)
    customer_name = Column(String, nullable=False)
    customer_company = Column(String, nullable=True)
    customer_phone = Column(String, nullable=True)
    video_type = Column(String, nullable=False)
    script = Column(Text, nullable=False)
    target_audience = Column(String, nullable=True)
    avatar = Column(String, nullable=False)
    language = Column(String, nullable=False)
    delivery_days = Column(Integer, nullable=False)
    quote_price = Column(Integer, nullable=False)  # in cents
    status = Column(String, default="quote_requested")  # quote_requested, payment_received, video_ready, failed, timeout
    paid = Column(Boolean, default=False)
    stripe_session_id = Column(String, nullable=True)
    video_generation_status = Column(String, default="pending")  # pending, generating, completed, failed, timeout
    video_url = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    metadata = Column(JSON, default={})


class CustomerSubscription(Base):
    """Customer subscription tier"""
    __tablename__ = "customer_subscriptions"

    id = Column(Integer, primary_key=True)
    customer_email = Column(String, unique=True, nullable=False, index=True)
    tier_id = Column(String, nullable=False)
    tier_name = Column(String, nullable=False)
    start_date = Column(DateTime, default=datetime.utcnow)
    current_period_start = Column(DateTime, default=datetime.utcnow)
    current_period_end = Column(DateTime, nullable=False)
    videos_used_this_month = Column(Integer, default=0)
    active = Column(Boolean, default=True)
    stripe_subscription_id = Column(String, nullable=True)
    stripe_customer_id = Column(String, nullable=True)
    payment_status = Column(String, default="pending")  # pending, active, past_due, cancelled
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


async def init_db():
    """Initialize database - create tables if needed"""
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            print("Database tables initialized successfully")
    except Exception as e:
        print(f"Database init error: {e}")


async def get_db():
    """Get database session"""
    async with AsyncSessionLocal() as session:
        yield session
