"""Shared admin-only auth dependency for business-data/aggregate endpoints.

Several endpoints (dashboards, stats, admin subscriber/order listings) expose
full revenue figures and customer PII (names, emails, scripts) across every
customer, with no legitimate anonymous or per-customer use case. None of the
live frontends call them. Gate them behind a single shared admin key rather
than leaving them open to anyone who finds the URL.
"""
import os
from fastapi import Header, HTTPException


async def require_admin_key(x_admin_key: str = Header(None)):
    """FastAPI dependency: raises unless X-Admin-Key matches ADMIN_API_KEY.

    Fails closed - if ADMIN_API_KEY isn't configured, access is denied
    rather than silently left open, since these endpoints have no other
    protection at all.
    """
    admin_key = os.getenv("ADMIN_API_KEY")
    if not admin_key:
        raise HTTPException(status_code=500, detail="Admin API key not configured")
    if not x_admin_key or x_admin_key != admin_key:
        raise HTTPException(status_code=401, detail="Invalid or missing admin key")
