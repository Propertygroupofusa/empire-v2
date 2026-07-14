"""
PROPERTY GROUP USA — DOCUMENTS PLATFORM BACKEND
=================================================
Full SaaS backend with worker management, client booking,
job matching, payments, admin dashboard, and white label API.
"""

from fastapi import FastAPI, HTTPException, Depends, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from sqlalchemy import text
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pgusa")


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


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
