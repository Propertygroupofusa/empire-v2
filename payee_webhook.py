from fastapi import APIRouter
import asyncio

router = APIRouter()

async def payee_worker():
    """Placeholder payee webhook worker"""
    while True:
        await asyncio.sleep(60)
