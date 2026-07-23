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

try:
    import prop_bot as prop_bot_module
except Exception as e:
    log.warning(f"prop_bot not importable, /signals will report unavailable: {e}")
    prop_bot_module = None

ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

# Pre-8-bot names, kept only to migrate whatever they were already
# tracking into the new bot_N buckets the first time this runs.
LEGACY_BOT_NAME = "bare_metal_builders"
LEGACY_MIRROR_PREFIX = "mirror_"

# The account owner chose real per-bot withdrawal, not just a display
# split: NUM_BOTS named buckets, each individually withdrawable, all
# compounding together since every dollar is still the same real Alpaca
# equity and the same real trades prop_bot.py places. A bot_N row's
# base_capital means "this bucket's current tracked value" (its whole
# balance is withdrawable), not a fixed floor like the old single-bucket
# design - whatever's left after a withdrawal keeps compounding.
NUM_BOTS = int(os.getenv("PROP_NUM_BOTS", "8"))
BOT_PREFIX = "bot_"

# Reg T's real minimum equity to open a margin account (and therefore be
# eligible for shorting) - a federal requirement, not an Alpaca setting or
# anything this app can change. Confirmed directly against the real
# account: multiplier stayed at 1 (cash-account behavior) even after
# selecting a margin multiplier preference in Alpaca's UI, because the
# account sits below this threshold.
MARGIN_MIN_EQUITY = 2000.0


async def _get_or_init_bots(db: AsyncSession, current_equity: float) -> list:
    """Fetches the NUM_BOTS tracked buckets, creating them on first call.
    One-time migration: folds in whatever the old single-bucket/mirror
    design was already tracking (if anything) and splits it evenly across
    NUM_BOTS; otherwise splits the real current equity evenly instead."""
    result = await db.execute(
        select(TradingBotState).where(TradingBotState.bot_name.like(f"{BOT_PREFIX}%")).order_by(TradingBotState.bot_name)
    )
    bots = list(result.scalars().all())
    if bots:
        return bots

    legacy_result = await db.execute(
        select(TradingBotState).where(
            (TradingBotState.bot_name == LEGACY_BOT_NAME) | (TradingBotState.bot_name.like(f"{LEGACY_MIRROR_PREFIX}%"))
        )
    )
    legacy_rows = list(legacy_result.scalars().all())
    starting_total = sum(r.base_capital for r in legacy_rows) if legacy_rows else current_equity

    share = starting_total / NUM_BOTS
    bots = []
    for i in range(1, NUM_BOTS + 1):
        bot = TradingBotState(bot_name=f"{BOT_PREFIX}{i}", base_capital=share)
        db.add(bot)
        bots.append(bot)
    for r in legacy_rows:
        await db.delete(r)

    await db.commit()
    for bot in bots:
        await db.refresh(bot)
    log.info(f"Initialized {NUM_BOTS} bots at ${share:.2f} each (migrated ${starting_total:.2f} from legacy tracking)")
    return bots


def _rebalance_bots(bots: list, equity: float) -> float:
    """Distributes whatever changed in real equity since the bots' tracked
    total was last synced, proportionally to each bot's current share -
    every bucket compounds (or draws down) together with real trading
    results, none singled out. Returns the raw change applied (positive or
    negative, 0.0 if nothing to apply) - mutates the bot objects in place,
    caller still needs to commit."""
    total_tracked = sum(b.base_capital for b in bots)
    change = equity - total_tracked

    if abs(change) < 0.005:
        return 0.0

    if total_tracked <= 0:
        # Every bucket has been fully drawn down - nowhere to proportion
        # the change against, so it goes to the first bucket.
        bots[0].base_capital += change
        return change

    for bot in bots:
        bot.base_capital += change * (bot.base_capital / total_tracked)
    return change


