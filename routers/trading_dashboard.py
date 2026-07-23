"""
Trading Dashboard API - real Alpaca account data + withdrawal-request log.

Backs trading_dashboard.html (Bare Metal Builders). All read endpoints hit
Alpaca's real trading API for equity/cash/positions/orders - no mock data.

Alpaca's standard self-directed trading API (the same APCA-API-KEY-ID/
SECRET-KEY credentials prop_bot.py and tradovate_bot.py use) does not expose
a programmatic ACH/bank-transfer endpoint - that's only available through
Alpaca's own app, or through the separate Broker API product (a different
business relationship with Alpaca entirely). So "withdraw" here creates a
real database record of the request; the actual bank transfer has to be
done manually in Alpaca's app, and the request gets marked completed here
once you've done that - this is bookkeeping, not a real money-movement API.
"""

import os
import logging
from datetime import datetime, timezone

import aiohttp
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from admin_auth import require_admin_key
from models import TradingBotState, WithdrawalRequest

log = logging.getLogger("trading_dashboard")
router = APIRouter()

BOT_NAME = "bare_metal_builders"

ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

AUTO_COMPOUND_INCREMENT = 100.0


async def _get_or_create_state(db: AsyncSession, current_equity: float) -> TradingBotState:
    result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == BOT_NAME))
    state = result.scalar_one_or_none()
    if not state:
        # First time this runs - whatever's in the account right now becomes
        # the baseline. Profit is only what accrues above this point on.
        state = TradingBotState(bot_name=BOT_NAME, base_capital=current_equity)
        db.add(state)
        await db.commit()
        await db.refresh(state)
        log.info(f"Initialized base capital for {BOT_NAME}: ${current_equity:.2f}")
    return state


async def _fetch_alpaca_account(session: aiohttp.ClientSession) -> dict:
    async with session.get(f"{ALPACA_BASE_URL}/v2/account", headers=ALPACA_HEADERS) as r:
        if r.status != 200:
            body = await r.text()
            raise HTTPException(status_code=502, detail=f"Alpaca account fetch failed ({r.status}): {body}")
        return await r.json()


async def _fetch_alpaca_positions(session: aiohttp.ClientSession) -> list:
    async with session.get(f"{ALPACA_BASE_URL}/v2/positions", headers=ALPACA_HEADERS) as r:
        if r.status != 200:
            return []
        return await r.json()


async def _fetch_todays_filled_orders(session: aiohttp.ClientSession) -> list:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    params = {"status": "closed", "after": f"{today}T00:00:00Z", "direction": "desc", "limit": "100"}
    async with session.get(f"{ALPACA_BASE_URL}/v2/orders", headers=ALPACA_HEADERS, params=params) as r:
        if r.status != 200:
            return []
        orders = await r.json()
        return [o for o in orders if o.get("filled_at")]


@router.get("/status", dependencies=[Depends(require_admin_key)])
async def get_dashboard_status(db: AsyncSession = Depends(get_db)):
    """Real account snapshot: equity, cash, positions, today's trades, and
    profit/base-capital as tracked by us. Auto-compounds $100 increments of
    profit into base capital on every call (this endpoint is polled every
    30s by the dashboard, so that's effectively continuous)."""
    if not (ALPACA_KEY and ALPACA_SECRET):
        raise HTTPException(status_code=500, detail="Alpaca credentials not configured")

    async with aiohttp.ClientSession() as session:
        account = await _fetch_alpaca_account(session)
        positions = await _fetch_alpaca_positions(session)
        todays_orders = await _fetch_todays_filled_orders(session)

    equity = float(account["equity"])
    cash = float(account["cash"])
    buying_power = float(account["buying_power"])
    last_equity = float(account["last_equity"])
    session_pl = equity - last_equity
    session_pl_pct = (session_pl / last_equity * 100) if last_equity else 0.0

    state = await _get_or_create_state(db, equity)

    compounded = 0.0
    profit = equity - state.base_capital
    while profit >= AUTO_COMPOUND_INCREMENT:
        state.base_capital += AUTO_COMPOUND_INCREMENT
        profit -= AUTO_COMPOUND_INCREMENT
        compounded += AUTO_COMPOUND_INCREMENT
    if compounded > 0:
        await db.commit()
        await db.refresh(state)
        log.info(f"Auto-compounded ${compounded:.2f} into base capital | new base: ${state.base_capital:.2f}")

    result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.bot_name == BOT_NAME))
    all_withdrawals = result.scalars().all()
    total_withdrawn = sum(w.amount for w in all_withdrawals if w.status == "completed")

    return {
        "equity": equity,
        "cash": cash,
        "buying_power": buying_power,
        "session_pl": session_pl,
        "session_pl_pct": session_pl_pct,
        "active_positions": len(positions),
        "todays_trade_count": len(todays_orders),
        "base_capital": state.base_capital,
        "profit_available": max(profit, 0.0),
        "total_withdrawn": total_withdrawn,
        "auto_compounded_this_check": compounded,
        "live_trading": os.getenv("ALPACA_LIVE_TRADE", "false").lower() == "true",
        "stop_trading": os.getenv("STOP_TRADING", "false").lower() == "true",
    }


