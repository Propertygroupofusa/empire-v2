"""
PROPERTY GROUP USA — DOCUMENTS PLATFORM BACKEND
=================================================
Full SaaS backend with worker management, client booking,
job matching, payments, admin dashboard, and white label API.
"""

from fastapi import FastAPI, HTTPException, Depends, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pgusa")


async def create_monitor_tables():
    """Create health monitor tables if they don't exist"""
    async with engine.begin() as conn:
        try:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS monitor_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS monitor_fixed_issues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS monitor_performance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric_data TEXT NOT NULL,
                    checked_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            log.info("Migration OK: monitor_performance table")
        except Exception as e:
            log.warning(f"Migration skip monitor_performance: {e}")

        try:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS monitor_errors_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS monitor_fixed_issues_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS monitor_performance_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS data_retention_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                await conn.execute(text(
                    f"ALTER TABLE workers ADD COLUMN IF NOT EXISTS {col} {col_type}"
                ))
                log.info(f"Migration OK: {col}")
            except Exception as e:
                log.warning(f"Migration skip {col}: {e}")


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


@app.get("/dashboard")
async def serve_dashboard():
    """Serve the social media dashboard HTML"""
    dashboard_path = os.path.join(os.path.dirname(__file__), "social_media_dashboard.html")
    if not os.path.exists(dashboard_path):
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return FileResponse(dashboard_path, media_type="text/html")


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
    return {"status": "ok", "platform": "pgusa-documents"}


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


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