async def _fetch_dividend_activities(session: aiohttp.ClientSession) -> list:
    """Real dividend cash actually paid into the account, from Alpaca's
    account-activities history (activity_type=DIV) - not a projection or
    estimate. Alpaca's standard trading API doesn't expose forward-looking
    ex-dividend/payment-date schedules (that needs a separate
    corporate-actions data entitlement this account may not have), so this
    only ever reflects dividends already received."""
    params = {"activity_types": "DIV", "direction": "desc", "page_size": "100"}
    async with session.get(f"{ALPACA_BASE_URL}/v2/account/activities", headers=ALPACA_HEADERS, params=params) as r:
        if r.status != 200:
            return []
        return await r.json()


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
    each of the NUM_BOTS tracked buckets' current share. Every poll,
    whatever changed in real equity since the last check gets distributed
    proportionally across all bots (see _rebalance_bots) so they all
    compound together in real time."""
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

    # Real account.multiplier/shorting_enabled fields - confirmed via a
    # direct account check that this account sits at multiplier=1
    # (cash-account behavior) despite "Shorting Enabled" being toggled on
    # in Alpaca's UI, because margin hasn't actually been granted yet.
    # MARGIN_MIN_EQUITY is Reg T's real ~$2,000 minimum to open a margin
    # account - not a setting either side of this app can change; shown
    # purely so the dashboard reflects why shorting is blocked instead of
    # that only being visible by manually querying the account.
    margin_multiplier = account.get("multiplier")
    shorting_enabled = account.get("shorting_enabled")

    bots = await _get_or_init_bots(db, equity)
    rebalanced = _rebalance_bots(bots, equity)
    if rebalanced != 0.0:
        await db.commit()
        for bot in bots:
            await db.refresh(bot)
        log.info(f"Rebalanced ${rebalanced:+.2f} across {len(bots)} bots proportionally to their current share")

    total_committed = sum(b.base_capital for b in bots)

    result = await db.execute(select(WithdrawalRequest))
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
        "bots": [{"name": b.bot_name, "capital": round(b.base_capital, 2)} for b in bots],
        "total_committed_capital": round(total_committed, 2),
        "rebalanced_this_check": round(rebalanced, 2),
        "total_withdrawn": total_withdrawn,
        "margin_multiplier": margin_multiplier,
        "shorting_enabled": shorting_enabled,
        "margin_min_equity": MARGIN_MIN_EQUITY,
        "live_trading": os.getenv("ALPACA_LIVE_TRADE", "false").lower() == "true",
        "stop_trading": os.getenv("STOP_TRADING", "false").lower() == "true",
    }


class WithdrawRequestBody(BaseModel):
    bot_name: str
    amount: float


@router.post("/withdraw-request", dependencies=[Depends(require_admin_key)])
async def create_withdrawal_request(payload: WithdrawRequestBody, db: AsyncSession = Depends(get_db)):
    """Logs a real withdrawal request against one specific bot's tracked
    capital. Does not move any money - the actual ACH transfer has to be
    done manually in Alpaca's app (see module docstring). Each bot's
    entire tracked balance is individually withdrawable (no separate
    floor/profit split per bucket) - validates against that bot's own
    current share, not the account total."""
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")

    async with aiohttp.ClientSession() as session:
        account = await _fetch_alpaca_account(session)
    equity = float(account["equity"])

    bots = await _get_or_init_bots(db, equity)
    bot = next((b for b in bots if b.bot_name == payload.bot_name), None)
    if not bot:
        valid_names = ", ".join(b.bot_name for b in bots)
        raise HTTPException(status_code=400, detail=f"Unknown bot '{payload.bot_name}' - must be one of: {valid_names}")

    if payload.amount > bot.base_capital:
        raise HTTPException(
            status_code=400,
            detail=f"Requested ${payload.amount:.2f} exceeds {bot.bot_name}'s tracked capital (${bot.base_capital:.2f})",
        )

    withdrawal = WithdrawalRequest(bot_name=bot.bot_name, amount=payload.amount, status="requested")
    db.add(withdrawal)
    await db.commit()
    await db.refresh(withdrawal)
    log.info(f"Withdrawal requested from {bot.bot_name}: ${payload.amount:.2f} (id={withdrawal.id})")
    return withdrawal.to_dict()


@router.post("/withdraw-request/{withdrawal_id}/complete", dependencies=[Depends(require_admin_key)])
async def complete_withdrawal_request(withdrawal_id: int, db: AsyncSession = Depends(get_db)):
    """Mark a withdrawal request completed once you've actually done the
    real transfer manually in Alpaca's app - this is also when the
    specific bot's tracked capital actually gets reduced by the withdrawn
    amount, so the next /status rebalance correctly treats the transfer as
    money that left (attributed to that one bot), not as trading loss
    smeared proportionally across every bot."""
    withdrawal = await db.get(WithdrawalRequest, withdrawal_id)
    if not withdrawal:
        raise HTTPException(status_code=404, detail="Withdrawal request not found")
    if withdrawal.status == "completed":
        return withdrawal.to_dict()

    result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == withdrawal.bot_name))
    bot = result.scalar_one_or_none()
    if bot:
        bot.base_capital = max(bot.base_capital - withdrawal.amount, 0.0)

    withdrawal.status = "completed"
    withdrawal.completed_at = datetime.utcnow()
    await db.commit()
    await db.refresh(withdrawal)
    log.info(f"Withdrawal marked completed: ${withdrawal.amount:.2f} from {withdrawal.bot_name} (id={withdrawal.id})")
    return withdrawal.to_dict()


@router.get("/withdrawals", dependencies=[Depends(require_admin_key)])
async def list_withdrawals(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(WithdrawalRequest).order_by(WithdrawalRequest.requested_at.desc()))
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


@router.get("/signals", dependencies=[Depends(require_admin_key)])
async def get_live_signals():
    """Live per-symbol price/RSI/trend from prop_bot.py's most recent scan
    cycle - the same numbers that were previously only visible in Railway
    logs. Read-only view into the bot's in-memory state (same process,
    same thread's module-level dict) - this endpoint doesn't call Alpaca
    itself, so it's cheap enough to poll every 30s alongside /status."""
    if prop_bot_module is None:
        raise HTTPException(status_code=503, detail="prop_bot not available")

    return {
        "last_cycle_at": prop_bot_module.last_cycle_at,
        "market_open": prop_bot_module.last_market_open,
        "rsi_buy_below": prop_bot_module.RSI_BUY_BELOW,
        "rsi_sell_above": prop_bot_module.RSI_SELL_ABOVE,
        "signals": prop_bot_module.latest_signals,
    }


