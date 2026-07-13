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
<<<<<<< HEAD
from routers import workers, clients, jobs, bookings, payments, admin, whitelabel, auth, partners, labeling, outreach, trading_signals
from payee_webhook import router as payee_router, payee_worker
from paycom_features import router as payroll_router

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
    await init_db()
    await run_migrations()
    # Start Payee Trust async worker
    import asyncio
    asyncio.create_task(payee_worker())
    log.info("Payee Trust webhook worker started")
    log.info("Database initialized and migrations complete")
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
app.include_router(auth.router,        prefix="/auth",        tags=["Auth"])
app.include_router(workers.router,     prefix="/workers",     tags=["Workers"])
app.include_router(clients.router,     prefix="/clients",     tags=["Clients"])
app.include_router(jobs.router,        prefix="/jobs",        tags=["Jobs"])
app.include_router(bookings.router,    prefix="/bookings",    tags=["Bookings"])
app.include_router(payments.router,    prefix="/payments",    tags=["Payments"])
app.include_router(admin.router,       prefix="/admin",       tags=["Admin"])
app.include_router(whitelabel.router,  prefix="/whitelabel",  tags=["White Label"])
app.include_router(partners.router,    prefix="/partners",    tags=["Partners"])
app.include_router(labeling.router,    prefix="/labeling",    tags=["AI Labeling"])
app.include_router(payee_router,        prefix="/payee",       tags=["Payee Trust"])
app.include_router(payroll_router,      prefix="/workers/payroll", tags=["Worker Payroll"])
app.include_router(outreach.router,     prefix="/outreach",    tags=["Outreach & Campaigns"])
app.include_router(trading_signals.router, prefix="/trading", tags=["Trading Signals"])


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
