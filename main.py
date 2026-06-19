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
import os
import uvicorn
import logging

from database import init_db
from routers import workers, clients, jobs, bookings, payments, admin, whitelabel, auth
from create_worker_manual import create_worker as seed_kisha

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pgusa")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("PGUSA Platform starting...")
    await init_db()
    log.info("Database initialized")
    try:
        await seed_kisha()
        log.info("Worker seed check complete")
    except Exception as e:
        log.error(f"Worker seed failed (non-fatal): {e}")
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
        }
    }


@app.get("/health")
async def health():
    return {"status": "ok", "platform": "pgusa-documents"}


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
