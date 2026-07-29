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

NUM_BOTS = int(os.getenv("PROP_NUM_BOTS", "8"))
if NUM_BOTS <= 0:
    raise ValueError("PROP_NUM_BOTS must be positive")

log = logging.getLogger("trading_dashboard")
router = APIRouter()

try:
    import prop_bot as prop_bot_module
except Exception as e:
    log.warning(f"prop_bot not importable, /signals will report unavailable: {e}")
    prop_bot_module = None

try:
    import crypto_coinbase_bot as crypto_coinbase_bot_module
except Exception as e:
    log.warning(f"crypto_coinbase_bot not importable, /crypto-coinbase-status will report unavailable: {e}")
    crypto_coinbase_bot_module = None

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
        # Buckets created before starting_capital existed have it as NULL -
        # backfill it to their current value the first time we see that, so
        # profit tracking (see _bot_profit) starts counting from right now
        # rather than inventing a retroactive history it has no record of.
        needs_backfill = [b for b in bots if b.starting_capital is None]
        if needs_backfill:
            for b in needs_backfill:
                b.starting_capital = b.base_capital
            await db.commit()
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
        bot = TradingBotState(bot_name=f"{BOT_PREFIX}{i}", base_capital=share, starting_capital=share)
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


def _bot_profit(bot: TradingBotState) -> float:
    """This bucket's real gain since it started - base_capital minus its
    never-updated starting_capital snapshot, floored at 0 (a bucket that's
    currently underwater has no profit to withdraw, even though its
    base_capital is still its own whole withdrawable balance)."""
    baseline = bot.starting_capital if bot.starting_capital is not None else bot.base_capital
    return max(0.0, bot.base_capital - baseline)


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
        try:
            orders = await r.json()
            return [o for o in orders if isinstance(o, dict) and o.get("filled_at")]
        except Exception as e:
            log.warning(f"Failed to parse orders: {e}")
            return []


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

    try:
        equity = float(account.get("equity", 0))
        cash = float(account.get("cash", 0))
        buying_power = float(account.get("buying_power", 0))
        last_equity = float(account.get("last_equity", equity))
    except (ValueError, TypeError) as e:
        log.error(f"Failed to parse account fields: {e}")
        raise HTTPException(status_code=502, detail="Invalid account data from Alpaca")

    session_pl = equity - last_equity
    session_pl_pct = (session_pl / last_equity * 100) if last_equity > 0 else 0.0

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
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "buying_power": round(buying_power, 2),
        "session_pl": round(session_pl, 2),
        "session_pl_pct": round(session_pl_pct, 2),
        "active_positions": len(positions),
        "todays_trade_count": len(todays_orders),
        "bots": [{"name": b.bot_name, "capital": round(b.base_capital, 2), "profit": round(_bot_profit(b), 2)} for b in bots],
        "total_committed_capital": round(total_committed, 2),
        "rebalanced_this_check": round(rebalanced, 2),
        "total_withdrawn": round(total_withdrawn, 2),
        "margin_multiplier": margin_multiplier if margin_multiplier is not None else 1.0,
        "shorting_enabled": shorting_enabled if shorting_enabled is not None else False,
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


@router.post("/withdraw-all-profit", dependencies=[Depends(require_admin_key)])
async def withdraw_all_profit(db: AsyncSession = Depends(get_db)):
    """One-tap version of create_withdrawal_request above, for every bot
    that currently has real profit instead of picking one bot at a time.
    Only ever requests each bucket's profit (base_capital minus its
    starting_capital snapshot - see _bot_profit), never its principal, and
    only for buckets where that's actually positive - a bucket sitting at
    or below its starting point gets no request. Same bookkeeping-only
    semantics as the single-bot endpoint: this logs the requests, it
    doesn't move money - still requires the real manual transfer in
    Alpaca's app, then confirming via complete_all_requested_withdrawals
    below (or the single complete endpoint, per request)."""
    async with aiohttp.ClientSession() as session:
        account = await _fetch_alpaca_account(session)
    equity = float(account["equity"])

    bots = await _get_or_init_bots(db, equity)
    created = []
    for bot in bots:
        profit = _bot_profit(bot)
        if profit < 0.01:
            continue
        withdrawal = WithdrawalRequest(bot_name=bot.bot_name, amount=profit, status="requested")
        db.add(withdrawal)
        created.append(withdrawal)

    if not created:
        return {"requested": [], "total": 0.0, "message": "No bot currently has profit above its starting capital"}

    await db.commit()
    for w in created:
        await db.refresh(w)
    total = sum(w.amount for w in created)
    log.info(f"Withdraw-all-profit: requested ${total:.2f} across {len(created)} bot(s)")
    return {"requested": [w.to_dict() for w in created], "total": round(total, 2)}


