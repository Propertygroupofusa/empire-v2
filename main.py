"""
PROPERTY GROUP USA — DOCUMENTS PLATFORM BACKEND
=================================================
Full SaaS backend with worker management, client booking,
job matching, payments, admin dashboard, and white label API.

VERSION: v2.3-stable-broker-recovery
Deployed: 2026-08-12 02:20 UTC | Stable redeploy - broker network recovery
"""

from fastapi import FastAPI, HTTPException, Depends, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from sqlalchemy import text, inspect, String, Integer, Enum as SAEnum
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from datetime import datetime
import os
import asyncio
import uvicorn
import logging
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List

# Load .env file to make credentials available to background bots
load_dotenv(override=True)

# CRITICAL: Ensure greenlet is available for SQLAlchemy async support
try:
    import greenlet
    assert greenlet.__version__, "greenlet module loaded"
except (ImportError, AssertionError) as e:
    logging.error(f"FATAL: greenlet not available - async database will fail: {e}")
    raise

from database import init_db, engine
from initialize_bot_worker import initialize_bot_worker


# Pydantic request models for Hermes Phase 1 endpoints
class BotStatusRequest(BaseModel):
    """Request model for recording bot status"""
    bot_name: str
    cash_available: float
    daily_pnl: float
    win_rate: float = 0.0
    trade_count: int = 0
    open_positions: Dict[str, Any] = Field(default_factory=dict)
    errors: List[str] = Field(default_factory=list)


# Load routers gracefully to prevent import errors from crashing startup
routers_to_load = {
    'workers': None,
    'clients': None,
    'jobs': None,
    'bookings': None,
    'payments': None,
    'admin': None,
    'whitelabel': None,
    'auth': None,
    'partners': None,
    'labeling': None,
    'revenue_automation': None,
    'social_dashboard': None,
    'orders': None,
    'subscriptions': None,
    'trading_signals': None,
    'outreach': None,
    'study': None,
    'trading_dashboard': None,
    'support': None,
    'sales': None,
    'alpaca_funding': None,
    'sweep': None,
    'bot_race': None,
    'alpaca_dashboard': None,
    'trading_hub': None,
}

for router_name in routers_to_load:
    try:
        routers_to_load[router_name] = __import__(f'routers.{router_name}', fromlist=[router_name])
    except Exception as e:
        logging.basicConfig(level=logging.INFO)
        logging.warning(f"Failed to import router {router_name}: {e}")

# Extract routers for app registration
workers = routers_to_load['workers']
clients = routers_to_load['clients']
jobs = routers_to_load['jobs']
bookings = routers_to_load['bookings']
payments = routers_to_load['payments']
admin = routers_to_load['admin']
whitelabel = routers_to_load['whitelabel']
auth = routers_to_load['auth']
partners = routers_to_load['partners']
labeling = routers_to_load['labeling']
revenue_automation = routers_to_load['revenue_automation']
social_dashboard = routers_to_load['social_dashboard']
orders = routers_to_load['orders']
subscriptions = routers_to_load['subscriptions']
trading_signals = routers_to_load['trading_signals']
outreach = routers_to_load['outreach']
study = routers_to_load['study']
trading_dashboard = routers_to_load['trading_dashboard']
support = routers_to_load['support']
sales = routers_to_load['sales']
alpaca_funding = routers_to_load['alpaca_funding']
sweep = routers_to_load['sweep']
bot_race = routers_to_load['bot_race']
alpaca_dashboard = routers_to_load['alpaca_dashboard']
trading_hub = routers_to_load['trading_hub']

# Load remaining modules gracefully
payee_router = None
payee_worker = None
try:
    from payee_webhook import router as payee_router, payee_worker
except Exception as e:
    logging.warning(f"Failed to import payee_webhook: {e}")

payroll_router = None
try:
    from paycom_features import router as payroll_router
except Exception as e:
    logging.warning(f"Failed to import paycom_features: {e}")

video_revenue_router = None
try:
    from video_revenue_api import router as video_revenue_router
except Exception as e:
    logging.warning(f"Failed to import video_revenue_api: {e}")

start_daily_publisher = None
try:
    from daily_publisher import start_daily_publisher
except Exception as e:
    logging.warning(f"Failed to import daily_publisher: {e}")

start_daily_brief = None
try:
    from daily_brief import start_daily_brief
except Exception as e:
    logging.warning(f"Failed to import daily_brief: {e}")

health_monitor_service = None
try:
    from health_monitor import start_health_monitor, monitor
    health_monitor_service = start_health_monitor
except Exception as e:
    logging.warning(f"Failed to import health_monitor: {e}")

retention_manager = None
try:
    from data_retention import retention_manager as rm
    retention_manager = rm
except Exception as e:
    logging.warning(f"Failed to import data_retention: {e}")

prop_bot_module = None
try:
    import prop_bot
    prop_bot_module = prop_bot
except Exception as e:
    logging.warning(f"Failed to import prop_bot: {e}")

notary_bot_module = None
try:
    import notary_bot
    notary_bot_module = notary_bot
except Exception as e:
    logging.warning(f"Failed to import notary_bot: {e}")

crypto_coinbase_bot_module = None
try:
    import crypto_coinbase_bot
    crypto_coinbase_bot_module = crypto_coinbase_bot
except Exception as e:
    logging.warning(f"Failed to import crypto_coinbase_bot: {e}")

alpaca_swing_bot_module = None
try:
    import alpaca_swing_bot
    alpaca_swing_bot_module = alpaca_swing_bot
except Exception as e:
    logging.warning(f"Failed to import alpaca_swing_bot: {e}")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pgusa")

# Deployment marker - forces fresh Railway build
_deployment_version = "2026-07-15-stripe-subscriptions"

# Ensure all subscription modules load correctly
try:
    from stripe_subscriptions import setup_stripe_products
    from subscription_tiers import SUBSCRIPTION_TIERS
except Exception as e:
    log.warning(f"Subscription modules pre-check: {e}")