@router.get("/dividends", dependencies=[Depends(require_admin_key)])
async def get_dividend_tracker():
    """Real dividend income received into the account, grouped by symbol -
    pulled straight from Alpaca's account-activities history (activity
    type DIV), not estimated or projected. Dividend cash lands in the same
    real cash balance /status already tracks, so it's already covered by
    the existing withdraw-profit flow - there's no separate "dividend
    withdrawal" to build. Forward-looking payment schedules (next
    ex-dividend date, yield) aren't shown here - Alpaca's standard trading
    API doesn't expose that; it needs a separate corporate-actions data
    entitlement this account may not have, and this endpoint won't guess."""
    if not (ALPACA_KEY and ALPACA_SECRET):
        raise HTTPException(status_code=500, detail="Alpaca credentials not configured")

    async with aiohttp.ClientSession() as session:
        activities = await _fetch_dividend_activities(session)
        positions = await _fetch_alpaca_positions(session)

    by_symbol = {}
    total_received = 0.0
    for a in activities:
        symbol = a.get("symbol") or "UNKNOWN"
        amount = float(a.get("net_amount") or a.get("amount") or 0)
        entry = by_symbol.setdefault(symbol, {"symbol": symbol, "total_received": 0.0, "payment_count": 0, "last_payment_date": None})
        entry["total_received"] += amount
        entry["payment_count"] += 1
        payment_date = a.get("date")
        if payment_date and (entry["last_payment_date"] is None or payment_date > entry["last_payment_date"]):
            entry["last_payment_date"] = payment_date
        total_received += amount

    return {
        "total_dividends_received": round(total_received, 2),
        "dividend_payers": sorted(
            ({**d, "total_received": round(d["total_received"], 2)} for d in by_symbol.values()),
            key=lambda d: -d["total_received"],
        ),
        "currently_held_symbols": sorted({p["symbol"] for p in positions}),
    }
