"""Database configuration and initialization"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import NullPool
from sqlalchemy import event, inspect, text
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

# Lazy initialization: don't connect until first use
# This prevents Railway app from crashing on startup if DB is unavailable
engine = None
AsyncSessionLocal = None
Base = declarative_base()

def get_engine():
    """Lazy engine initialization - connect only when needed"""
    global engine
    if engine is None:
        engine = create_async_engine(DATABASE_URL, **_engine_kwargs)
    return engine

def get_session_factory():
    """Lazy session factory initialization"""
    global AsyncSessionLocal
    if AsyncSessionLocal is None:
        AsyncSessionLocal = sessionmaker(
            get_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return AsyncSessionLocal

async def init_db():
    """Initialize database - create tables if needed"""
    try:
        import models  # noqa: F401  (registers model classes on Base.metadata)

        print("[DB] Starting database initialization...")
        print("[DB] Calling Base.metadata.create_all()...")

        db_engine = get_engine()
        async with db_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        print("[DB] ✅ Base.metadata.create_all() completed successfully")
    except Exception as e:
        print(f"[DB] ⚠️  Base.metadata.create_all() failed (non-critical): {e}")
        # Don't crash on DB failure - trading can run without persistent storage


async def ensure_grid_status_schema():
    """Add columns required by the read-only Grid status path first.

    The full model-registry migration can exceed the startup timeout on
    production Postgres. These columns must exist before a full ORM select of
    Grid branches or slices can report live unrealized P&L.
    """
    import models  # noqa: F401  (registers model classes on Base.metadata)

    required_columns = {
        "crypto_grid_branches": ("self_tuned_multiplier",),
        "crypto_grid_slices": ("entry_fee_rate",),
    }
    db_engine = get_engine()

    for table_name, column_names in required_columns.items():
        table = Base.metadata.tables[table_name]
        async with db_engine.begin() as conn:
            table_names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
            if table_name not in table_names:
                continue

            existing_columns = {
                column["name"]
                for column in await conn.run_sync(
                    lambda sync_conn, name=table_name: inspect(sync_conn).get_columns(name)
                )
            }
            for column_name in column_names:
                if column_name in existing_columns:
                    continue
                column = table.c[column_name]
                ddl_type = column.type.compile(dialect=conn.dialect)
                await conn.execute(text(
                    f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {ddl_type}'
                ))
                log.info("Priority migration OK: %s.%s", table_name, column_name)

async def get_db():
    """Get database session"""
    factory = get_session_factory()
    if factory is None:
        return
    async with factory() as session:
        yield session