@router.post("/withdrawals/complete-all-requested", dependencies=[Depends(require_admin_key)])
async def complete_all_requested_withdrawals(db: AsyncSession = Depends(get_db)):
    """Bulk version of complete_withdrawal_request - confirms every
    currently 'requested' withdrawal (from either the single-bot or
    withdraw-all-profit endpoints) in one action, once you've actually
    done the real transfers manually in Alpaca's app for all of them."""
    result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.status == "requested"))
    pending = list(result.scalars().all())
    if not pending:
        return {"completed": [], "total": 0.0}

    bots_result = await db.execute(select(TradingBotState))
    bots_by_name = {b.bot_name: b for b in bots_result.scalars().all()}

    for withdrawal in pending:
        bot = bots_by_name.get(withdrawal.bot_name)
        if bot:
            bot.base_capital = max(bot.base_capital - withdrawal.amount, 0.0)
        withdrawal.status = "completed"
        withdrawal.completed_at = datetime.utcnow()

    await db.commit()
    for w in pending:
        await db.refresh(w)
    total = sum(w.amount for w in pending)
    log.info(f"Completed {len(pending)} withdrawal(s) totaling ${total:.2f}")
    return {"completed": [w.to_dict() for w in pending], "total": round(total, 2)}


@router.get("/withdrawals", dependencies=[Depends(require_admin_key)])
async def list_withdrawals(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(WithdrawalRequest).order_by(WithdrawalRequest.requested_at.desc()))
    withdrawals = result.scalars().all()
    return {"withdrawals": [w.to_dict() for w in withdrawals]}


@router.get("/trades", dependencies=[Depends(require_admin_key)])
async def get_todays_trades():
    """Detail behind /status's todays_trade_count - the actual filled
    orders (symbol, side, qty, fill price, time), not just a count. Same
    real Alpaca order history, just not collapsed to a number."""
    async with aiohttp.ClientSession() as session:
        orders = await _fetch_todays_filled_orders(session)

    return {
        "trades": [
            {
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "qty": o.get("filled_qty"),
                "price": o.get("filled_avg_price"),
                "filled_at": o.get("filled_at"),
            }
            for o in orders
        ]
    }


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


@router.get("/crypto-coinbase-status", dependencies=[Depends(require_admin_key)])
async def get_crypto_coinbase_status():
    """Same read-only in-memory view as /signals, but for
    crypto_coinbase_bot.py - the 24/7 BTC/ETH bot trading through a
    separate Coinbase account (Alpaca crypto is blocked for this
    account's state)."""
    if crypto_coinbase_bot_module is None:
        raise HTTPException(status_code=503, detail="crypto_coinbase_bot not available")

    return {
        "last_cycle_at": crypto_coinbase_bot_module.last_cycle_at,
        "daily_pnl": round(crypto_coinbase_bot_module.daily_pnl, 2),
        "open_positions": crypto_coinbase_bot_module.open_crypto_positions,
        "max_allocation": crypto_coinbase_bot_module.MAX_ALLOCATION,
        "rsi_buy_below": crypto_coinbase_bot_module.RSI_BUY_BELOW,
        "rsi_sell_above": crypto_coinbase_bot_module.RSI_SELL_ABOVE,
        "signals": crypto_coinbase_bot_module.latest_signals,
    }


