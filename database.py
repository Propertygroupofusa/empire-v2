"""Database configuration and initialization"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import NullPool
from sqlalchemy import event
import os
import traceback
import logging

log = logging.getLogger(__name__)

# Try to import greenlet, but don't fail if unavailable
# Use sync_engine_mode as fallback if greenlet isn't available
try:
    import greenlet
    _HAS_GREENLET = True
except ImportError:
    _HAS_GREENLET = False

# Database URL - using SQLite for simplicity, or PostgreSQL if DATABASE_URL is set.
# Railway's Postgres plugin injects a plain postgresql:// URL, which defaults to
# the sync psycopg2 driver — create_async_engine requires an async driver, so
# rewrite the scheme to use asyncpg regardless of what's provided.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./empire.db")
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

# This engine is shared by the main FastAPI event loop AND every bot's own
# background-thread event loop (prop_bot.py opens a brand-new loop every
# single cycle via asyncio.run(); crypto_coinbase_bot.py/notary_bot.py each
# keep one persistent loop for their thread's lifetime). A pooled connection
# checked out on one loop and later handed back out to a call running on a
# different loop is exactly what asyncpg's "attached to a different loop"/
# "unknown protocol state" errors mean - seen in production once PR #109
# started running DB reads/writes from these bot threads every cycle.
# NullPool sidesteps this entirely: every checkout opens a fresh connection
# instead of reusing one tied to a specific loop. These bots cycle every
# 30-60s, so the extra connect overhead is a non-issue - correctness across
# loops matters far more here than pooling a handful of infrequent queries.
# pool_recycle is meaningless with NullPool (nothing is kept around to go
# stale) so it's dropped; pool_pre_ping stays since it's harmless.
_engine_kwargs = {"echo": False, "future": True, "pool_pre_ping": True, "poolclass": NullPool}

# If greenlet is not available, use sync_engine_mode with thread executor
# This allows async operations without requiring greenlet
if not _HAS_GREENLET:
    _engine_kwargs["sync_engine_mode"] = "sync_with_exec"
    import logging
    logging.warning("greenlet not available - using thread executor mode for async DB operations")

if DATABASE_URL.startswith("postgresql+asyncpg://"):
    _engine_kwargs["connect_args"] = {"timeout": 10}

engine = create_async_engine(DATABASE_URL, **_engine_kwargs)

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

        log.info("Starting database initialization...")
        log.info("Calling Base.metadata.create_all()...")

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        log.info("✅ Base.metadata.create_all() completed successfully")
    except Exception:
        log.exception("❌ Base.metadata.create_all() failed")

async def get_db():
    """Get database session"""
    async with AsyncSessionLocal() as session:
        yield session