async def create_monitor_tables():
    """Create health monitor tables if they don't exist"""
    # AUTOINCREMENT is SQLite-only syntax; Postgres needs SERIAL. Pick the
    # right primary-key clause for whichever DATABASE_URL is actually in use.
    pk = "SERIAL PRIMARY KEY" if engine.dialect.name == "postgresql" else "INTEGER PRIMARY KEY AUTOINCREMENT"
    async with engine.begin() as conn:
        try:
            await conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS monitor_errors (
                    id {pk},
                    error_type VARCHAR NOT NULL,
                    error_message TEXT NOT NULL,
                    severity VARCHAR,
                    detected_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            log.info("Migration OK: monitor_errors table")
        except Exception as e:
            log.warning(f"Migration skip monitor_errors: {e}")

        try:
            await conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS monitor_fixed_issues (
                    id {pk},
                    issue_name VARCHAR NOT NULL,
                    fixed_at TIMESTAMP,
                    status VARCHAR,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            log.info("Migration OK: monitor_fixed_issues table")
        except Exception as e:
            log.warning(f"Migration skip monitor_fixed_issues: {e}")

        try:
            await conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS monitor_performance (
                    id {pk},
                    metric_data TEXT NOT NULL,
                    checked_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            log.info("Migration OK: monitor_performance table")
        except Exception as e:
            log.warning(f"Migration skip monitor_performance: {e}")

        try:
            await conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS monitor_errors_archive (
                    id {pk},
                    error_type VARCHAR NOT NULL,
                    error_message TEXT NOT NULL,
                    severity VARCHAR,
                    detected_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            log.info("Migration OK: monitor_errors_archive (PERMANENT STORAGE)")
        except Exception as e:
            log.warning(f"Migration skip monitor_errors_archive: {e}")

        try:
            await conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS monitor_fixed_issues_archive (
                    id {pk},
                    issue_name VARCHAR NOT NULL,
                    fixed_at TIMESTAMP,
                    status VARCHAR,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            log.info("Migration OK: monitor_fixed_issues_archive (PERMANENT STORAGE)")
        except Exception as e:
            log.warning(f"Migration skip monitor_fixed_issues_archive: {e}")

        try:
            await conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS monitor_performance_archive (
                    id {pk},
                    metric_data TEXT NOT NULL,
                    checked_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            log.info("Migration OK: monitor_performance_archive (PERMANENT STORAGE)")
        except Exception as e:
            log.warning(f"Migration skip monitor_performance_archive: {e}")

        try:
            await conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS data_retention_log (
                    id {pk},
                    action VARCHAR NOT NULL,
                    table_name VARCHAR,
                    records_archived INTEGER,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            log.info("Migration OK: data_retention_log (PERMANENT STORAGE)")
        except Exception as e:
            log.warning(f"Migration skip data_retention_log: {e}")


async def run_migrations():
    """Add any columns models.py declares that the real tables are
    actually missing - safe to run every startup (skips whatever already
    exists).

    Discovered live in production: the real "workers" table was missing
    even "name" - one of the ORIGINAL base columns, not something added
    this session (PR #72). Root cause: these tables were created some
    other way before their ORM models existed (e.g. workers predates the
    notary work via an unrelated /workers/payroll feature), and nothing
    ever surfaced the drift because routers/workers.py, routers/jobs.py,
    etc. were no-op empty stubs until the notary marketplace work - the
    first code that ever actually queried these tables for real.

    Confirmed the SAME issue hits "jobs" too (column jobs.job_type does
    not exist, breaking notary_bot.py's matching cycle every 60s in
    production) - so this now covers every model introduced alongside
    Worker in that same PR (#70), not just Worker, since any of them
    could have the same kind of pre-existing-table drift.

    Model-driven (iterates each table's columns) rather than a manually
    maintained list of column names/types, specifically so this doesn't
    only patch the columns anyone remembered to add here - it catches ANY
    drift between the ORM model and the real table, present or future.
    Also dialect-agnostic (SQLAlchemy's inspector, not raw SQLite PRAGMA),
    so it actually works on Postgres - production.

    The set of TABLES is now derived the same way: every table registered
    on Base.metadata, not a hand-picked tuple. The tuple was the actual
    bug. create_all (database.py) creates tables that do not exist yet,
    but it never adds a column to a table that already exists - so a new
    field on any pre-existing table is invisible to the real table unless
    it is migrated HERE, and every write then fails with "column does not
    exist". Two separate outages came from a model being absent from that
    tuple: bot_positions.peak_pct, and then payments.stripe_payout_id,
    which errored the payout cycle on every pass. Enumerating the registry
    means adding a model can no longer silently opt out of migration."""
    import models  # noqa: F401  (registers every model on Base.metadata)
    from database import Base

    # CRITICAL: Ensure crypto_rsi_state table exists for bot RSI state machine
    async with engine.begin() as conn:
        try:
            existing_tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
            if "crypto_rsi_state" not in existing_tables:
                log.info("Migration: Creating missing crypto_rsi_state table...")
                try:
                    if engine.dialect.name == "postgresql":
                        await conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS crypto_rsi_state (
                                id SERIAL PRIMARY KEY,
                                symbol VARCHAR(50) UNIQUE NOT NULL,
                                entered_oversold BOOLEAN DEFAULT FALSE,
                                armed_rsi FLOAT,
                                last_rsi FLOAT,
                                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                            )
                        """))
                        try:
                            await conn.execute(text("CREATE INDEX idx_crypto_rsi_state_symbol ON crypto_rsi_state(symbol)"))
                        except:
                            pass
                        try:
                            await conn.execute(text("CREATE INDEX idx_crypto_rsi_state_updated_at ON crypto_rsi_state(updated_at)"))
                        except:
                            pass
                    else:
                        await conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS crypto_rsi_state (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                symbol TEXT UNIQUE NOT NULL,
                                entered_oversold INTEGER DEFAULT 0,
                                armed_rsi REAL,
                                last_rsi REAL,
                                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                            )
                        """))
                    await conn.commit()
                    log.info("✅ Migration OK: crypto_rsi_state table created")
                except Exception as migration_err:
                    log.error(f"❌ Migration FAILED - crypto_rsi_state creation: {type(migration_err).__name__}: {migration_err}")
                    # Don't raise - bot can work without persistence, just won't save RSI state across restarts
            else:
                log.info("✅ Migration OK: crypto_rsi_state table already exists")

            # CRITICAL: Fix workers table auto-increment on PostgreSQL (bot_autoscaler needs this)
            if engine.dialect.name == "postgresql":
                try:
                    max_id_result = await conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM workers"))
                    max_id = max_id_result.scalar() or 0
                    await conn.execute(text("DROP SEQUENCE IF EXISTS workers_id_seq CASCADE"))
                    await conn.execute(text(f"CREATE SEQUENCE workers_id_seq START {max_id + 1}"))
                    await conn.execute(text("ALTER SEQUENCE workers_id_seq OWNED BY workers.id"))
                    try:
                        await conn.execute(text("ALTER TABLE workers ALTER COLUMN id DROP DEFAULT"))
                    except:
                        pass
                    await conn.execute(text("ALTER TABLE workers ALTER COLUMN id SET DEFAULT nextval('workers_id_seq')"))
                    log.info(f"✅ workers table auto-increment fixed (max_id: {max_id}, starts: {max_id + 1})")
                except Exception as e:
                    log.debug(f"Workers auto-increment (may already exist): {e}")
        except Exception as e:
            log.warning(f"⚠️ Initial migration block (crypto_rsi_state / workers seq): {type(e).__name__}: {e}")
            # Don't re-raise - continue with main migration loop. The first async with block can fail
            # without blocking the rest of startup. Bot will just skip some optional persistence features.

    # Counters exist so the run reports what it DID, not just that it ran.
    # Both outages so far were invisible in the logs: nothing announced that
    # payments was never considered, because a table absent from the old
    # tuple produced no output at all. Silence read exactly like success.
    scanned = added = converted = failed = sequenced = 0

    async with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            table_name = table.name
            try:
                raw_columns = await conn.run_sync(
                    lambda sync_conn, t=table_name: inspect(sync_conn).get_columns(t)
                )
                existing_columns = {c["name"]: c["type"] for c in raw_columns}
            except Exception as e:
                log.warning(f"Migration: could not inspect {table_name} table columns: {e}")
                failed += 1
                continue
            scanned += 1

            # ── auto-increment repair ───────────────────────────────────
            #
            # Confirmed live: inserting a Worker died with
            #
            #   asyncpg.exceptions.NotNullViolationError: null value in
            #   column "id" of relation "workers" violates not-null
            #   constraint
            #
            # The model declares id as an Integer primary key, so
            # SQLAlchemy omits it from the INSERT and expects the database
            # to generate it (note the RETURNING workers.id). But the real
            # workers table was created outside the ORM as a plain INTEGER
            # PRIMARY KEY - no SERIAL, no IDENTITY, no DEFAULT - so nothing
            # generates a value and the NOT NULL implied by PRIMARY KEY
            # rejects the row. Every bot-worker insert failed this way,
            # from initialize_bot and from the autoscaler alike.
            #
            # Same drift class this function already exists for, so it is
            # repaired the same way: generically, across every table, not
            # just the one that happened to surface it. clients/jobs/
            # bookings all have Integer primary keys and could have been
            # created the same way.
            #
            # Postgres only - SQLite makes INTEGER PRIMARY KEY an alias for
            # rowid and assigns automatically, which is exactly why this
            # bug is invisible in a SQLite test.
            if conn.dialect.name == "postgresql":
                by_name = {c["name"]: c for c in raw_columns}
                for column in table.primary_key.columns:
                    info = by_name.get(column.name)
                    if info is None or not isinstance(column.type, Integer):
                        continue
                    # AGGRESSIVE: always try to fix Integer PKs on Postgres.
                    # Don't skip even if info.get("default") exists - it might
                    # be broken/malformed. Always rebuild from scratch.
                    seq = f"{table_name}_{column.name}_seq"
                    try:
                        # Step 1: Kill any existing sequence (may be broken)
                        await conn.execute(text(f'DROP SEQUENCE IF EXISTS "{seq}" CASCADE'))

                        # Step 2: Find current max ID so we don't collide
                        max_val = 0
                        try:
                            result = await conn.execute(text(
                                f'SELECT COALESCE(MAX("{column.name}"), 0) FROM "{table_name}"'
                            ))
                            max_val = result.scalar() or 0
                        except Exception as query_err:
                            log.debug(f"Could not query max({column.name}) on {table_name}: {query_err}")

                        # Step 3: Create fresh sequence
                        await conn.execute(text(f'CREATE SEQUENCE "{seq}" START {max_val + 1}'))

                        # Step 4: Own it
                        await conn.execute(text(
                            f'ALTER SEQUENCE "{seq}" OWNED BY "{table_name}"."{column.name}"'))

                        # Step 5: Drop existing DEFAULT if present (replace it)
                        try:
                            await conn.execute(text(
                                f'ALTER TABLE "{table_name}" ALTER COLUMN "{column.name}" '
                                f'DROP DEFAULT'))
                        except:
                            pass  # OK if no DEFAULT existed yet

                        # Step 6: Set new DEFAULT that uses the sequence
                        await conn.execute(text(
                            f'ALTER TABLE "{table_name}" ALTER COLUMN "{column.name}" '
                            f'SET DEFAULT nextval(\'{seq}\')'))

                        log.info(f"Migration OK: {table_name}.{column.name} "
                                 f"auto-increment repaired via {seq} (max existing: {max_val}, "
                                 f"sequence starts: {max_val + 1})")
                        sequenced += 1
                    except Exception as e:
                        log.error(f"Migration FAILED {table_name}.{column.name} "
                                  f"auto-increment: [{type(e).__name__}] {e}", exc_info=True)
                        failed += 1

            for column in table.columns:
                if column.name not in existing_columns:
                    try:
                        ddl_type = column.type.compile(dialect=conn.dialect)
                        # Always added nullable regardless of the model's own
                        # nullable=False, even for required-looking columns like
                        # name/email - a NOT NULL ALTER TABLE ADD COLUMN without a
                        # default fails outright on Postgres if the table already
                        # has rows, which is exactly the scenario this exists for.
                        await conn.execute(text(f'ALTER TABLE {table_name} ADD COLUMN "{column.name}" {ddl_type}'))
                        log.info(f"Migration OK: {table_name}.{column.name}")
                        added += 1
                    except Exception as e:
                        # WARNING, not debug. Reaching here means the column
                        # is genuinely absent from the real table AND the
                        # ALTER to add it failed - so every write touching
                        # that column will now fail, which is precisely the
                        # payments/bot_positions outage. At debug level that
                        # never appears in Railway's logs, so the migration
                        # would report itself as fine while leaving the
                        # column missing.
                        log.warning(f"Migration FAILED {table_name}.{column.name}: "
                                    f"[{type(e).__name__}] {e}")
                        failed += 1
                    continue

                # Type drift, not just missing-column drift: confirmed live
                # in production on jobs.status - the real column was a
                # native Postgres ENUM type ("jobstatus"), not the plain
                # VARCHAR the model declares. Some earlier version of the
                # model must have used SQLAlchemy's Enum(...) (which
                # auto-creates that type) before being simplified to plain
                # String, but the existing production table's column was
                # never migrated to match - comparing an enum column to a
                # string bind param fails outright ("operator does not
                # exist: jobstatus = character varying"). Checked generally
                # across every String column on all 4 models here, not just
                # jobs.status, since Worker/Client/Booking all have their
                # own "status" columns from the same era and could have the
                # identical drift (e.g. routers/bookings.py already filters
                # on Booking.status == status the same way).
                #
                # SAEnum is excluded, and that exclusion is load-bearing now
                # that this loop covers every table instead of four.
                # sqlalchemy.Enum SUBCLASSES String, so isinstance(...,
                # String) is True for a column the model declares as
                # Enum(SomePyEnum) - and such a column is SUPPOSED to be a
                # native PG enum in the database. Without this guard the
                # widened loop would "fix" sales_leads.source,
                # sales_leads.status and sales_outreach.outreach_type by
                # converting three deliberately-enum columns to VARCHAR:
                # a destructive change, applied to exactly the columns where
                # model and database already agree. The rule this encodes is
                # "model says plain string, database says enum", not
                # "database says enum".
                if (isinstance(column.type, String)
                        and not isinstance(column.type, SAEnum)
                        and isinstance(existing_columns[column.name], PGEnum)):
                    try:
                        await conn.execute(text(
                            f'ALTER TABLE {table_name} ALTER COLUMN "{column.name}" TYPE VARCHAR USING "{column.name}"::text'
                        ))
                        log.info(f"Migration OK: {table_name}.{column.name} converted from enum to VARCHAR")
                        converted += 1
                    except Exception as e:
                        log.warning(f"Migration FAILED {table_name}.{column.name} type fix: "
                                    f"[{type(e).__name__}] {e}")
                        failed += 1

    # One line that is always emitted, including on a clean no-op run. This
    # is the line to grep for in Railway after a deploy: "tables=26" proves
    # the registry-wide sweep actually happened, and failed=0 proves nothing
    # was left broken. A deploy where this line is missing entirely means
    # run_migrations raised before finishing - main.py catches that and logs
    # "Migrations failed", which is easy to miss among startup noise.
    log.info(f"Migration summary: tables={scanned} columns_added={added} "
             f"enum_conversions={converted} autoincrement_fixed={sequenced} "
             f"failures={failed}")


async def validate_foreign_keys():
    """Validate that all required tables exist and have correct foreign key constraints.

    NotaryPayout table in particular sometimes fails to create on PostgreSQL due to
    foreign key constraint ordering issues. This function detects and repairs such issues.
    """
    from database import Base
    import models  # noqa: F401

    if engine.dialect.name != "postgresql":
        return  # Foreign key checks are for PostgreSQL only

    async with engine.begin() as conn:
        inspector = inspect.__call__(conn.sync_conn)
        existing_tables = {t.lower() for t in inspector.get_table_names()}

        # Check each model's table exists
        missing_tables = []
        for table in Base.metadata.sorted_tables:
            if table.name.lower() not in existing_tables:
                missing_tables.append(table.name)

        if missing_tables:
            log.warning(f"Foreign key validation: Missing tables: {missing_tables}")
            # Try to create missing tables explicitly
            for table in Base.metadata.sorted_tables:
                if table.name.lower() in {t.lower() for t in missing_tables}:
                    try:
                        await conn.run_sync(lambda sync_conn, t=table: t.create(sync_conn, checkfirst=True))
                        log.info(f"Foreign key validation: Created table {table.name}")
                    except Exception as e:
                        log.warning(f"Foreign key validation: Could not create {table.name}: {e}")
        else:
            log.info(f"Foreign key validation: All {len(Base.metadata.sorted_tables)} tables exist")

        # Verify NotaryPayout specifically
        if "notary_payouts" in existing_tables:
            constraints = inspector.get_foreign_keys("notary_payouts")
            if constraints:
                log.info(f"Foreign key validation: notary_payouts has {len(constraints)} FK constraints")
            else:
                log.warning("Foreign key validation: notary_payouts exists but has NO foreign key constraints - this is unexpected")


# ── Bot Earnings System ──────────────────────────────────────

async def initialize_bot():
    """Initialize bot workers with Stripe Connect accounts.

    Raises on failure. It used to swallow its own exceptions into a
    warning while the caller logged "Bot worker initialized" regardless,
    which is how the failure below survived unnoticed for months."""
    from database import AsyncSessionLocal
    from models import Worker
    # bcrypt's own API, not passlib's CryptContext. passlib was never in
    # requirements.txt, so this import raised ModuleNotFoundError and took
    # the whole function down before a single worker was created - and it
    # would not have worked if added, because passlib's bcrypt backend
    # reads bcrypt.__about__.__version__ which bcrypt removed in 4.x:
    #
    #   AttributeError: module 'bcrypt' has no attribute '__about__'
    #
    # against the pinned bcrypt==5.0.0. worker_auth.py already hit this
    # and documented it; reusing its helper rather than adding a fourth
    # copy of the same two lines.
    from worker_auth import hash_password
    from sqlalchemy import select
    import stripe

    stripe_key = os.getenv("STRIPE_SECRET_KEY")
    if stripe_key:
        stripe.api_key = stripe_key

    async with AsyncSessionLocal() as session:
        # Check existing bots
        result = await session.execute(
            select(Worker).where(Worker.email.like("%bot%pgusa.local"))
        )
        existing_bots = result.scalars().all()

        # If no bots exist, create initial fleet of 2
        if not existing_bots:
            created = 0
            for i in range(1, 3):
                bot_email = f"bot{i if i > 1 else ''}@pgusa.local"
                worker = Worker(
                    email=bot_email,
                    name=f"Job Bot {i}",
                    status="active",
                    password_hash=hash_password("auto_bot_password_123"),
                )
                session.add(worker)
                await session.flush()
                created += 1
                log.info(f"🤖 Bot worker created: {bot_email}")

                if stripe_key:
                    try:
                        account = stripe.Account.create(
                            type="express",
                            email=bot_email,
                            capabilities={"transfers": {"requested": True}},
                        )
                        worker.stripe_account_id = account.id
                        log.info(f"💳 Stripe Connect account created: {account.id}")
                    except Exception as e:
                        log.warning(f"Stripe Connect setup failed for {bot_email}: {e}")

            await session.commit()
            # Count what was actually created, not len(range(1, 3)) - that
            # is the constant 2 regardless of what happened in the loop.
            log.info(f"✅ Initialized {created} bot workers")
        else:
            log.info(f"✅ {len(existing_bots)} bot workers already exist")


async def start_job_bot():
    """DEMO ONLY - off unless DEMO_JOB_BOT_ENABLED=true.

    Claims jobs, marks them complete two seconds later, and books a
    payment. It is a demo of the marketplace mechanics, not the
    marketplace: no notarization actually happens.

    WHY IT IS GATED
    ---------------
    It selects on `Job.status == "requested"` with NO filter on `paid`,
    and then sets `job.paid = True` itself. The real notarization flow
    depends on that flag: routers/jobs.py opens a job with paid=False and
    routes the client to Stripe checkout, and notary_bot.py plus
    POST /{job_id}/match both refuse to match a job that is not paid.
    This loop bypasses that gate and overwrites the flag.

    So a real customer submitting a notarization request would have their
    job claimed within 10 seconds - before they entered a card - stamped
    paid, marked completed 2 seconds later, and turned into a payout
    obligation for the full price. No work performed, no money collected.

    That was harmless only because two other bugs kept it inert: there
    were no bot workers (passlib, #129) and no jobs (nothing creates them
    but real intake and a seeder nothing runs). #129 and #131 removed the
    first protection, so this needs a deliberate one.

    Turning it on is safe when the only jobs in the table came from
    seed_bot_jobs.py. It is not safe while real client intake is open."""
    if os.getenv("DEMO_JOB_BOT_ENABLED", "false").strip().lower() != "true":
        log.info("Demo job bot disabled (DEMO_JOB_BOT_ENABLED not set to true) "
                 "- jobs will not be auto-claimed or auto-completed")
        return

    try:
        from database import AsyncSessionLocal
        from models import Job, Worker, Payment
        from sqlalchemy import select
        import uuid

        log.warning("🚀 Demo Job Bot ARMED - DEMO_JOB_BOT_ENABLED=true. It will "
                    "claim ANY job in 'requested' status, including unpaid real "
                    "client jobs, mark it paid and completed, and book a payout.")

        while True:
            try:
                async with AsyncSessionLocal() as session:
                    # Get ALL bot workers (any worker with email containing "bot")
                    bot_result = await session.execute(
                        select(Worker).where(Worker.email.like("%bot%pgusa.local"))
                    )
                    bot_workers = bot_result.scalars().all()

                    if not bot_workers:
                        log.warning("No bot workers found, skipping cycle")
                        await asyncio.sleep(10)
                        continue

                    log.info(f"🤖 Running {len(bot_workers)} bot workers")

                    # Get all open jobs
                    result = await session.execute(
                        select(Job).where(Job.status == "requested")
                    )
                    available_jobs = result.scalars().all()

                    if available_jobs:
                        log.info(f"📋 Found {len(available_jobs)} available jobs for {len(bot_workers)} bots")

                        # Distribute jobs among available bots
                        jobs_per_bot = max(1, len(available_jobs) // len(bot_workers))
                        job_idx = 0

                        for bot_worker in bot_workers:
                            for _ in range(jobs_per_bot):
                                if job_idx >= len(available_jobs):
                                    break

                                job = available_jobs[job_idx]
                                job_idx += 1

                                try:
                                    # Claim the job
                                    job.status = "matched"
                                    job.worker_id = bot_worker.id
                                    job.paid = True
                                    await session.commit()
                                    log.info(f"✅ {bot_worker.email} CLAIMED: {job.description} (${job.price:.2f})")

                                    # Complete the job after a moment
                                    await asyncio.sleep(2)
                                    job.status = "completed"
                                    job.completed_at = datetime.utcnow()
                                    await session.commit()

                                    # Create payment for bot
                                    payment = Payment(
                                        id=str(uuid.uuid4()),
                                        job_id=str(job.id),
                                        worker_id=str(bot_worker.id),
                                        client_id=job.client_id,
                                        gross_amount=job.price * 1.2,
                                        worker_amount=job.price,
                                        platform_amount=job.price * 0.2,
                                        payout_status="pending",
                                    )
                                    session.add(payment)
                                    await session.commit()
                                    log.info(f"💰 {bot_worker.email} earned: ${job.price:.2f}")

                                except Exception as e:
                                    log.error(f"Job processing error for {bot_worker.email}: {e}")

                    await asyncio.sleep(10)  # Poll every 10 seconds
            except Exception as e:
                log.warning(f"Job bot cycle error: {e}")
                await asyncio.sleep(10)

    except Exception as e:
        log.warning(f"Job bot error: {e}")


async def process_payouts_periodically():
    """Process pending payouts every 30 seconds.

    OFF BY DEFAULT - set PAYOUTS_ENABLED=true to arm. See the note below
    before doing that."""
    try:
        import stripe
        from database import AsyncSessionLocal
        from models import Payment, Worker
        from sqlalchemy import select, update as sa_update

        # This loop has never completed a single successful pass in
        # production. Every cycle died at the SELECT below, because
        # select(Payment) emits every mapped column and
        # payments.stripe_payout_id did not exist on the real table - the
        # "Payout cycle error" repeating every 30s in Railway. That failure
        # happened BEFORE stripe.Transfer.create, so no money ever moved,
        # and the broken schema was the only thing holding it back.
        #
        # Repairing the migration removes that accidental safety, so the
        # loop needs a deliberate one. What it would be armed into:
        #
        #   - session.commit() is OUTSIDE the per-payment loop, so one
        #     failed commit loses the "paid" status of EVERY transfer in
        #     that pass while the transfers themselves are real and final
        #   - Transfer.create carries no idempotency key
        #   - rows therefore stay "pending", and 30 seconds later the same
        #     payments are transferred again, unbounded
        #
        # A redeploy mid-loop is enough to trigger it. Idempotency keys and
        # per-payment commits are the actual fix and are being handled
        # separately; until that lands this stays off, so that enabling
        # payouts is a decision someone makes rather than a side effect of
        # fixing an unrelated schema bug.
        if os.getenv("PAYOUTS_ENABLED", "false").strip().lower() != "true":
            log.info("Payout processor disabled (PAYOUTS_ENABLED not set to true) "
                     "- no Stripe transfers will be attempted")
            return

        stripe_key = os.getenv("STRIPE_SECRET_KEY")
        if not stripe_key:
            log.warning("Stripe key not configured - payouts disabled")
            return

        stripe.api_key = stripe_key
        log.warning("Payout processor ARMED - PAYOUTS_ENABLED=true, real Stripe "
                    "transfers will be attempted every 30s")

        while True:
            try:
                async with AsyncSessionLocal() as session:
                    # stripe_transfer_id IS NULL is a second, independent
                    # guard against paying twice. The idempotency key below
                    # is the primary one, but Stripe expires keys after 24
                    # hours - past that window the same key is treated as
                    # new and would create a second transfer. A payment that
                    # already carries a transfer id has demonstrably been
                    # paid, whatever its status column says, so it is never
                    # a candidate again regardless of elapsed time.
                    result = await session.execute(
                        select(Payment).where(
                            Payment.payout_status == "pending",
                            Payment.stripe_transfer_id.is_(None),
                        )
                    )
                    pending = result.scalars().all()

                    for payment in pending:
                        try:
                            # Worker.id is Integer, Payment.worker_id is
                            # String. Comparing them directly does not work
                            # on Postgres - verified against a real server:
                            #
                            #   asyncpg.exceptions.UndefinedFunctionError:
                            #   operator does not exist: integer = character
                            #   varying
                            #
                            # SQLAlchemy's Integer has no bind processor on
                            # the postgresql dialect, so the Python str went
                            # straight through to the driver. This raised on
                            # EVERY payment, before Stripe was ever reached,
                            # and the handler below then marked each one
                            # "failed" - so arming payouts would have flipped
                            # the whole pending table to failed rather than
                            # paying anything.
                            #
                            # routers/payments.py already casts with int() at
                            # both of its call sites; only this one did not.
                            # SQLite hides the bug completely (it coerces
                            # '7' == 7), so it is Postgres-only.
                            try:
                                worker_pk = int(payment.worker_id)
                            except (TypeError, ValueError):
                                log.warning(f"Payment {payment.id}: worker_id "
                                            f"{payment.worker_id!r} is not a valid "
                                            f"worker id, skipping")
                                continue

                            w_result = await session.execute(
                                select(Worker).where(Worker.id == worker_pk)
                            )
                            worker = w_result.scalar_one_or_none()

                            if worker and worker.stripe_account_id:
                                # Derived from payment.id, so it is stable
                                # across retries, restarts and redeploys.
                                # Replaying this call returns the ORIGINAL
                                # transfer instead of creating a second one,
                                # which is what makes the window between
                                # "money moved" and "database updated"
                                # survivable rather than expensive.
                                transfer = stripe.Transfer.create(
                                    amount=int(payment.worker_amount * 100),
                                    currency="usd",
                                    destination=worker.stripe_account_id,
                                    description=f"Job payout: {payment.job_id}",
                                    idempotency_key=f"payout-{payment.id}",
                                )
                                payment.payout_status = "paid"
                                payment.stripe_transfer_id = transfer.id
                                payment.paid_at = datetime.utcnow()
                                # Commit per payment, immediately after the
                                # transfer. The commit used to sit after the
                                # whole loop, so one failure discarded the
                                # "paid" status of every transfer in the
                                # pass while the transfers stayed real - and
                                # 30 seconds later all of them were sent
                                # again. Committing here bounds the exposure
                                # to a single payment, and the idempotency
                                # key covers even that one.
                                await session.commit()
                                log.info(f"💰 Payout processed: {payment.id} → {worker.email} (${payment.worker_amount})")
                            else:
                                log.debug(f"Payment {payment.id}: Worker has no Stripe Connect account")
                        except Exception as e:
                            log.error(f"Payout error for {payment.id}: {e}")
                            # The session may hold a failed flush, so the
                            # "failed" mark cannot ride on it - roll back
                            # first, then write the status through a fresh
                            # UPDATE. Without the rollback every subsequent
                            # payment in this pass dies on PendingRollback,
                            # turning one bad payment into a dead cycle.
                            #
                            # Deliberately does NOT touch rows that already
                            # have a transfer id: if the failure happened
                            # after Stripe accepted the transfer, the money
                            # is gone and marking it "failed" would invite
                            # someone to pay it a second time by hand.
                            await session.rollback()
                            try:
                                await session.execute(
                                    sa_update(Payment)
                                    .where(Payment.id == payment.id,
                                           Payment.stripe_transfer_id.is_(None))
                                    .values(payout_status="failed")
                                )
                                await session.commit()
                            except Exception as mark_err:
                                log.error(f"Could not mark {payment.id} failed: {mark_err}")
                                await session.rollback()
            except Exception as e:
                log.warning(f"Payout cycle error: {e}")

            await asyncio.sleep(30)
    except Exception as e:
        log.warning(f"Payout processor error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    print("[LIFESPAN] Platform startup beginning...", flush=True)
    log.info("PGUSA Platform starting...")
    print("[LIFESPAN] Initializing database...", flush=True)
    try:
        await init_db()
        print("[LIFESPAN] ✓ Database initialized", flush=True)
        log.info("Database initialized")
    except Exception as e:
        print(f"[LIFESPAN] ✗ Database init failed: {e}", flush=True)
        log.warning(f"Database init failed: {e}")

    # TEMPORARILY DISABLED: notary_payouts migration was causing startup timeout
    # Will re-enable once migration system is refactored
    # try:
    #     import importlib.util
    #     _spec = importlib.util.spec_from_file_location(
    #         "fix_notary_payouts_job_id_type",
    #         os.path.join(os.path.dirname(__file__), "migrations",
    #                      "0002_fix_notary_payouts_job_id_type.py"),
    #     )
    #     _mod = importlib.util.module_from_spec(_spec)
    #     _spec.loader.exec_module(_mod)
    #     await _mod.migrate()
    # except Exception as e:
    #     log.warning(f"notary_payouts job_id/worker_id type migration failed: {e}")

    print("[LIFESPAN] Creating monitor tables...", flush=True)
    try:
        await create_monitor_tables()
        print("[LIFESPAN] ✓ Monitor tables created", flush=True)
        log.info("Monitor tables ready")
    except Exception as e:
        print(f"[LIFESPAN] ✗ Monitor tables failed: {e}", flush=True)
        log.warning(f"Monitor tables failed: {e}")

    print("[LIFESPAN] Running migrations...", flush=True)
    try:
        await run_migrations()
        print("[LIFESPAN] ✓ Migrations complete", flush=True)
    except Exception as e:
        print(f"[LIFESPAN] ✗ Migrations failed: {e}", flush=True)
        log.warning(f"Migrations failed: {e}")

    print("[LIFESPAN] Validating foreign keys...", flush=True)
    try:
        await validate_foreign_keys()
        print("[LIFESPAN] ✓ Foreign keys validated", flush=True)
    except Exception as e:
        print(f"[LIFESPAN] ✗ Foreign key validation failed: {e}", flush=True)
        log.warning(f"Foreign key validation failed: {e}")

    print("[LIFESPAN] Initializing bot worker...", flush=True)
    try:
        await initialize_bot()
        print("[LIFESPAN] ✓ Bot worker initialized", flush=True)
        log.info("✅ Bot worker initialized")
    except Exception as e:
        # ERROR with a traceback, not a bare warning. This branch was
        # unreachable while initialize_bot swallowed its own exceptions,
        # so "✅ Bot worker initialized" printed even when zero workers
        # existed - and the only other signal was "No bot workers found,
        # skipping cycle" 30 seconds later, in a different log line, from
        # a different task. Nothing connected the two.
        log.error(f"Bot initialization FAILED - no bot workers will exist, so no "
                  f"jobs will be claimed and no payments created: "
                  f"[{type(e).__name__}] {e}", exc_info=True)

    try:
        await initialize_bot_worker()
        log.info("✅ Earnings bot worker initialized (for /payments/bot/earnings)")
    except Exception as e:
        log.error(f"Earnings bot worker initialization failed: [{type(e).__name__}] {e}", exc_info=True)

    try:
        asyncio.create_task(start_job_bot())
        log.info("🤖 Job bot background task started")
    except Exception as e:
        log.warning(f"Job bot startup failed: {e}")

    try:
        asyncio.create_task(process_payouts_periodically())
        log.info("💳 Automatic payout processor started")
    except Exception as e:
        log.warning(f"Payout processor startup failed: {e}")

    # DISABLED: bot_autoscaler has NULL id constraint errors on Worker creation
    # (not critical for trading bot revenue - crypto/alpaca bots work independently)
    # TODO: Fix Worker ORM auto-increment flushing if job scaling becomes priority
    # try:
    #     from bot_autoscaler import auto_scale_bots
    #     asyncio.create_task(auto_scale_bots())
    #     log.info("📈 Bot auto-scaler started - will create bots based on demand")
    # except Exception as e:
    #     log.warning(f"Bot auto-scaler startup failed: {e}")

    try:
        if payee_worker is not None:
            import asyncio
            asyncio.create_task(payee_worker())
            log.info("Payee Trust webhook worker started")
    except Exception as e:
        log.warning(f"Payee worker failed: {e}")

    try:
        if start_daily_publisher is not None and start_daily_publisher():
            log.info("Daily video publisher started")
    except Exception as e:
        log.warning(f"Daily publisher failed: {e}")

    try:
        if start_daily_brief is not None and start_daily_brief():
            log.info("☀️ Daily Ventures Brief scheduled")
    except Exception as e:
        log.warning(f"Daily brief failed to start: {e}")

    try:
        if health_monitor_service is not None:
            import asyncio
            asyncio.create_task(health_monitor_service())
            log.info("🔍 Health Monitor started - continuous error checking active")
    except Exception as e:
        log.warning(f"Health monitor failed: {e}")

    try:
        if retention_manager is not None:
            await retention_manager.initialize_retention_tables(engine)
            log.info("💾 Data Retention Manager initialized - ALL DATA KEPT FOREVER")
    except Exception as e:
        log.warning(f"Retention manager failed: {e}")

    try:
        if prop_bot_module is not None:
            import threading
            mode = "LIVE" if os.getenv("ALPACA_LIVE_TRADE", "false").lower() == "true" else "PAPER"
            stopped = os.getenv("STOP_TRADING", "false").lower() == "true"
            threading.Thread(target=prop_bot_module.run, daemon=True).start()
            log.info(f"📈 Prop bot started (background thread) | Mode: {mode} | STOP_TRADING: {stopped}")
            log.info("💰 Strategy: Market hours stock scalping + 24/7 crypto = constant opportunities and taking profits")
    except Exception as e:
        log.warning(f"Prop bot failed to start: {e}")

    try:
        if notary_bot_module is not None:
            import threading
            threading.Thread(target=notary_bot_module.run, daemon=True).start()
            log.info("🖋️ Notary matching bot started (background thread)")
    except Exception as e:
        log.warning(f"Notary bot failed to start: {e}")

    try:
        if crypto_coinbase_bot_module is not None:
            import threading
            log.info("📡 Starting Crypto (Coinbase) bot daemon thread...")
            bot_thread = threading.Thread(target=crypto_coinbase_bot_module.run, daemon=True)
            bot_thread.start()
            log.info("✓ Crypto (Coinbase) bot thread started | 28 pairs × 12 positions | 24/7 trading | Capital: $700 USD")
            log.info("💰 Strategy: 24/7 crypto + market hours stock scalping = constant opportunities and taking profits")
        else:
            log.warning("⚠️ crypto_coinbase_bot module failed to import - bot will not run")
    except Exception as e:
        log.error(f"🛑 Crypto (Coinbase) bot thread startup failed: {e}")

    print(f"[LIFESPAN] About to check alpaca_swing_bot_module: {alpaca_swing_bot_module is not None}", flush=True)
    try:
        if alpaca_swing_bot_module is not None:
            import threading
            print("[LIFESPAN] ✓ alpaca_swing_bot_module is not None, starting thread...", flush=True)
            log.info("🌊 Starting Alpaca Swing bot daemon thread...")
            swing_bot_thread = threading.Thread(target=alpaca_swing_bot_module.run, daemon=True)
            swing_bot_thread.start()
            print("[LIFESPAN] ✓ Bot thread started", flush=True)
            log.info("✅ Alpaca Swing bot started | Weekly RSI < 30 entries | 5-10 day holds | Indices + Commodities")
        else:
            print("[LIFESPAN] ✗ alpaca_swing_bot_module is None", flush=True)
            log.warning("⚠️ alpaca_swing_bot module failed to import - bot will not run")
    except Exception as e:
        import traceback
        print(f"[LIFESPAN] ✗ Exception: {e}", flush=True)
        print(f"[LIFESPAN] Traceback: {traceback.format_exc()}", flush=True)
        log.error(f"🛑 Alpaca Swing bot startup failed: {e}")

    # bot_2_crypto_scalper.py retired: it traded crypto (BTC/ETH/SOL/AVAX/
    # DOGE/LINK) through Alpaca using the same ALPACA_API_KEY as prop_bot.py
    # trades stocks with - but Alpaca crypto is blocked for this account's
    # state (see crypto_coinbase_bot.py), so every order it placed almost
    # certainly failed silently the entire time it ran (confirmed: 0 wins,
    # 0 losses, ever). Its state also lived in a local JSON file on this
    # same container's ephemeral filesystem, reset to defaults on every
    # redeploy - the same "forgets everything on restart" bug PR #109 fixed
    # for the other two bots, except this one never got that fix. And even
    # if Alpaca crypto were ever enabled for this account, it sized every
    # order off the SAME shared account balance prop_bot.py trades from,
    # with zero coordination between them - a real capital-collision risk.
    # Crypto trading belongs on Coinbase (crypto_coinbase_bot.py) - Alpaca
    # is for stocks only.

    try:
        from stripe_subscriptions import setup_stripe_products
        if setup_stripe_products():
            log.info("💳 Stripe subscription products initialized")
        else:
            log.warning("Stripe products setup skipped - no API key configured")
    except Exception as e:
        log.warning(f"Stripe setup failed: {e}")

    # Check video payment system
    stripe_secret = os.getenv("STRIPE_SECRET_KEY")
    stripe_publishable = os.getenv("STRIPE_PUBLISHABLE_KEY")
    stripe_webhook = os.getenv("STRIPE_WEBHOOK_SECRET")
    if not (stripe_secret and stripe_publishable and stripe_webhook):
        log.error("🔴 CRITICAL: Video payment system blocked - Stripe env vars missing!")
        log.error("   Required in Railway Variables:")
        log.error("   • STRIPE_SECRET_KEY (sk_...)")
        log.error("   • STRIPE_PUBLISHABLE_KEY (pk_...)")
        log.error("   • STRIPE_WEBHOOK_SECRET (whsec_...)")
        log.error("   11 video orders ($82.50 revenue) are waiting for payment - add keys and redeploy")
    else:
        log.info("✅ Stripe payment system configured - video orders ready for payment")

    # Initialize Hermes Agent (Phase 1)
    try:
        from hermes_agent import init_hermes, HermesAgentConfig
        from telegram_integration import init_telegram, TelegramConfig
        from status_reporter import init_status_reporter

        hermes_config = HermesAgentConfig(
            enabled=bool(os.getenv("HERMES_API_KEY")),
            api_key=os.getenv("HERMES_API_KEY"),
            model=os.getenv("HERMES_MODEL", "claude-opus"),
        )

        telegram_config = TelegramConfig(
            enabled=bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")),
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
            chat_id=os.getenv("TELEGRAM_CHAT_ID"),
        )

        hermes = init_hermes(hermes_config)
        telegram = init_telegram(telegram_config)
        status_reporter = init_status_reporter(telegram)

        if hermes.enabled:
            log.info("🤖 Hermes Agent initialized - autonomous bot management active")
        if telegram.enabled:
            log.info("📱 Telegram integration ready - status reports will be sent to chat")
        if not hermes.enabled and not telegram.enabled:
            log.warning("⚠️ Hermes & Telegram disabled - configure HERMES_API_KEY and TELEGRAM_BOT_TOKEN to enable")
    except Exception as e:
        log.warning(f"Hermes Agent initialization failed: {e}")

    log.info("Platform startup complete")
    yield
    log.info("PGUSA Platform shutting down")


app = FastAPI(
    title="Property Group USA Documents Platform API",
    description="SaaS backend for notary, tax prep, and legal document services",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files for study assistant
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# ── Routers ──────────────────────────────────────────────────
routers_list = [
    (auth, "/auth", "Auth"),
    (workers, "/workers", "Workers"),
    (clients, "/clients", "Clients"),
    (jobs, "/jobs", "Jobs"),
    (bookings, "/bookings", "Bookings"),
    (payments, "/payments", "Payments"),
    (admin, "/admin", "Admin"),
    (whitelabel, "/whitelabel", "White Label"),
    (partners, "/partners", "Partners"),
    (labeling, "/labeling", "AI Labeling"),
    (revenue_automation, "/revenue", "Revenue Automation"),
    (orders, "/orders", "Video Orders"),
    (subscriptions, "/subscriptions", "Subscriptions"),
    (trading_signals, "/trading", "Trading Signals"),
    (outreach, "/outreach", "Outreach & Campaigns"),
    (study, "/study", "Study Assistant"),
    (trading_dashboard, "/api/trading-dashboard", "Trading Dashboard"),
    (support, "/support", "AI Customer Support"),
    (sales, "/sales", "AI Sales Agent"),
    (alpaca_funding, "/funding", "Alpaca Broker Auto-Funding"),
    (sweep, "/sweep", "Profit Sweep Engine"),
    (bot_race, "/api", "Bot Race Dashboard"),
    (alpaca_dashboard, "/alpaca", "Alpaca Trading Dashboard"),
    (trading_hub, "/trading-hub", "Trading Hub - Live Bot Dashboard"),
]

for router_module, prefix, tag in routers_list:
    if router_module is not None:
        try:
            app.include_router(router_module.router, prefix=prefix, tags=[tag])
            log.info(f"Router loaded: {prefix}")
        except Exception as e:
            log.warning(f"Failed to include router {prefix}: {e}")

if payee_router is not None:
    try:
        app.include_router(payee_router, prefix="/payee", tags=["Payee Trust"])
        log.info("Router loaded: /payee")
    except Exception as e:
        log.warning(f"Failed to include payee router: {e}")

if payroll_router is not None:
    try:
        app.include_router(payroll_router, prefix="/workers/payroll", tags=["Worker Payroll"])
        log.info("Router loaded: /workers/payroll")
    except Exception as e:
        log.warning(f"Failed to include payroll router: {e}")

if social_dashboard is not None:
    try:
        app.include_router(social_dashboard.router, prefix="/social", tags=["Social Media Dashboard"])
        log.info("Router loaded: /social")
    except Exception as e:
        log.warning(f"Failed to include social dashboard router: {e}")

if video_revenue_router is not None:
    try:
        # No prefix: video_auto_editor.py calls these paths
        # (e.g. /publish/youtube/social-content) directly against this
        # service's own port (YOUTUBE_API_URL defaults to localhost:10000).
        app.include_router(video_revenue_router, tags=["Video Revenue"])
        log.info("Router loaded: video revenue (no prefix)")
    except Exception as e:
        log.warning(f"Failed to include video revenue router: {e}")

if trading_signals is not None:
    try:
        # Frontend calls /api/subscribe; keep /trading as the canonical prefix too.
        app.include_router(trading_signals.router, prefix="/api", tags=["API Alias"])
        log.info("Router loaded: /api (trading signals alias)")
    except Exception as e:
        log.warning(f"Failed to include trading signals /api alias: {e}")

# Bot earnings and payouts dashboard
try:
    from routers import dashboard
    app.include_router(dashboard.router, tags=["Bot Dashboard"])
    log.info("✅ Router loaded: /dashboard (bot earnings)")
except Exception as e:
    log.warning(f"Failed to load bot dashboard router: {e}")

# Crypto bot analytics and trade instrumentation
try:
    from routers import crypto_analytics
    app.include_router(crypto_analytics.router, tags=["Crypto Analytics"])
    log.info("✅ Router loaded: /crypto/analytics (state machine instrumentation)")
except Exception as e:
    log.warning(f"Failed to load crypto analytics router: {e}")


@app.get("/dashboard")
async def serve_dashboard():
    """Serve the social media dashboard HTML"""
    dashboard_path = os.path.join(os.path.dirname(__file__), "social_media_dashboard.html")
    if not os.path.exists(dashboard_path):
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return FileResponse(dashboard_path, media_type="text/html")


@app.get("/signals")
async def serve_signals_signup():
    """Serve the trading signals signup page"""
    signals_path = os.path.join(os.path.dirname(__file__), "signals_signup.html")
    if not os.path.exists(signals_path):
        raise HTTPException(status_code=404, detail="Signals signup page not found")
    return FileResponse(signals_path, media_type="text/html")


# routers/bot_status.py (mounted at /api/bot) was retired along with
# bot_2_crypto_scalper.py - see the lifespan startup comment above for why.


@app.get("/trading-dashboard")
async def serve_trading_dashboard():
    """Serve the Bare Metal Builders live trading dashboard (admin-only data,
    gated by X-Admin-Key on the /api/trading-dashboard/* endpoints it calls)"""
    dashboard_path = os.path.join(os.path.dirname(__file__), "trading_dashboard.html")
    if not os.path.exists(dashboard_path):
        raise HTTPException(status_code=404, detail="Trading dashboard not found")
    return FileResponse(dashboard_path, media_type="text/html")


@app.get("/api/orchestrator/stats")
async def get_orchestrator_stats(db = Depends(lambda: None)):
    """Aggregate all earnings: video production + trading (prop + crypto)"""
    import aiohttp
    import asyncio

    try:
        stats = {
            "timestamp": datetime.now().isoformat(),
            "video_production": {"total_earned": 0, "jobs_completed": 0, "pending": 0},
            "trading": {"total_earned": 0, "equity": 0, "daily_pnl": 0, "accounts": []},
            "summary": {"total_earned": 0, "net_equity": 0, "status": "loading"}
        }

        # Video production stats
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("http://localhost:8000/payments/bot/earnings", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        video_data = await resp.json()
                        stats["video_production"] = {
                            "total_earned": video_data.get("total_earned", 0),
                            "jobs_completed": video_data.get("payment_count", 0),
                            "pending": video_data.get("pending_payout", 0)
                        }
        except Exception as e:
            log.warning(f"Error fetching video stats: {e}")

        # Alpaca trading stats
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("http://localhost:8000/payments/alpaca/account", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        alpaca_data = await resp.json()
                        if alpaca_data.get("status") == "ok":
                            stats["trading"]["accounts"].append({
                                "name": "Alpaca (Stocks/ETFs/Futures)",
                                "equity": alpaca_data.get("capital", {}).get("equity", 0),
                                "buying_power": alpaca_data.get("buying_power", {}).get("available", 0),
                                "mode": alpaca_data.get("trading_mode", "PAPER")
                            })
                            stats["trading"]["equity"] += alpaca_data.get("capital", {}).get("equity", 0)
        except Exception as e:
            log.warning(f"Error fetching Alpaca stats: {e}")

        # Coinbase crypto stats (real-time USD balance)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("http://localhost:8000/api/trading-dashboard/coinbase/usd-balance", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        crypto_data = await resp.json()
                        if crypto_data.get("status") == "ok":
                            usd_balance = crypto_data.get("usd_balance", 0)
                            stats["trading"]["accounts"].append({
                                "name": "Coinbase (BTC/ETH 24/7)",
                                "usd_balance": usd_balance,
                                "equity": usd_balance,
                                "mode": "🔴 LIVE CRYPTO (24/7)"
                            })
                            stats["trading"]["equity"] += usd_balance
        except Exception as e:
            log.warning(f"Error fetching Coinbase stats: {e}")

        # Calculate totals
        stats["trading"]["total_earned"] = stats["trading"]["equity"]
        stats["summary"]["total_earned"] = stats["video_production"]["total_earned"] + stats["trading"]["total_earned"]
        stats["summary"]["net_equity"] = stats["trading"]["equity"]
        stats["summary"]["status"] = "ok"

        return stats

    except Exception as e:
        log.error(f"Error in orchestrator stats: {e}")
        return {
            "timestamp": datetime.now().isoformat(),
            "error": str(e),
            "status": "error"
        }


@app.get("/orchestrator")
async def serve_orchestrator_dashboard():
    """Serve the unified Orchestrator dashboard - video production + trading earnings"""
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>🎯 Orchestrator Dashboard - Empire v2</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
                background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
                color: #fff;
                min-height: 100vh;
                padding: 20px;
            }
            .container { max-width: 1400px; margin: 0 auto; }
            h1 { text-align: center; margin-bottom: 30px; font-size: 2.5em; text-shadow: 0 2px 10px rgba(0,0,0,0.3); }

            .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(350px, 1fr)); gap: 20px; margin-bottom: 30px; }
            .card {
                background: rgba(255,255,255,0.1);
                border: 1px solid rgba(255,255,255,0.2);
                border-radius: 12px;
                padding: 25px;
                backdrop-filter: blur(10px);
                box-shadow: 0 8px 32px rgba(0,0,0,0.2);
                transition: transform 0.3s ease, box-shadow 0.3s ease;
            }
            .card:hover { transform: translateY(-5px); box-shadow: 0 12px 40px rgba(0,0,0,0.3); }

            .card h2 { font-size: 1.2em; margin-bottom: 15px; opacity: 0.9; }
            .metric { margin: 12px 0; display: flex; justify-content: space-between; align-items: center; }
            .metric-label { opacity: 0.8; font-size: 0.95em; }
            .metric-value { font-size: 1.5em; font-weight: bold; color: #4ade80; }

            .summary-card {
                grid-column: 1 / -1;
                background: linear-gradient(135deg, rgba(74,222,128,0.2), rgba(59,130,246,0.2));
                border: 2px solid rgba(74,222,128,0.4);
            }
            .summary-card h2 { font-size: 1.8em; color: #4ade80; }
            .summary-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; }

            .status-badge {
                display: inline-block;
                padding: 6px 12px;
                border-radius: 20px;
                font-size: 0.85em;
                font-weight: 600;
                background: rgba(74,222,128,0.2);
                color: #4ade80;
                border: 1px solid rgba(74,222,128,0.5);
            }
            .status-badge.error { background: rgba(239,68,68,0.2); color: #ef4444; border-color: rgba(239,68,68,0.5); }
            .status-badge.loading { background: rgba(59,130,246,0.2); color: #3b82f6; border-color: rgba(59,130,246,0.5); }

            .account-list { margin-top: 15px; }
            .account-item {
                background: rgba(255,255,255,0.05);
                padding: 10px;
                border-radius: 6px;
                margin: 8px 0;
                font-size: 0.9em;
            }
            .account-item .name { font-weight: 600; margin-bottom: 5px; }
            .account-item .details { opacity: 0.8; display: flex; justify-content: space-between; }

            .refresh-btn {
                background: rgba(59,130,246,0.3);
                border: 1px solid rgba(59,130,246,0.6);
                color: #fff;
                padding: 10px 20px;
                border-radius: 6px;
                cursor: pointer;
                font-size: 1em;
                transition: all 0.3s ease;
            }
            .refresh-btn:hover { background: rgba(59,130,246,0.5); }
            .controls { text-align: center; margin-bottom: 30px; }

            .last-update { text-align: center; opacity: 0.7; font-size: 0.9em; margin-top: 20px; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🎯 Orchestrator Dashboard</h1>

            <div class="controls">
                <button class="refresh-btn" onclick="refreshData()">🔄 Refresh</button>
            </div>

            <div class="grid" id="dashboard">
                <div class="card" style="grid-column: 1 / -1;">
                    <h2>Loading data...</h2>
                    <p>Aggregating video production, trading, and equity data...</p>
                </div>
            </div>

            <div class="last-update" id="lastUpdate"></div>
        </div>

        <script>
            async function loadData() {
                try {
                    const resp = await fetch('/api/orchestrator/stats');
                    const data = await resp.json();
                    renderDashboard(data);
                } catch (e) {
                    console.error('Error loading data:', e);
                    document.getElementById('dashboard').innerHTML = '<div class="card" style="grid-column: 1 / -1;"><h2>⚠️ Error Loading Data</h2><p>' + e.message + '</p></div>';
                }
            }

            function renderDashboard(data) {
                const dashboard = document.getElementById('dashboard');
                let html = '';

                // Summary card
                html += `
                    <div class="card summary-card">
                        <h2>💰 Total Earnings</h2>
                        <div class="summary-metrics">
                            <div>
                                <div class="metric-label">Combined Revenue</div>
                                <div class="metric-value">$${data.summary.total_earned.toFixed(2)}</div>
                            </div>
                            <div>
                                <div class="metric-label">Trading Equity</div>
                                <div class="metric-value">$${data.summary.net_equity.toFixed(2)}</div>
                            </div>
                            <div>
                                <div class="metric-label">System Status</div>
                                <div style="margin-top: 8px;"><span class="status-badge ${data.summary.status === 'ok' ? '' : 'error'}">● ${data.summary.status.toUpperCase()}</span></div>
                            </div>
                        </div>
                    </div>
                `;

                // Video production
                html += `
                    <div class="card">
                        <h2>🎬 Video Production</h2>
                        <div class="metric">
                            <span class="metric-label">Total Earned</span>
                            <span class="metric-value">$${data.video_production.total_earned.toFixed(2)}</span>
                        </div>
                        <div class="metric">
                            <span class="metric-label">Jobs Completed</span>
                            <span class="metric-value">${data.video_production.jobs_completed}</span>
                        </div>
                        <div class="metric">
                            <span class="metric-label">Pending Payout</span>
                            <span class="metric-value" style="color: #fbbf24;">$${data.video_production.pending.toFixed(2)}</span>
                        </div>
                    </div>
                `;

                // Trading summary
                html += `
                    <div class="card">
                        <h2>📈 Trading Accounts</h2>
                        <div class="metric">
                            <span class="metric-label">Total Equity</span>
                            <span class="metric-value">$${data.trading.equity.toFixed(2)}</span>
                        </div>
                        <div class="metric">
                            <span class="metric-label">Active Accounts</span>
                            <span class="metric-value">${data.trading.accounts.length}</span>
                        </div>
                        <div class="account-list">
                            ${data.trading.accounts.map(acc => `
                                <div class="account-item">
                                    <div class="name">${acc.name}</div>
                                    <div class="details">
                                        <span>Equity: $${(acc.equity || acc.usd_balance || 0).toFixed(2)}</span>
                                        <span>${acc.mode}</span>
                                    </div>
                                </div>
                            `).join('')}
                        </div>
                    </div>
                `;

                dashboard.innerHTML = html;
                document.getElementById('lastUpdate').textContent = '🕐 Updated: ' + new Date(data.timestamp).toLocaleString();
            }

            function refreshData() {
                loadData();
            }

            // Load on page load
            loadData();
            // Auto-refresh every 30 seconds
            setInterval(loadData, 30000);
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.get("/notary-portal")
async def serve_notary_portal():
    """Serve the notary partner self-service portal. Until now, /workers,
    /jobs, and /bookings only existed as bare JSON APIs - real notaries had
    no page to actually register, log in, submit credentials, or see jobs
    matched to them on. This is per-worker login (their own email+password,
    see worker_auth.py), not the shared admin key the trading/social
    dashboards use."""
    portal_path = os.path.join(os.path.dirname(__file__), "notary_portal.html")
    if not os.path.exists(portal_path):
        raise HTTPException(status_code=404, detail="Notary portal not found")
    return FileResponse(portal_path, media_type="text/html")


@app.get("/get-notarized")
async def serve_notary_request():
    """Public, no-auth client-facing intake page for POST
    /jobs/notarization/request - the other half of the same gap the notary
    portal fixed. Without this, the only way a real client could ever
    submit a notarization job was hand-crafting a raw JSON POST - there was
    no marketplace demand side at all, just a backend API nobody outside
    this codebase could actually use."""
    request_path = os.path.join(os.path.dirname(__file__), "notary_request.html")
    if not os.path.exists(request_path):
        raise HTTPException(status_code=404, detail="Notarization request page not found")
    return FileResponse(request_path, media_type="text/html")


@app.get("/notary-admin")
async def serve_notary_admin():
    """Admin-only (gated by X-Admin-Key on every API call it makes, same
    pattern as /trading-dashboard) panel to review pending notary
    credential submissions and approve/reject them, and to manually match
    'requested' jobs to an eligible verified notary. Verification is
    deliberately not self-service (see routers/workers.py) since these are
    real legal credentials - this page is what makes that admin step
    actually usable instead of requiring a raw curl command."""
    admin_path = os.path.join(os.path.dirname(__file__), "notary_admin.html")
    if not os.path.exists(admin_path):
        raise HTTPException(status_code=404, detail="Notary admin panel not found")
    return FileResponse(admin_path, media_type="text/html")


@app.get("/quote")
async def serve_quote_form():
    """Serve the subscription-aware video quote form"""
    try:
        app_dir = os.path.dirname(os.path.abspath(__file__))
        possible_paths = [
            os.path.join(app_dir, "subscription_quote_form.html"),
            os.path.join(app_dir, "quote_request.html"),
            "/app/subscription_quote_form.html",
            "/app/quote_request.html",
            "subscription_quote_form.html",
            "quote_request.html",
            os.path.join(os.getcwd(), "quote_request.html"),
        ]

        quote_path = None
        for path in possible_paths:
            if os.path.exists(path):
                quote_path = path
                log.info(f"✓ Quote form found at: {path}")
                break

        if not quote_path:
            log.error(f"Quote form not found. Tried: {possible_paths}. CWD: {os.getcwd()}, APP_DIR: {app_dir}")
            raise HTTPException(status_code=404, detail="Quote form file not found")

        with open(quote_path, 'r') as f:
            html_content = f.read()
        return HTMLResponse(content=html_content, status_code=200)
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Error serving quote form: {e}")
        raise HTTPException(status_code=500, detail=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/trading-hub/dashboard")
async def serve_trading_hub_dashboard():
    """Serve the real-time trading hub dashboard"""
    try:
        app_dir = os.path.dirname(os.path.abspath(__file__))
        possible_paths = [
            os.path.join(app_dir, "trading_hub.html"),
            "/app/trading_hub.html",
            "trading_hub.html",
            os.path.join(os.getcwd(), "trading_hub.html"),
        ]

        hub_path = None
        for path in possible_paths:
            if os.path.exists(path):
                hub_path = path
                log.info(f"✓ Trading hub found at: {path}")
                break

        if not hub_path:
            log.error(f"Trading hub not found. Tried: {possible_paths}. CWD: {os.getcwd()}, APP_DIR: {app_dir}")
            raise HTTPException(status_code=404, detail="Trading hub file not found")

        with open(hub_path, 'r') as f:
            html_content = f.read()
        return HTMLResponse(content=html_content, status_code=200)
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Error serving trading hub: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/bot-earnings")
async def serve_bot_earnings_dashboard():
    """Serve the bot earnings dashboard widget"""
    try:
        possible_paths = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_earnings.html"),
            "/app/bot_earnings.html",
            "bot_earnings.html",
        ]

        bot_path = None
        for path in possible_paths:
            if os.path.exists(path):
                bot_path = path
                break

        if not bot_path:
            raise HTTPException(status_code=404, detail="Bot earnings dashboard not found")

        with open(bot_path, 'r') as f:
            html_content = f.read()
        return HTMLResponse(content=html_content, status_code=200)
    except Exception as e:
        log.error(f"Error serving quote form: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error serving quote form: {str(e)}")


@app.get("/order-success")
async def order_success(session_id: str = None):
    """Stripe payment success page"""
    return {
        "status": "success",
        "message": "Payment received! Your video creation is starting now.",
        "session_id": session_id,
        "next_step": "Check your email for updates",
    }


@app.get("/")
async def root():
    return {
        "platform": "Property Group USA Documents Platform",
        "version": "1.0.0",
        "status": "online",
        "endpoints": {
            "docs": "/docs",
            "workers": "/workers",
            "clients": "/clients",
            "jobs": "/jobs",
            "bookings": "/bookings",
            "admin": "/admin",
            "whitelabel": "/whitelabel",
            "partners": "/partners",
        }
    }


@app.get("/health")
async def health():
    return {"status": "ok", "platform": "pgusa-documents", "version": "v2.1-trading-signals"}


@app.get("/study-app")
async def study_app():
    """Serve the Study Assistant web app"""
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    study_html = os.path.join(static_dir, "study.html")
    if os.path.exists(study_html):
        return FileResponse(study_html)
    raise HTTPException(status_code=404, detail="Study app not found")


@app.get("/monitor/status")
async def get_monitor_status():
    """Get current health monitor status"""
    if monitor is None:
        return {"error": "Monitor not available"}
    return monitor.get_status()


@app.get("/monitor/errors")
async def get_monitor_errors(limit: int = 50):
    """Get error history from monitor"""
    if monitor is None:
        return {"error": "Monitor not available"}
    return {
        "total_errors": len(monitor.error_history),
        "errors": monitor.get_error_history(limit)
    }


@app.get("/monitor/fixed-issues")
async def get_fixed_issues(limit: int = 50):
    """Get list of auto-fixed issues"""
    if monitor is None:
        return {"error": "Monitor not available"}
    return {
        "total_fixed": len(monitor.fixed_issues),
        "fixed_issues": monitor.get_fixed_issues(limit)
    }


@app.get("/monitor/metrics")
async def get_performance_metrics(limit: int = 50):
    """Get performance metrics history"""
    if monitor is None:
        return {"error": "Monitor not available"}
    return {
        "total_metrics_logged": len(monitor.performance_metrics),
        "metrics": monitor.get_performance_metrics(limit)
    }


@app.get("/monitor/comprehensive")
async def get_comprehensive_status():
    """Get complete comprehensive monitoring status and all data"""
    if monitor is None:
        return {"error": "Monitor not available"}
    return {
        "status": monitor.get_status(),
        "error_history": monitor.get_error_history(100),
        "fixed_issues": monitor.get_fixed_issues(100),
        "performance_metrics": monitor.get_performance_metrics(50),
        "timestamp": datetime.now().isoformat()
    }


@app.get("/retention/data-count")
async def get_total_data_stored():
    """Get complete count of all data ever stored (current + archived)"""
    if retention_manager is None:
        return {"error": "Retention manager not available"}
    return await retention_manager.get_total_data_stored(engine)


@app.get("/retention/status")
async def get_retention_status():
    """Get data retention and archival status"""
    if retention_manager is None:
        return {"error": "Retention manager not available"}
    return await retention_manager.get_retention_status(engine)


@app.get("/retention/database-size")
async def get_database_size():
    """Get database size and storage usage"""
    if retention_manager is None:
        return {"error": "Retention manager not available"}
    return await retention_manager.get_database_size(engine)


@app.post("/retention/archive-old-data")
async def trigger_archival(days_threshold: int = 90):
    """Manually trigger data archival (moves old data to archive tables)"""
    if retention_manager is None:
        return {"error": "Retention manager not available"}
    await retention_manager.archive_old_data(engine, days_threshold)
    return {
        "status": "archived",
        "message": f"Data older than {days_threshold} days moved to permanent archive",
        "note": "IMPORTANT: No data is deleted, only moved to archive tables"
    }


# ============================================================================
# BOT MONITORING ENDPOINTS — Real-time visibility into trading bot operations
# ============================================================================

def _read_log_file(filepath: str, lines: int = 50) -> list:
    """Read last N lines from a log file, return as list"""
    import os
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, 'r') as f:
            all_lines = f.readlines()
            return all_lines[-lines:] if all_lines else []
    except Exception as e:
        return [f"Error reading log: {e}"]


def _read_json_file(filepath: str) -> dict:
    """Read JSON file, return parsed content or empty dict"""
    import json
    import os
    if not os.path.exists(filepath):
        return {"status": "no_data", "message": f"File not found: {filepath}"}
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/admin/bot-logs/pl-tracker")
async def get_pl_tracker_logs(lines: int = 100):
    """Get last N lines from P&L tracker log (logs/bot_pl_tracker.log)"""
    log_lines = _read_log_file("logs/bot_pl_tracker.log", lines)
    return {
        "service": "pl-tracker",
        "log_file": "logs/bot_pl_tracker.log",
        "lines_requested": lines,
        "lines_returned": len(log_lines),
        "logs": [line.rstrip('\n') for line in log_lines],
        "timestamp": datetime.now().isoformat()
    }


@app.get("/admin/bot-pl-history")
async def get_bot_pl_history(limit: int = 500):
    """Get P&L history snapshots (bot_pl_history.json) - last N snapshots"""
    history = _read_json_file("bot_pl_history.json")

    # If we have snapshots, return only the last N
    if isinstance(history, dict) and "snapshots" in history:
        snapshots = history.get("snapshots", [])
        return {
            "service": "pl-tracker",
            "history_file": "bot_pl_history.json",
            "total_snapshots": len(snapshots),
            "snapshots_returned": min(limit, len(snapshots)),
            "snapshots": snapshots[-limit:] if len(snapshots) > limit else snapshots,
            "milestones_hit": history.get("milestones_hit", []),
            "timestamp": datetime.now().isoformat()
        }

    return {
        "service": "pl-tracker",
        "history_file": "bot_pl_history.json",
        "total_snapshots": 0,
        "error": "No history data yet",
        "data": history,
        "timestamp": datetime.now().isoformat()
    }


@app.get("/admin/bot-status")
async def get_bot_status():
    """Get comprehensive bot status - logs and P&L in one call"""
    pl_logs = _read_log_file("logs/bot_pl_tracker.log", 20)
    history = _read_json_file("bot_pl_history.json")

    # Get latest snapshot if available
    latest_snapshot = None
    if isinstance(history, dict) and "snapshots" in history:
        snapshots = history.get("snapshots", [])
        if snapshots:
            latest_snapshot = snapshots[-1]

    return {
        "timestamp": datetime.now().isoformat(),
        "services": {
            "pl-tracker": {
                "log_file": "logs/bot_pl_tracker.log",
                "recent_logs": [line.rstrip('\n') for line in pl_logs],
                "latest_snapshot": latest_snapshot,
                "milestones_hit": history.get("milestones_hit", []) if isinstance(history, dict) else []
            }
        }
    }


# ============================================================================
# HERMES AGENT - PHASE 1 ENDPOINTS (Autonomous Bot Management)
# ============================================================================

@app.get("/hermes/status")
async def hermes_status():
    """Get current Hermes Agent status and configuration"""
    from hermes_agent import get_hermes
    from telegram_integration import get_telegram
    from status_reporter import get_status_reporter

    hermes = get_hermes()
    telegram = get_telegram()
    reporter = get_status_reporter()

    if not hermes:
        return {"error": "Hermes not initialized", "timestamp": datetime.utcnow().isoformat()}

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "hermes": hermes.get_config(),
        "telegram": telegram.get_config() if telegram else None,
        "reporter": reporter.get_statistics() if reporter else None,
    }


@app.post("/hermes/init-session")
async def hermes_init_session(session_id: str):
    """Initialize a new Hermes Agent session"""
    from hermes_agent import get_hermes

    hermes = get_hermes()
    if not hermes or not hermes.enabled:
        raise HTTPException(status_code=503, detail="Hermes not available")

    try:
        session = await hermes.initialize_session(session_id)
        return {"status": "ok", "session": {
            "session_id": session.session_id,
            "status": session.status,
            "started_at": session.started_at.isoformat(),
        }}
    except Exception as e:
        log.error(f"Session init error: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/telegram/health")
async def telegram_health():
    """Check Telegram bot connection status"""
    from telegram_integration import get_telegram

    telegram = get_telegram()
    if not telegram:
        return {"status": "not_initialized", "enabled": False}

    return {
        "status": "ok" if telegram.enabled else "disabled",
        "enabled": telegram.enabled,
        "chat_id": telegram.chat_id[:5] + "..." if telegram.chat_id else None,
        "message_count": len(telegram.message_history),
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.post("/telegram/send-test")
async def telegram_send_test(message: str = "Test message from Hermes Agent 🤖"):
    """Send a test message to Telegram"""
    from telegram_integration import get_telegram

    telegram = get_telegram()
    if not telegram or not telegram.enabled:
        raise HTTPException(status_code=503, detail="Telegram not available")

    try:
        success = await telegram.send_message(message, message_type="test")
        return {"status": "ok" if success else "failed", "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        log.error(f"Telegram send error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/bot/status/record")
async def record_bot_status(request: BotStatusRequest):
    """Record trading bot status (called by bots)"""
    from status_reporter import get_status_reporter
    from database import AsyncSessionLocal
    from models import BotStatus as BotStatusModel

    reporter = get_status_reporter()

    if not reporter:
        raise HTTPException(status_code=503, detail="Status reporter not initialized")

    try:
        # Record in reporter
        await reporter.record_bot_status(
            bot_name=request.bot_name,
            open_positions=request.open_positions,
            cash_available=request.cash_available,
            daily_pnl=request.daily_pnl,
            win_rate=request.win_rate,
            trade_count=request.trade_count,
            errors=request.errors,
        )

        # Also persist to database
        async with AsyncSessionLocal() as session:
            db_status = BotStatusModel(
                bot_name=request.bot_name,
                timestamp=datetime.utcnow(),
                cash_available=request.cash_available,
                daily_pnl=request.daily_pnl,
                win_rate=request.win_rate,
                trade_count=request.trade_count,
                open_positions_count=len(request.open_positions),
                errors=request.errors,
                metadata={"positions": request.open_positions},
            )
            session.add(db_status)
            await session.commit()

        return {"status": "recorded", "bot": request.bot_name, "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        log.error(f"Status record error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/bot/status/latest")
async def get_latest_bot_status(bot_name: str = None):
    """Get latest status for a bot or all bots"""
    from status_reporter import get_status_reporter

    reporter = get_status_reporter()
    if not reporter:
        raise HTTPException(status_code=503, detail="Status reporter not initialized")

    try:
        if bot_name:
            status = await reporter.get_bot_status(bot_name)
            if not status:
                raise HTTPException(status_code=404, detail=f"No status found for {bot_name}")
            return {
                "bot": bot_name,
                "cash": status.cash_available,
                "daily_pnl": status.daily_pnl,
                "win_rate": status.win_rate,
                "positions": len(status.open_positions),
                "timestamp": status.timestamp.isoformat(),
            }
        else:
            # Return summary for all bots
            report = await reporter.generate_summary_report()
            return report
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Status fetch error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/bot/status/report")
async def send_bot_status_report(bot_name: str = None):
    """Send status report to Telegram"""
    from status_reporter import get_status_reporter

    reporter = get_status_reporter()
    if not reporter:
        raise HTTPException(status_code=503, detail="Status reporter not initialized")

    try:
        success = await reporter.send_status_report(bot_name)
        return {
            "status": "sent" if success else "failed",
            "bot": bot_name or "all",
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        log.error(f"Report send error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    try:
        port = int(os.getenv("PORT", 8000))
    except (ValueError, TypeError):
        log.warning("Invalid PORT value, using default: 8000")
        port = 8000
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)