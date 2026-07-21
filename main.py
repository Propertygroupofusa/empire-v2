"""
PROPERTY GROUP USA — DOCUMENTS PLATFORM BACKEND
=================================================
Full SaaS backend with worker management, client booking,
job matching, payments, admin dashboard, and white label API.
"""

from fastapi import FastAPI, HTTPException, Depends, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from sqlalchemy import text
from datetime import datetime
import os
import uvicorn
import logging

from database import init_db, engine

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

tradovate_bot_module = None
try:
    import tradovate_bot
    tradovate_bot_module = tradovate_bot
except Exception as e:
    logging.warning(f"Failed to import tradovate_bot: {e}")

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
    """Add any missing columns to existing tables — safe to run every startup."""
    cols = [
        ("w9_submitted",          "BOOLEAN DEFAULT FALSE"),
        ("w9_legal_name",         "VARCHAR"),
        ("w9_tax_classification", "VARCHAR"),
        ("w9_tin_last4",          "VARCHAR"),
        ("w9_address",            "TEXT"),
        ("credentials_submitted",     "BOOLEAN DEFAULT FALSE"),
        ("credentials_verified",      "BOOLEAN DEFAULT FALSE"),
        ("notary_commission_number",  "VARCHAR"),
        ("notary_commission_state",   "VARCHAR"),
        ("notary_commission_expires", "VARCHAR"),
        ("ron_authorized",            "BOOLEAN DEFAULT FALSE"),
        ("ron_authorization_state",   "VARCHAR"),
    ]
    async with engine.begin() as conn:
        for col, col_type in cols:
            try:
                # Check if column exists first (SQLite doesn't support IF NOT EXISTS for ALTER TABLE)
                check_result = await conn.execute(text(
                    f"PRAGMA table_info(workers)"
                ))
                existing_columns = [row[1] for row in await check_result.fetchall()]

                if col not in existing_columns:
                    await conn.execute(text(
                        f"ALTER TABLE workers ADD COLUMN {col} {col_type}"
                    ))
                    log.info(f"Migration OK: {col}")
            except Exception as e:
                log.debug(f"Migration skip {col}: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("PGUSA Platform starting...")
    try:
        await init_db()
        log.info("Database initialized")
    except Exception as e:
        log.warning(f"Database init failed: {e}")

    try:
        await create_monitor_tables()
        log.info("Monitor tables ready")
    except Exception as e:
        log.warning(f"Monitor tables failed: {e}")

    try:
        await run_migrations()
    except Exception as e:
        log.warning(f"Migrations failed: {e}")

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
        if health_monitor_service is not None:
            import asyncio
            await health_monitor_service()
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
    except Exception as e:
        log.warning(f"Prop bot failed to start: {e}")

    try:
        if tradovate_bot_module is not None:
            import threading
            threading.Thread(target=tradovate_bot_module.run, daemon=True).start()
            log.info("📊 Tradovate bot thread started (stays inert until TRADOVATE_* credentials are set)")
    except Exception as e:
        log.warning(f"Tradovate bot failed to start: {e}")

    try:
        import subprocess
        import threading
        def start_crypto_bot():
            try:
                subprocess.Popen(['python', 'bot_2_crypto_scalper.py'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                log.info("🤖 Crypto scalper bot #2 started (background subprocess)")
            except Exception as e:
                log.warning(f"Crypto bot subprocess failed: {e}")

        threading.Thread(target=start_crypto_bot, daemon=True).start()
    except Exception as e:
        log.warning(f"Crypto bot startup failed: {e}")

    try:
        from stripe_subscriptions import setup_stripe_products
        if setup_stripe_products():
            log.info("💳 Stripe subscription products initialized")
        else:
            log.warning("Stripe products setup skipped - no API key configured")
    except Exception as e:
        log.warning(f"Stripe setup failed: {e}")

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


@app.get("/quote")
async def serve_quote_form():
    """Serve the subscription-aware video quote form"""
    try:
        # Try multiple possible paths for new subscription form first
        possible_paths = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "subscription_quote_form.html"),
            "/app/subscription_quote_form.html",
            "subscription_quote_form.html",
            # Fallback to old form if new one not found
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "quote_request.html"),
            "/app/quote_request.html",
            "quote_request.html",
        ]

        quote_path = None
        for path in possible_paths:
            if os.path.exists(path):
                quote_path = path
                break

        if not quote_path:
            log.error(f"Quote form HTML file not found in any location: {possible_paths}")
            raise HTTPException(status_code=404, detail="Quote form file not found")

        with open(quote_path, 'r') as f:
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


@app.get("/admin/bot-logs/crypto")
async def get_crypto_bot_logs(lines: int = 100):
    """Get last N lines from crypto trading bot log (bot2_crypto.log)"""
    log_lines = _read_log_file("bot2_crypto.log", lines)
    return {
        "service": "crypto-trading-bot",
        "log_file": "bot2_crypto.log",
        "lines_requested": lines,
        "lines_returned": len(log_lines),
        "logs": [line.rstrip('\n') for line in log_lines],
        "timestamp": datetime.now().isoformat()
    }


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


@app.get("/admin/bot-state")
async def get_bot_state():
    """Get current bot state (day count, win/loss ratio, peak portfolio, etc)"""
    state = _read_json_file("bot2_state.json")
    return {
        "service": "crypto-trading-bot",
        "state_file": "bot2_state.json",
        "data": state,
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
    """Get comprehensive bot status - logs, state, and P&L in one call"""
    crypto_logs = _read_log_file("bot2_crypto.log", 30)
    pl_logs = _read_log_file("logs/bot_pl_tracker.log", 20)
    state = _read_json_file("bot2_state.json")
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
            "crypto-trading-bot": {
                "log_file": "bot2_crypto.log",
                "recent_logs": [line.rstrip('\n') for line in crypto_logs],
                "state": state
            },
            "pl-tracker": {
                "log_file": "logs/bot_pl_tracker.log",
                "recent_logs": [line.rstrip('\n') for line in pl_logs],
                "latest_snapshot": latest_snapshot,
                "milestones_hit": history.get("milestones_hit", []) if isinstance(history, dict) else []
            }
        }
    }


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
