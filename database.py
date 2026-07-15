"""Database configuration and initialization"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
import os

# Database URL - using SQLite for simplicity, or PostgreSQL if DATABASE_URL is set
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./empire.db")

# Create async engine
engine = create_async_engine(DATABASE_URL, echo=False, future=True)

# Create session factory
AsyncSessionLocal = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

# Declarative base for all models (models.py imports this)
Base = declarative_base()

async def init_db():
    """Initialize database - create tables if needed"""
    try:
        import models  # noqa: F401  (registers model classes on Base.metadata)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as e:
        print(f"Database init warning: {e}")

async def get_db():
    """Get database session"""
    async with AsyncSessionLocal() as session:
        yield session
