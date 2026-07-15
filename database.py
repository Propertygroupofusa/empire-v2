"""Database configuration and initialization"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
import os

# Database URL - using SQLite for simplicity, or PostgreSQL if DATABASE_URL is set.
# Railway's Postgres plugin injects a plain postgresql:// URL, which defaults to
# the sync psycopg2 driver — create_async_engine requires an async driver, so
# rewrite the scheme to use asyncpg regardless of what's provided.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./empire.db")
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

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