class WithdrawRequestBody(BaseModel):
    amount: float


@router.post("/withdraw-request", dependencies=[Depends(require_admin_key)])
async def create_withdrawal_request(payload: WithdrawRequestBody, db: AsyncSession = Depends(get_db)):
    """Logs a real withdrawal request. Does not move any money - the actual
    ACH transfer has to be done manually in Alpaca's app (see module
    docstring). Validates the amount against currently available profit so
    you can't request more than what's actually sitting there."""
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")

    async with aiohttp.ClientSession() as session:
        account = await _fetch_alpaca_account(session)
    equity = float(account["equity"])

    result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == BOT_NAME))
    state = result.scalar_one_or_none()
    if not state:
        raise HTTPException(status_code=400, detail="No base capital tracked yet - call /status first")

    available_profit = equity - state.base_capital
    if payload.amount > available_profit:
        raise HTTPException(
            status_code=400,
            detail=f"Requested ${payload.amount:.2f} exceeds available profit (${available_profit:.2f})",
        )

    withdrawal = WithdrawalRequest(bot_name=BOT_NAME, amount=payload.amount, status="requested")
    db.add(withdrawal)
    await db.commit()
    await db.refresh(withdrawal)
    log.info(f"Withdrawal requested: ${payload.amount:.2f} (id={withdrawal.id})")
    return withdrawal.to_dict()


@router.post("/withdraw-request/{withdrawal_id}/complete", dependencies=[Depends(require_admin_key)])
async def complete_withdrawal_request(withdrawal_id: int, db: AsyncSession = Depends(get_db)):
    """Mark a withdrawal request completed once you've actually done the
    real transfer manually in Alpaca's app."""
    withdrawal = await db.get(WithdrawalRequest, withdrawal_id)
    if not withdrawal:
        raise HTTPException(status_code=404, detail="Withdrawal request not found")
    if withdrawal.status == "completed":
        return withdrawal.to_dict()

    withdrawal.status = "completed"
    withdrawal.completed_at = datetime.utcnow()
    await db.commit()
    await db.refresh(withdrawal)
    log.info(f"Withdrawal marked completed: ${withdrawal.amount:.2f} (id={withdrawal.id})")
    return withdrawal.to_dict()


@router.get("/withdrawals", dependencies=[Depends(require_admin_key)])
async def list_withdrawals(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(WithdrawalRequest)
        .where(WithdrawalRequest.bot_name == BOT_NAME)
        .order_by(WithdrawalRequest.requested_at.desc())
    )
    withdrawals = result.scalars().all()
    return {"withdrawals": [w.to_dict() for w in withdrawals]}


@router.get("/crypto-status", dependencies=[Depends(require_admin_key)])
async def get_crypto_bot_status():
    """24/7 Crypto Scalp-Grid bot status (BTC, ETH, XRP)"""
    try:
        from crypto_scalp_grid_bot import get_status
        return get_status()
    except ImportError:
        raise HTTPException(status_code=503, detail="Crypto bot not available")
    except Exception as e:
        log.error(f"Crypto status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
