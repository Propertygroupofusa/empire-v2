"""Database configuration and initialization"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
import os

# Database URL - using SQLite for simplicity, or PostgreSQL if DATABASE_URL is set
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./empire.db")

# Create async engine
engine = create_async_engine(DATABASE_URL, echo=False, future=True)

# Create session factory
AsyncSessionLocal = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

async def init_db():
    """Initialize database - create tables if needed"""
    try:
        async with engine.begin() as conn:
            # Tables would be created here via declarative base
            pass
    except Exception as e:
        print(f"Database init warning: {e}")

async def get_db():
    """Get database session"""
    async with AsyncSessionLocal() as session:
        yield session
