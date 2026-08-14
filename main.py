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