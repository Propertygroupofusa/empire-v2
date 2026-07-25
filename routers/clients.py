"""Client registration and profile - the customer side of the marketplace
(a client requests a job via routers/jobs.py, gets matched with a worker)."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from admin_auth import require_admin_key
from models import Client

log = logging.getLogger("clients")
router = APIRouter()


class ClientRegisterRequest(BaseModel):
    email: str
    name: str
    company: str = None
    phone: str = None


@router.post("/register")
async def register_client(payload: ClientRegisterRequest, db: AsyncSession = Depends(get_db)):
    """Creates or returns an existing client profile by email - idempotent
    so a client can re-submit the same intake form without erroring."""
    email = payload.email.lower()
    result = await db.execute(select(Client).where(Client.email == email))
    client = result.scalar_one_or_none()
    if client:
        return client.to_dict()

    client = Client(email=email, name=payload.name, company=payload.company, phone=payload.phone)
    db.add(client)
    await db.commit()
    await db.refresh(client)
    log.info(f"New client registered: {client.email} (id={client.id})")
    return client.to_dict()


@router.get("/{client_id}")
async def get_client(client_id: int, email: str, db: AsyncSession = Depends(get_db)):
    """Client-facing lookup, scoped by ID+email like the existing order
    customer-portal pattern (routers/orders.py's /customer/{order_id}) -
    client_id is a small sequential integer with no other protection, so
    the registered email is required as a second factor, and the same 404
    is returned whether the ID doesn't exist or the email doesn't match -
    this can't be used to enumerate which client IDs are in use."""
    client = await db.get(Client, client_id)
    if not client or client.email.lower() != email.lower():
        raise HTTPException(status_code=404, detail="Client not found")
    return client.to_dict()


@router.get("", dependencies=[Depends(require_admin_key)])
async def list_clients(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Client).order_by(Client.created_at.desc()))
    return {"clients": [c.to_dict() for c in result.scalars().all()]}
