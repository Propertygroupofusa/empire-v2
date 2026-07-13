from fastapi import APIRouter
import asyncio
import logging

log = logging.getLogger("pgusa")

router = APIRouter()

async def payee_worker():
    """Background worker for Payee Trust webhook processing."""
    while True:
        await asyncio.sleep(60)

@router.post("/webhook")
async def webhook_receiver():
    return {"status": "ok"}