# Chart-eligible symbols only - an explicit allowlist, checked before the
# symbol is ever interpolated into an outbound URL, so this endpoint can
# never be turned into an open SSRF proxy via an arbitrary path param.
# Same tickers prop_bot.py/crypto_coinbase_bot.py already trade.
CHART_STOCK_SYMBOLS = {"SPY", "QQQ", "DIA", "IWM", "GLD", "USO", "SLV"}
CHART_CRYPTO_SYMBOLS = {"BTC-USD", "ETH-USD"}


def _rolling_rsi(closes: list, period: int = 14) -> list:
    """Same simple-rolling-average RSI prop_bot.py/crypto_coinbase_bot.py
    use for their own trade decisions (get_price_rsi), computed at every
    point instead of just the latest one, so the chart's RSI line matches
    exactly what the bot itself was seeing at each point in time."""
    n = len(closes)
    rsi_series = [None] * n
    if n <= period:
        return rsi_series
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, n)]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, n)]
    for i in range(period, n):
        avg_gain = sum(gains[i - period:i]) / period
        avg_loss = sum(losses[i - period:i]) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else 100
        rsi_series[i] = round(100 - (100 / (1 + rs)), 1)
    return rsi_series


@router.get("/price-history/{symbol}", dependencies=[Depends(require_admin_key)])
async def get_price_history(symbol: str):
    """Real OHLC candles + an RSI series for the dashboard's live chart -
    fetched fresh from the exact same public data sources the bots already
    use for their own RSI calc (Alpaca bars for the stock proxies,
    Coinbase's public candles for BTC/ETH). Nothing new is stored; this is
    a read-only view computed on each request, same spirit as /signals."""
    symbol = symbol.upper()

    async with aiohttp.ClientSession() as session:
        if symbol in CHART_STOCK_SYMBOLS:
            if not (ALPACA_KEY and ALPACA_SECRET):
                raise HTTPException(status_code=500, detail="Alpaca credentials not configured")
            url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=5Min&limit=100"
            async with session.get(url, headers=ALPACA_HEADERS) as r:
                if r.status != 200:
                    raise HTTPException(status_code=502, detail=f"Alpaca bars request failed ({r.status})")
                data = await r.json()
            candles = [
                {"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]}
                for b in data.get("bars", [])
            ]
        elif symbol in CHART_CRYPTO_SYMBOLS:
            url = f"https://api.exchange.coinbase.com/products/{symbol}/candles?granularity=300"
            async with session.get(url, headers={"Accept": "application/json"}) as r:
                if r.status != 200:
                    raise HTTPException(status_code=502, detail=f"Coinbase candles request failed ({r.status})")
                data = await r.json()
            # Coinbase returns newest-first; each row is [time, low, high, open, close, volume].
            rows = list(reversed(data or []))[-100:]
            candles = [
                {
                    "t": datetime.fromtimestamp(row[0], tz=timezone.utc).isoformat(),
                    "o": row[3], "h": row[2], "l": row[1], "c": row[4],
                }
                for row in rows
            ]
        else:
            raise HTTPException(status_code=404, detail=f"Unknown chart symbol: {symbol}")

    closes = [c["c"] for c in candles]
    return {"symbol": symbol, "candles": candles, "rsi": _rolling_rsi(closes)}


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
        try:
            activities = await _fetch_dividend_activities(session)
            positions = await _fetch_alpaca_positions(session)
        except Exception as e:
            log.error(f"Failed to fetch dividend data: {e}")
            raise HTTPException(status_code=502, detail="Failed to fetch dividend data")

    by_symbol = {}
    total_received = 0.0
    for a in activities:
        if not isinstance(a, dict):
            continue
        symbol = a.get("symbol") or "UNKNOWN"
        try:
            amount = float(a.get("net_amount") or a.get("amount") or 0)
        except (ValueError, TypeError):
            continue
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
