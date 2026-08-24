"""
Trading Dashboard API - real Alpaca account data + withdrawal-request log.

Backs trading_dashboard.html (Bare Metal Builders). All read endpoints hit
Alpaca's real trading API for equity/cash/positions/orders - no mock data.

Alpaca's standard self-directed trading API (the same APCA-API-KEY-ID/
SECRET-KEY credentials prop_bot.py uses) does not expose
a programmatic ACH/bank-transfer endpoint - that's only available through
Alpaca's own app, or through the separate Broker API product (a different
business relationship with Alpaca entirely). So "withdraw" here creates a
real database record of the request; the actual bank transfer has to be
done manually in Alpaca's app, and the request gets marked completed here
once you've done that - this is bookkeeping, not a real money-movement API.
"""

import os
import logging
import asyncio
import uuid
from datetime import datetime, timezone, timedelta

import aiohttp
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func, case
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, AsyncSessionLocal
from admin_auth import require_admin_key
from models import TradingBotState, WithdrawalRequest, CryptoTreeBranch, BotPosition, Payment, CryptoCoinTradeHistory

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

try:
    import crypto_family_tree_bot as crypto_family_tree_bot_module
except Exception as e:
    log.warning(f"crypto_family_tree_bot not importable, /family-tree-status won't include locked profit: {e}")
    crypto_family_tree_bot_module = None

try:
    import crypto_selection_backtest as crypto_selection_backtest_module
except Exception as e:
    log.warning(f"crypto_selection_backtest not importable, /crypto-selection-backtest will report unavailable: {e}")
    crypto_selection_backtest_module = None

try:
    import alpaca_selection_backtest as alpaca_selection_backtest_module
except Exception as e:
    log.warning(f"alpaca_selection_backtest not importable, /alpaca-selection-backtest will report unavailable: {e}")
    alpaca_selection_backtest_module = None

ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

# Real, unattended position management - per the account owner's explicit
# request. No bot enforces either of these today: exits were otherwise
# only RSI/profit-target/stop-loss driven per-bot, and several real open
# positions (e.g. AMZD, YUM) don't even match a known bot's symbol
# universe (see _fetch_position_opened_at's docstring) - so this acts on
# every real open Alpaca position account-wide, not inside any one bot's
# already-narrow logic. 8% chosen over the 33% first floated: the swing
# bot's own declared convention for this kind of hold is 5%, and 33%
# would essentially never fire in a 10-day window for these symbols,
# making the max-hold timeout do all the work instead of ever taking a
# real profit early.
ALPACA_AUTO_CLOSE_PROFIT_PCT = float(os.getenv("ALPACA_AUTO_CLOSE_PROFIT_PCT", "0.08"))
ALPACA_AUTO_CLOSE_MAX_HOLD_DAYS = float(os.getenv("ALPACA_AUTO_CLOSE_MAX_HOLD_DAYS", "10"))
ALPACA_AUTO_CLOSE_CHECK_INTERVAL_SECONDS = int(os.getenv("ALPACA_AUTO_CLOSE_CHECK_INTERVAL_SECONDS", "900"))
# Same 10%-of-realized-profit pattern crypto_family_tree_bot.py uses (never
# on a loss) - the other 90% + principal just returns to the account's
# real buying power on close, no reinvestment decision made here.
ALPACA_PROFIT_SKIM_PCT = float(os.getenv("ALPACA_PROFIT_SKIM_PCT", "0.10"))
ALPACA_LOCKED_PROFIT_KEY = "alpaca_locked_usd"

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
    return max(0.0, _bot_pl(bot))


def _bot_pl(bot: TradingBotState) -> float:
    """Same delta as _bot_profit but NOT floored at 0 - the real signed
    gain or loss since this bucket started. _bot_profit's floor exists for
    withdrawal eligibility (you can't withdraw a loss), which is a
    different question from "is this bucket actually up or down" - a
    waterfall/bridge chart needs the real signed number, not the
    withdrawal-eligible one, or a bucket that's underwater would silently
    show as flat instead of red."""
    baseline = bot.starting_capital if bot.starting_capital is not None else bot.base_capital
    return bot.base_capital - baseline


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


async def _fetch_position_opened_at(session: aiohttp.ClientSession, symbol: str) -> str:
    """Real fill time of the most recent buy order for this symbol - the
    trade that actually opened the position currently held. Alpaca's
    /v2/positions doesn't include an open date itself, so this is
    reconstructed from real order history (same source /trades/closed
    above already trusts) rather than guessed or read from any bot's own
    bookkeeping - not every position open in the account was necessarily
    opened by code in this repo, so a bot's own tables aren't a reliable
    source here."""
    params = {"status": "closed", "symbols": symbol, "direction": "desc", "limit": "50"}
    async with session.get(f"{ALPACA_BASE_URL}/v2/orders", headers=ALPACA_HEADERS, params=params) as r:
        if r.status != 200:
            return None
        try:
            orders = await r.json()
        except Exception:
            return None
    for o in orders:
        if isinstance(o, dict) and o.get("side") == "buy" and o.get("filled_at"):
            return o["filled_at"]
    return None


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


@router.get("/trades/closed")
async def get_closed_trades():
    """Get all closed trades with real entry/exit prices and P&L.
    Shows each trade individually, not bucketed by bot."""
    if not (ALPACA_KEY and ALPACA_SECRET):
        raise HTTPException(status_code=500, detail="Alpaca credentials not configured")

    try:
        async with aiohttp.ClientSession() as session:
            # Fetch closed orders from last 30 days
            params = {"status": "closed", "direction": "desc", "limit": "500"}
            async with session.get(f"{ALPACA_BASE_URL}/v2/orders", headers=ALPACA_HEADERS, params=params) as r:
                if r.status != 200:
                    raise HTTPException(status_code=502, detail="Failed to fetch orders from Alpaca")
                orders = await r.json()

            # Group buy/sell pairs to calculate P&L per round-trip trade
            trades = []
            buy_orders = {}

            for order in orders:
                if not isinstance(order, dict) or not order.get("filled_at"):
                    continue

                symbol = order.get("symbol", "?")
                side = order.get("side", "").lower()
                qty = float(order.get("filled_qty", 0))
                price = float(order.get("filled_avg_price", 0))
                filled_at = order.get("filled_at", "")

                if side == "buy":
                    buy_orders[symbol] = {
                        "qty": qty,
                        "price": price,
                        "filled_at": filled_at,
                    }
                elif side == "sell" and symbol in buy_orders:
                    buy = buy_orders.pop(symbol)
                    entry_price = buy["price"]
                    exit_price = price
                    entry_qty = buy["qty"]

                    # Calculate P&L
                    gross_pnl = (exit_price - entry_price) * min(entry_qty, qty)
                    pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price > 0 else 0

                    trades.append({
                        "symbol": symbol,
                        "entry_price": round(entry_price, 2),
                        "exit_price": round(exit_price, 2),
                        "qty": round(min(entry_qty, qty), 2),
                        "entry_at": buy["filled_at"],
                        "exit_at": filled_at,
                        "pnl": round(gross_pnl, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "status": "closed",
                    })

            # Sort by exit date, newest first
            trades.sort(key=lambda t: t.get("exit_at", ""), reverse=True)

            total_pnl = sum(t["pnl"] for t in trades)
            winning_trades = len([t for t in trades if t["pnl"] > 0])
            losing_trades = len([t for t in trades if t["pnl"] < 0])

            return {
                "trades": trades[:50],  # Last 50 closed trades
                "total_trades": len(trades),
                "total_pnl": round(total_pnl, 2),
                "winning_trades": winning_trades,
                "losing_trades": losing_trades,
                "win_rate": round(winning_trades / len(trades) * 100, 1) if trades else 0,
            }
    except Exception as e:
        log.error(f"Failed to get closed trades: {e}")
        raise HTTPException(status_code=502, detail=str(e))


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
        "bots": [{"name": b.bot_name, "capital": round(b.base_capital, 2), "profit": round(_bot_profit(b), 2), "pl": round(_bot_pl(b), 2)} for b in bots],
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


@router.get("/family-tree-status", dependencies=[Depends(require_admin_key)])
async def get_family_tree_status(db: AsyncSession = Depends(get_db)):
    """Real DB state of every crypto_family_tree_bot.py branch. Unlike
    /crypto-coinbase-status above, there's no single in-memory module dict
    to read here - each branch runs as its own independent thread, so the
    CryptoTreeBranch/BotPosition rows in the database are the only place a
    branch's live state actually exists. Backs family_tree_dashboard.html."""
    branches_result = await db.execute(select(CryptoTreeBranch).order_by(CryptoTreeBranch.created_at))
    branches = list(branches_result.scalars().all())

    positions_by_bot = {}
    if branches:
        positions_result = await db.execute(
            select(BotPosition).where(BotPosition.bot.in_([b.bot_name for b in branches]))
        )
        for p in positions_result.scalars().all():
            positions_by_bot[p.bot] = p

    # Real live price per open position, so the dashboard can show a real
    # unrealized P&L and - per the account owner - only ever offer the
    # manual "Sell now" button on a position that's ACTUALLY in profit
    # right now, not just one that's still holding. entry_price/target/stop
    # alone can't answer that; the position needs to be marked to the
    # current real market price like every other unrealized-P&L figure
    # elsewhere in this file already is.
    #
    # Also fetch the real Coinbase cash balance here (same session) so the
    # dashboard can show the real spendable-for-a-new-branch figure and
    # grey out the "Start new $50 branch" button BEFORE it's clicked,
    # instead of only failing after - see spawn_family_tree_branch() below
    # for why this can't just subtract every branch's allocated_usd.
    current_price_by_bot = {}
    real_balance = None
    if crypto_family_tree_bot_module is not None:
        engine = crypto_family_tree_bot_module.engine
        async with engine.aiohttp.ClientSession() as session:
            for bot_name, pos in positions_by_bot.items():
                price, _atr_pct = await engine.get_price_and_volatility(session, pos.symbol)
                if price is not None:
                    current_price_by_bot[bot_name] = price
            real_balance, _err = await engine.get_usd_balance(session)

    out = []
    for b in branches:
        pos = positions_by_bot.get(b.bot_name)
        current_price = current_price_by_bot.get(b.bot_name) if pos else None
        # Real reason the branch's last order was rejected by Coinbase (if
        # any), e.g. "INVALID_ARGUMENT: ..." or "PERMISSION_DENIED: ..." -
        # only ever set when a real buy/sell attempt on this exact
        # product_id failed, cleared the moment one succeeds. Surfaced here
        # so a real order-rejection is visible directly on the dashboard,
        # not just in a Railway log line that gets truncated on mobile.
        last_order_error = (
            crypto_family_tree_bot_module.engine._last_order_error.get(b.product_id)
            if crypto_family_tree_bot_module is not None else None
        )
        out.append({
            "bot_name": b.bot_name,
            "product_id": b.product_id,
            "parent_bot_name": b.parent_bot_name,
            "allocated_usd": round(b.allocated_usd, 2),
            "equity_floor": round(b.equity_floor, 2),
            "next_unlock_tier": round(b.next_unlock_tier, 2),
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "last_order_error": last_order_error,
            "position": None if pos is None else {
                "symbol": pos.symbol,
                "entry_price": pos.entry_price,
                "qty": pos.qty,
                "target_price": pos.target_price,
                "stop_price": pos.stop_price,
                "opened_at": pos.opened_at.isoformat() if pos.opened_at else None,
                "current_price": current_price,
                "unrealized_pct": round((current_price / pos.entry_price - 1) * 100, 2) if current_price else None,
                # Coinbase takes its real trading fee out of the crypto you
                # get back on a buy - it's not a separate line item anywhere,
                # it's baked into pos.qty already. This is the same estimate
                # _branch_sell_and_settle uses for the sell-side fee (half
                # of ROUND_TRIP_FEE_RATE applied to the trade's dollar
                # value), so the dashboard can show what it actually cost to
                # get into this coin.
                "entry_fee_usd": round(
                    pos.entry_price * pos.qty * (crypto_family_tree_bot_module.ROUND_TRIP_FEE_RATE / 2), 2
                ) if crypto_family_tree_bot_module is not None else None,
                # Backs the dashboard's 💡 Sell advice button - reuses the
                # bot's own real TARGET/STOP/GIVEBACK exit checks (see
                # compute_sell_advice) so the advice can never disagree with
                # what the bot is actually about to do on its own.
                "sell_advice": crypto_family_tree_bot_module.compute_sell_advice(
                    pos.entry_price, pos.qty, pos.target_price, pos.stop_price,
                    current_price, pos.peak_pct,
                ) if crypto_family_tree_bot_module is not None and current_price is not None else None,
                # Real historical context alongside the live verdict above -
                # the same CryptoBacktestRun data crypto_selection_backtest.html's
                # own table already shows for this exact coin, per the
                # account owner's explicit request to have the sell advice
                # draw on that real backtest system too, not just live
                # TARGET/STOP/GIVEBACK math. Purely informational - never
                # changes the verdict itself, which stays tied to what the
                # bot is actually about to do right now.
                "historical_backtest": (
                    await crypto_family_tree_bot_module.get_latest_backtest_result(b.product_id)
                ) if crypto_family_tree_bot_module is not None else None,
            },
        })

    locked_usd = 0.0
    if crypto_family_tree_bot_module is not None:
        locked_usd = round(await crypto_family_tree_bot_module.get_locked_usd(), 2)

    # Real spendable-for-a-new-branch figure: only FLAT branches (no open
    # position) are actually competing for the shared real cash pool right
    # now - a branch holding an open position has already deployed its
    # allocated_usd into crypto, so subtracting it again here would be
    # comparing cash-only balance against money that isn't cash anymore.
    spendable_for_spawn = None
    can_spawn = False
    seed_usd = round(crypto_family_tree_bot_module.SEED_USD, 2) if crypto_family_tree_bot_module is not None else None
    if real_balance is not None:
        flat_allocated_sum = sum(b.allocated_usd for b in branches if b.bot_name not in positions_by_bot)
        spendable_for_spawn = round(real_balance - locked_usd - flat_allocated_sum, 2)
        can_spawn = seed_usd is not None and spendable_for_spawn >= seed_usd

    return {
        "branches": out,
        "branch_count": len(out),
        "total_allocated_usd": round(sum(b["allocated_usd"] for b in out), 2),
        "locked_usd": locked_usd,
        "spendable_for_spawn": spendable_for_spawn,
        "seed_usd": seed_usd,
        "can_spawn": can_spawn,
    }


@router.get("/family-tree-status/coin-history", dependencies=[Depends(require_admin_key)])
async def get_coin_trade_history(db: AsyncSession = Depends(get_db)):
    """Real per-coin trade history and P&L, per the account owner's
    explicit request: since branches switch coins over time and different
    branches can independently trade the SAME coin at different points,
    this is grouped by product_id (not by branch) - buying SOL back after
    having sold it before picks up right where its history left off
    ("the third time he bought Sol he sold it for this price, and so far
    the profit has been X") rather than resetting every time some branch
    happens to hold it. Backed by CryptoCoinTradeHistory, written once per
    real completed sell in crypto_family_tree_bot.py's
    _branch_sell_and_settle(). Coins with no trades yet simply don't
    appear - there's nothing real to show for them."""
    agg_result = await db.execute(
        select(
            CryptoCoinTradeHistory.product_id,
            func.count(CryptoCoinTradeHistory.id).label("trade_count"),
            func.sum(CryptoCoinTradeHistory.pnl).label("total_pnl"),
            func.avg(CryptoCoinTradeHistory.pnl).label("avg_pnl"),
            func.sum(case((CryptoCoinTradeHistory.pnl > 0, 1), else_=0)).label("win_count"),
        ).group_by(CryptoCoinTradeHistory.product_id)
    )
    coins = []
    for row in agg_result.all():
        trade_count = row.trade_count
        win_count = row.win_count or 0
        coins.append({
            "product_id": row.product_id,
            "trade_count": trade_count,
            "total_pnl": round(row.total_pnl or 0.0, 2),
            "avg_pnl": round(row.avg_pnl or 0.0, 2),
            "win_count": win_count,
            "win_rate": round(100.0 * win_count / trade_count, 1) if trade_count else 0.0,
        })
    coins.sort(key=lambda c: abs(c["total_pnl"]), reverse=True)

    # Individual trades, most recent first - the dashboard shows these
    # nested under each coin so a real history like "3rd SOL trade, sold
    # at $X, running total $Y" is readable, not just the aggregate.
    trades_result = await db.execute(
        select(CryptoCoinTradeHistory).order_by(CryptoCoinTradeHistory.closed_at.desc()).limit(500)
    )
    trades_by_coin = {}
    for t in trades_result.scalars().all():
        trades_by_coin.setdefault(t.product_id, []).append(t.to_dict())
    for coin in coins:
        coin["trades"] = trades_by_coin.get(coin["product_id"], [])

    return {"coins": coins, "coin_count": len(coins)}


@router.post("/family-tree-status/root-take-profit", dependencies=[Depends(require_admin_key)])
async def take_root_profit():
    """Manually cash in BTC's (the tree's permanent root) profit right
    now, on demand - per the account owner's explicit request, since BTC
    is otherwise locked down from ANY manual sell (see
    close_family_tree_branch's root refusal below). This is NOT a
    carve-out of that protection: it reuses the exact same
    _branch_sell_and_settle() every automatic TARGET/STOP exit already
    uses, and root's own existing behavior in that function means it can
    never actually leave BTC-USD - it sells 100% at market, skims the
    same 10%-of-profit into locked_usd every other exit uses, and
    immediately rebuys BTC-USD with the rest at the new price. BTC never
    stops being the tree's root/parent (still able to spawn a child via
    _maybe_spawn_child, exactly as before) - this only lets that same
    real cycle be triggered on demand instead of waiting for the
    computed ATR target to be hit.

    Refused (400) if BTC has no open position, or isn't genuinely in
    profit right now against the real live price - same "never lock in
    a real loss" rule close_family_tree_branch already enforces for
    every other branch's manual sell.
    """
    if crypto_family_tree_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_family_tree_bot module not available")

    root_bot_name = crypto_family_tree_bot_module.ROOT_BOT_NAME
    branch = await crypto_family_tree_bot_module.load_branch(root_bot_name)
    if branch is None:
        raise HTTPException(status_code=404, detail="Root branch not found")

    position = await crypto_family_tree_bot_module._load_branch_position(root_bot_name)
    if position is None:
        raise HTTPException(status_code=400, detail="BTC has no open position to take profit on right now")

    engine = crypto_family_tree_bot_module.engine
    async with engine.aiohttp.ClientSession() as session:
        current_price, _atr_pct = await engine.get_price_and_volatility(session, branch.product_id)
        if current_price is None:
            raise HTTPException(status_code=503, detail="Could not fetch a live BTC price to confirm this would be a real profit - try again")
        if current_price <= position.entry_price:
            raise HTTPException(
                status_code=400,
                detail=f"BTC is not currently in profit (entry ${position.entry_price:,.2f}, now ${current_price:,.2f}) - refused to avoid locking in a loss",
            )
        await crypto_family_tree_bot_module._branch_sell_and_settle(
            session, root_bot_name, branch.product_id, position, "MANUAL PROFIT TAKE (dashboard)"
        )

    # Same reasoning as close_family_tree_branch below: the rebuy was
    # already decided inside _branch_sell_and_settle above (root always
    # stays on BTC-USD), so re-run the cycle immediately to place it now
    # instead of leaving BTC flat until its own thread wakes up next.
    await crypto_family_tree_bot_module.run_branch_cycle(root_bot_name)

    updated = await crypto_family_tree_bot_module.load_branch(root_bot_name)
    return {
        "status": "profit_taken",
        "bot_name": root_bot_name,
        "allocated_usd": round(updated.allocated_usd, 2) if updated else None,
        "product_id": updated.product_id if updated else None,
    }


@router.post("/family-tree-status/close/{bot_name}", dependencies=[Depends(require_admin_key)])
async def close_family_tree_branch(bot_name: str):
    """Manually force one branch to sell its open position right now, at
    market - a real Coinbase order via the exact same
    _branch_sell_and_settle() every automatic TARGET/STOP/floor-breach exit
    already uses, so a manual sell behaves identically: real P&L, the same
    10%-of-profit skim into locked_usd on a win, the same floor-reset-on-
    loss logic, and the same "pick a new coin and rebuy" handoff - nothing
    about this path is dashboard-only or simulated.

    Each branch also runs its own always-on background thread
    (_branch_thread_main) that can independently decide to sell the same
    position at any moment. This endpoint doesn't lock against that thread -
    it doesn't need to: place_market_sell() re-checks the REAL Coinbase
    balance immediately before selling and clamps to whatever's actually
    still held, so if the branch's own thread already sold first, this call
    finds nothing left to sell and safely no-ops instead of double-selling.
    """
    if crypto_family_tree_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_family_tree_bot module not available")

    # Per the account owner's explicit request: BTC (the tree's real,
    # permanent root - never any adopted legacy position, see the ROOT
    # badge fix in family_tree_dashboard.html) can never be manually sold,
    # matching its existing "root stays on BTC-USD by design" behavior on
    # automatic exits. Enforced here, not just hidden in the UI, so it
    # can't be bypassed by calling this endpoint directly.
    if bot_name == crypto_family_tree_bot_module.ROOT_BOT_NAME:
        raise HTTPException(
            status_code=400,
            detail=f"{bot_name} is the tree's permanent root - it can never be manually sold",
        )

    branch = await crypto_family_tree_bot_module.load_branch(bot_name)
    if branch is None:
        raise HTTPException(status_code=404, detail=f"No branch named {bot_name}")

    position = await crypto_family_tree_bot_module._load_branch_position(bot_name)
    if position is None:
        raise HTTPException(status_code=400, detail=f"{bot_name} has no open position to sell")

    engine = crypto_family_tree_bot_module.engine
    async with engine.aiohttp.ClientSession() as session:
        # Per the account owner: a manual sell must never be allowed to lock
        # in a real loss - only offered/accepted while genuinely in profit
        # right now, marked to the real live price. Re-checked here against
        # the real market, not just trusting whatever the dashboard button
        # last showed (that could be stale by the time this request lands).
        current_price, _atr_pct = await engine.get_price_and_volatility(session, branch.product_id)
        if current_price is None:
            raise HTTPException(status_code=503, detail="Could not fetch a live price to confirm this sell would be a real profit - try again")
        if current_price <= position.entry_price:
            raise HTTPException(
                status_code=400,
                detail=f"{bot_name} is not currently in profit (entry ${position.entry_price:,.2f}, now ${current_price:,.2f}) - manual sell refused to avoid locking in a loss",
            )
        await crypto_family_tree_bot_module._branch_sell_and_settle(
            session, bot_name, branch.product_id, position, "MANUAL SELL (dashboard)"
        )

    # Same reasoning as the automatic exit paths in run_branch_cycle: the
    # branch's next coin was already picked inside _branch_sell_and_settle
    # above, so re-run its cycle immediately to place the rebuy now instead
    # of leaving it idle until the branch's own thread wakes up next.
    await crypto_family_tree_bot_module.run_branch_cycle(bot_name)

    updated = await crypto_family_tree_bot_module.load_branch(bot_name)
    return {
        "status": "sold",
        "bot_name": bot_name,
        "allocated_usd": round(updated.allocated_usd, 2) if updated else None,
        "product_id": updated.product_id if updated else None,
    }


@router.post("/family-tree-status/spawn-branch", dependencies=[Depends(require_admin_key)])
async def spawn_family_tree_branch(db: AsyncSession = Depends(get_db)):
    """Manually starts a brand-new $50 branch right now, on demand -
    per the account owner, the same "$50 in, let it grow, swap coins,
    repeat" cycle every branch already runs, just kicked off immediately
    instead of waiting for an existing branch to organically earn its way
    to the next spawn tier.

    Funded from real currently-UNALLOCATED cash only (real Coinbase
    balance minus locked_usd minus every existing FLAT branch's own
    tracked allocated_usd) - never carved out of an existing branch's
    balance the way an organic parent-triggered spawn is. Refuses outright
    if there isn't at least SEED_USD of real free cash sitting around,
    rather than silently shorting an existing branch to make up the
    difference.

    Only branches with NO open position are subtracted here. get_usd_balance()
    returns real, LIQUID cash only - it does not include the value of any
    branch's currently-open crypto position. A branch holding an open
    position has already deployed its allocated_usd into crypto, so it
    isn't sitting in that cash figure and competing for it; subtracting it
    again would be comparing cash-only balance against money that isn't
    cash anymore (this was a real bug: with most branches holding open
    positions, this used to compute a wildly wrong negative "unallocated"
    figure and block spawns that were actually affordable).

    The new branch is inserted as a root-level child (same as any organic
    spawn from BTC) and immediately eligible to contest the throne against
    its siblings - see _check_and_lock_strongest_siblings(). No thread
    needs to be started here: the coordinator's own scan loop picks up any
    branch row without a running thread within COORDINATOR_SCAN_SECONDS."""
    if crypto_family_tree_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_family_tree_bot module not available")

    tree = crypto_family_tree_bot_module
    engine = tree.engine

    branches_result = await db.execute(select(CryptoTreeBranch))
    branches = list(branches_result.scalars().all())

    positions_result = await db.execute(
        select(BotPosition.bot).where(BotPosition.bot.in_([b.bot_name for b in branches]))
    ) if branches else None
    bots_with_open_position = set(positions_result.scalars().all()) if positions_result is not None else set()
    flat_allocated_sum = sum(b.allocated_usd for b in branches if b.bot_name not in bots_with_open_position)

    async with engine.aiohttp.ClientSession() as session:
        real_balance, err = await engine.get_usd_balance(session)
    if real_balance is None:
        raise HTTPException(status_code=503, detail=f"Could not fetch the real Coinbase balance to confirm funds ({err}) - try again")

    locked_usd = await tree.get_locked_usd()
    unallocated = real_balance - locked_usd - flat_allocated_sum
    if unallocated < tree.SEED_USD:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Not enough real unallocated cash to seed a new ${tree.SEED_USD:.0f} branch - only "
                f"${unallocated:.2f} is currently free (real balance ${real_balance:.2f} - locked "
                f"${locked_usd:.2f} - already allocated across flat branches ${flat_allocated_sum:.2f})"
            ),
        )

    next_product = await tree.get_next_eligible_product_id()
    if next_product is None:
        raise HTTPException(status_code=400, detail="No eligible coin to start a new branch on right now (every coin is excluded or cooling down)")

    try:
        child_name = await tree.spawn_child_branch_with_retry(next_product, tree.ROOT_BOT_NAME)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))

    log.info(f"[dashboard] 🌱 Manually spawned {child_name} ({next_product}) with ${tree.SEED_USD:.2f} seed, funded from real unallocated cash")
    return {
        "status": "spawned",
        "bot_name": child_name,
        "product_id": next_product,
        "seed_usd": round(tree.SEED_USD, 2),
        "remaining_unallocated": round(unallocated - tree.SEED_USD, 2),
    }


@router.post("/family-tree-status/spawn-branch/{product_id}", dependencies=[Depends(require_admin_key)])
async def spawn_family_tree_branch_on_coin(product_id: str, db: AsyncSession = Depends(get_db)):
    """Same real $50-seed spawn as spawn_family_tree_branch() above, except
    the caller picks the coin directly instead of the bot auto-selecting via
    get_next_eligible_product_id(). Backs the "Trade this coin" button on
    crypto_selection_backtest.html, per the account owner's explicit request
    to act on a coin that ranks well in the backtest (e.g. DOGE-USD/XRP-USD)
    without waiting for the bot's own coin search to reach it on its own.

    Funded from the same real-unallocated-cash pool as the auto-pick spawn
    endpoint, and subject to the same exclusion list as every other
    coin-selection path - a coin on get_effective_excluded_coins() can't be
    manually spawned into either, for the same real reason the bot itself
    won't auto-pick it.

    Per the account owner's explicit follow-up choice, no longer refuses a
    coin just because an existing branch already trades it - multiple
    branches can now hold the same coin at once (e.g. tapping "Trade this"
    on a coin that's already proving itself real, live, elsewhere in the
    tree). tree.spawn_child_branch_with_retry() gives the new branch a real,
    distinct identity even when its coin is already in use, and retries a
    few times server-side if that name collides with a concurrent spawn
    (the coordinator's own per-cycle catch-up check, or a second click)
    instead of making the account owner retry by hand."""
    if crypto_family_tree_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_family_tree_bot module not available")

    tree = crypto_family_tree_bot_module
    engine = tree.engine
    product_id = product_id.upper()

    if product_id not in tree.COIN_FAMILY_TREE:
        raise HTTPException(status_code=400, detail=f"{product_id} is not a coin the family tree bot trades")

    excluded = await tree.get_effective_excluded_coins()
    if product_id in excluded:
        raise HTTPException(
            status_code=400,
            detail=f"{product_id} is currently excluded (real backtest results) - can't manually start a branch on it",
        )

    branches_result = await db.execute(select(CryptoTreeBranch))
    branches = list(branches_result.scalars().all())

    positions_result = await db.execute(
        select(BotPosition.bot).where(BotPosition.bot.in_([b.bot_name for b in branches]))
    ) if branches else None
    bots_with_open_position = set(positions_result.scalars().all()) if positions_result is not None else set()
    flat_allocated_sum = sum(b.allocated_usd for b in branches if b.bot_name not in bots_with_open_position)

    async with engine.aiohttp.ClientSession() as session:
        real_balance, err = await engine.get_usd_balance(session)
    if real_balance is None:
        raise HTTPException(status_code=503, detail=f"Could not fetch the real Coinbase balance to confirm funds ({err}) - try again")

    locked_usd = await tree.get_locked_usd()
    unallocated = real_balance - locked_usd - flat_allocated_sum
    if unallocated < tree.SEED_USD:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Not enough real unallocated cash to seed a new ${tree.SEED_USD:.0f} branch - only "
                f"${unallocated:.2f} is currently free (real balance ${real_balance:.2f} - locked "
                f"${locked_usd:.2f} - already allocated across flat branches ${flat_allocated_sum:.2f})"
            ),
        )

    try:
        child_name = await tree.spawn_child_branch_with_retry(product_id, tree.ROOT_BOT_NAME)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))

    log.info(f"[dashboard] 🌱 Manually spawned {child_name} ({product_id}) with ${tree.SEED_USD:.2f} seed from the backtest page, funded from real unallocated cash")
    return {
        "status": "spawned",
        "bot_name": child_name,
        "product_id": product_id,
        "seed_usd": round(tree.SEED_USD, 2),
        "remaining_unallocated": round(unallocated - tree.SEED_USD, 2),
    }


class UnlockProfitRequest(BaseModel):
    amount: float
    bot_name: str | None = None  # omit to release as free spendable cash; set to add directly into that branch's balance


@router.post("/family-tree-status/unlock-profit", dependencies=[Depends(require_admin_key)])
async def unlock_locked_profit(payload: UnlockProfitRequest, db: AsyncSession = Depends(get_db)):
    """Manually releases real money back OUT of the crypto family tree's
    locked-profit ledger (see PROFIT_SKIM_PCT / the dust sweep in
    crypto_family_tree_bot.py). Per the account owner's explicit choice:
    a deliberate reversal of the "permanently out of the compounding
    loop" design everywhere else in this system - only ever happens via
    this explicit manual action, never automatically.

    Two modes, both real:
    - bot_name omitted: released as free spendable cash - locked_usd
      drops, so it's immediately available again to whichever branch's
      own cycle next wants to buy (or the account owner can withdraw it
      directly from Coinbase themselves, same as any other real cash).
    - bot_name given: added directly into that ONE branch's
      allocated_usd - a pure bookkeeping transfer (no Coinbase order
      needed, real dollars never left the account) exactly like a spawn's
      parent-deduct/child-add. No restriction on which branch - winning
      or losing, any existing branch, per the account owner's explicit
      choice ("all can be an option")."""
    if crypto_family_tree_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_family_tree_bot module not available")
    tree = crypto_family_tree_bot_module

    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")

    branch = None
    if payload.bot_name:
        result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == payload.bot_name))
        branch = result.scalar_one_or_none()
        if branch is None:
            raise HTTPException(status_code=404, detail=f"No branch named {payload.bot_name}")

    current_locked = await tree.get_locked_usd()
    if payload.amount > current_locked + 0.005:
        raise HTTPException(status_code=400, detail=f"Only ${current_locked:.2f} is currently locked - can't unlock ${payload.amount:.2f}")

    released = await tree._subtract_locked_usd(payload.amount)

    if branch is not None:
        branch.allocated_usd += released
        await db.commit()
        log.info(f"[dashboard] 🔓 Unlocked ${released:.2f} of locked profit and added it to {branch.bot_name}'s balance (now ${branch.allocated_usd:.2f})")
        return {
            "status": "added_to_branch", "amount": round(released, 2),
            "bot_name": branch.bot_name, "branch_new_balance": round(branch.allocated_usd, 2),
            "new_locked_usd": round(current_locked - released, 2),
        }

    log.info(f"[dashboard] 🔓 Unlocked ${released:.2f} of locked profit back into free spendable cash")
    return {"status": "cashed_out", "amount": round(released, 2), "new_locked_usd": round(current_locked - released, 2)}


@router.get("/family-tree-status/coin-watchlist", dependencies=[Depends(require_admin_key)])
async def family_tree_coin_watchlist():
    """Real-time (NOT backtested) view of every family-tree coin's live
    bullish/overbought/BTC-relative-strength status, per the account
    owner's explicit request after looking at the 30-day backtest table
    and asking "what's bullish right now" - the backtest replays history,
    this reads the exact same real, live checks the bot itself uses right
    now to pick a coin (crypto_family_tree_bot.get_live_coin_snapshot()),
    just reporting every coin instead of only the single best pick.
    Read-only, never places an order."""
    if crypto_family_tree_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_family_tree_bot module not available")
    return await crypto_family_tree_bot_module.get_live_coin_snapshot()


@router.post("/crypto-selection-backtest", dependencies=[Depends(require_admin_key)])
async def run_crypto_selection_backtest():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders,
    and no bot reads this result. Answers a real question raised about
    the family-tree bot's coin selection: find_most_volatile_unclaimed_coin()
    only checks whether a coin is up over a ~25-hour window before buying
    it, with no sense of whether that move already happened and the coin
    is now extended - a real gap, not a guess. This replays the bot's own
    real target/stop/breakeven/giveback rules (crypto_selection_backtest.py,
    importing the actual live functions rather than reimplementing them)
    against every family-tree coin's real historical Coinbase candles, and
    ranks them by what that strategy would actually have returned on each
    one - so coin selection can eventually be informed by real backtested
    results instead of only the 25-hour up/down check.

    Pulls real historical data from Coinbase's public candles endpoint
    concurrently across ~27 coins - can take 30-90 seconds depending on
    that endpoint's response time."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_full_backtest()


@router.post("/crypto-selection-backtest/btc-relative-strength", dependencies=[Depends(require_admin_key)])
async def run_btc_relative_strength_backtest():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders,
    and no bot reads this result yet. Per the account owner's explicit
    request: answers whether requiring a coin to be genuinely
    outperforming BTC-USD over the same real ~25-hour window - not just
    "up" in isolation - would have improved each coin's real backtested
    numbers. Runs the exact same real target/stop/breakeven/giveback
    replay as /crypto-selection-backtest, twice per coin, on the exact
    same real historical data: once with no entry filter (baseline,
    identical to the main backtest) and once gated by real BTC-relative
    strength, so the two are directly comparable. Does not change what
    the live bot buys unless/until wired into the live selection path
    separately, on purpose - this is a read-only comparison report.

    Pulls real historical data from Coinbase's public candles endpoint,
    plus one extra fetch for BTC-USD's own history to compare against -
    can take 30-90 seconds depending on that endpoint's response time."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_btc_relative_strength_comparison()


@router.post("/crypto-selection-backtest/higher-tf-trend", dependencies=[Depends(require_admin_key)])
async def run_higher_tf_trend_backtest():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders,
    and no bot reads this result yet. Answers a real question the account
    owner raised directly: the Alpaca side has a real 1-hour SMA20/SMA50
    trend-confirmation filter on new entries (get_higher_tf_trend() in
    prop_bot.py) that the crypto side has never had - would the same idea
    have helped here? Runs the exact same real target/stop/breakeven/
    giveback replay as /crypto-selection-backtest, twice per coin, on the
    exact same real historical hourly candles: once with no entry filter
    (baseline, identical to the main backtest) and once gated by the
    coin's own real SMA20 > SMA50 uptrend, so the two are directly
    comparable. Does not change what the live bot buys unless/until wired
    into the live selection path separately, on purpose - this is a
    read-only comparison report.

    Pulls real historical data from Coinbase's public candles endpoint -
    can take 30-90 seconds depending on that endpoint's response time."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_higher_tf_trend_comparison()


@router.post("/alpaca-selection-backtest", dependencies=[Depends(require_admin_key)])
async def run_alpaca_selection_backtest():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders.
    The Alpaca-side counterpart to /crypto-selection-backtest above, per
    the account owner's explicit request. Replays alpaca_mean_reversion.py's
    own real target/stop/breakeven/giveback rules (importing the actual
    live function, not reimplementing it) against real historical Alpaca
    bars for every symbol prop_bot.py/alpaca_swing_bot.py actually trade
    (SPY, QQQ, DIA, IWM, GLD, USO, SLV, plus the 1x inverse ETFs
    SH/PSQ/DOG/RWM), long-only - shorting is disabled on the real
    account, so a short-side backtest would be purely hypothetical.

    Pulls real historical data from Alpaca's market-data API concurrently
    across 11 symbols - can take up to ~60 seconds."""
    if alpaca_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="alpaca_selection_backtest module not available")
    return await alpaca_selection_backtest_module.run_full_backtest()


@router.get("/alpaca-overview", dependencies=[Depends(require_admin_key)])
async def get_alpaca_overview(db: AsyncSession = Depends(get_db)):
    """Real Alpaca account snapshot for a focused, at-a-glance dashboard:
    equity, each bot_N bucket's capital/profit, every real open position,
    and the same $1M-goal auto-scale progress prop_bot.py itself logs
    every cycle (AUTO-SCALE: Equity $X -> Scale Yx | Progress to $1M: Z%).
    Backs alpaca_dashboard.html - distinct from the older, denser /status
    endpoint above, which this reuses the same bot-bucket helpers as but
    doesn't overlap with (no withdrawal-request bookkeeping here)."""
    if not (ALPACA_KEY and ALPACA_SECRET):
        raise HTTPException(status_code=500, detail="Alpaca credentials not configured")

    async with aiohttp.ClientSession() as session:
        account = await _fetch_alpaca_account(session)
        positions = await _fetch_alpaca_positions(session)
        opened_at_by_symbol = {}
        for p in positions:
            sym = p.get("symbol")
            if sym:
                opened_at_by_symbol[sym] = await _fetch_position_opened_at(session, sym)

    try:
        equity = float(account.get("equity", 0))
        cash = float(account.get("cash", 0))
        last_equity = float(account.get("last_equity", equity))
    except (ValueError, TypeError) as e:
        log.error(f"Failed to parse account fields: {e}")
        raise HTTPException(status_code=502, detail="Invalid account data from Alpaca")

    session_pl = equity - last_equity
    session_pl_pct = (session_pl / last_equity * 100) if last_equity > 0 else 0.0

    # Mirrors prop_bot.py's own get_auto_scale formula exactly (1.0x at
    # $1K, scaling +0.01x per $1K earned, capped at 5.0x) - not importable
    # directly since it's a local closure inside prop_bot's run loop, not
    # a module-level function.
    scale = round(min(1.0 + (equity / 100000.0), 5.0), 2)
    goal = 1_000_000.0
    progress_to_goal_pct = round(min(100.0, (equity / goal) * 100), 4)

    # prop_bot's real in-memory ratchet, same read-only-module-state
    # pattern /crypto-coinbase-status above already uses.
    equity_floor = round(getattr(prop_bot_module, "equity_floor", 0.0), 2) if prop_bot_module else 0.0

    bots = await _get_or_init_bots(db, equity)
    rebalanced = _rebalance_bots(bots, equity)
    if rebalanced != 0.0:
        await db.commit()
        for bot in bots:
            await db.refresh(bot)

    locked_usd = round(await get_alpaca_locked_usd(), 2)

    return {
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "session_pl": round(session_pl, 2),
        "session_pl_pct": round(session_pl_pct, 2),
        "equity_floor": equity_floor,
        "scale": scale,
        "goal": goal,
        "progress_to_goal_pct": progress_to_goal_pct,
        "locked_usd": locked_usd,
        "auto_close_profit_pct": ALPACA_AUTO_CLOSE_PROFIT_PCT,
        "auto_close_max_hold_days": ALPACA_AUTO_CLOSE_MAX_HOLD_DAYS,
        "bots": [{"name": b.bot_name, "capital": round(b.base_capital, 2), "profit": round(_bot_profit(b), 2), "pl": round(_bot_pl(b), 2)} for b in bots],
        "positions": [
            {
                "symbol": p.get("symbol"),
                "side": p.get("side"),
                "qty": p.get("qty"),
                "avg_entry_price": p.get("avg_entry_price"),
                "current_price": p.get("current_price"),
                "market_value": p.get("market_value"),
                "unrealized_pl": p.get("unrealized_pl"),
                "unrealized_plpc": p.get("unrealized_plpc"),
                "opened_at": opened_at_by_symbol.get(p.get("symbol")),
            }
            for p in positions
        ],
    }


@router.post("/alpaca-overview/close/{symbol}", dependencies=[Depends(require_admin_key)])
async def close_alpaca_position(symbol: str, db: AsyncSession = Depends(get_db)):
    """Manually close one real open Alpaca position at market price - the
    same DELETE /v2/positions/{symbol} Alpaca's own app uses, so this is a
    real order, not a dashboard-only toggle. No bot enforces a scheduled
    close date on these positions today (exits are signal-based - RSI,
    profit target, stop loss - not calendar-based), so this is the only
    way to close one on demand before its own exit signal fires. Records
    the realized P&L as a real Payment row using the same worker/split the
    bots' own automatic exits use, so a manual close still shows up in
    earnings tracking like any other close."""
    symbol = symbol.upper()
    if not (ALPACA_KEY and ALPACA_SECRET):
        raise HTTPException(status_code=500, detail="Alpaca credentials not configured")

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{ALPACA_BASE_URL}/v2/positions/{symbol}", headers=ALPACA_HEADERS) as r:
            if r.status != 200:
                raise HTTPException(status_code=404, detail=f"No open position for {symbol}")
            position = await r.json()

        # cancel_orders=true: without it, an existing open order on this
        # symbol (e.g. a stop-loss/take-profit leg another bot placed)
        # holds the shares Alpaca considers "available", and the close
        # order this endpoint places gets rejected with a 403 (error body
        # shape {"available":..,"existing_qty":..,"held_for_orders":..}) -
        # real production symptom this fixes, not a hypothetical.
        async with session.delete(f"{ALPACA_BASE_URL}/v2/positions/{symbol}?cancel_orders=true", headers=ALPACA_HEADERS) as r:
            if r.status not in (200, 207):
                body = await r.text()
                raise HTTPException(status_code=502, detail=f"Alpaca close failed ({r.status}): {body}")
            close_result = await r.json()

    try:
        qty = float(position.get("qty", 0))
        entry_price = float(position.get("avg_entry_price", 0))
        current_price = float(position.get("current_price", entry_price))
        pnl = (current_price - entry_price) * qty
    except (ValueError, TypeError):
        qty, pnl = 0.0, 0.0

    try:
        payment = Payment(
            id=f"manual_close_{uuid.uuid4().hex[:8]}",
            job_id=f"manual_close_{symbol}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
            worker_id="bot@pgusa.local",
            client_id="alpaca_manual_close",
            gross_amount=pnl,
            worker_amount=pnl * 0.90,
            platform_amount=pnl * 0.10,
            payout_status="pending" if pnl > 0 else "completed",
        )
        db.add(payment)
        await db.commit()
    except Exception as e:
        log.warning(f"Failed to record manual-close earnings for {symbol}: {e}")

    log.info(f"Manually closed {symbol} via dashboard: qty={qty}, realized_pnl=${pnl:.2f}")
    return {"status": "closed", "symbol": symbol, "qty": qty, "realized_pnl": round(pnl, 2), "order": close_result}


class AlpacaUnlockProfitRequest(BaseModel):
    amount: float


@router.post("/alpaca-overview/unlock-profit", dependencies=[Depends(require_admin_key)])
async def unlock_alpaca_locked_profit(payload: AlpacaUnlockProfitRequest):
    """Cash-out ONLY, per the account owner's explicit choice - no
    "add to a bucket" mode (see _subtract_alpaca_locked_usd's docstring
    for why that wouldn't do anything meaningful here, unlike the crypto
    side). Just releases real tracked profit back out of the locked
    ledger."""
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")

    current_locked = await get_alpaca_locked_usd()
    if payload.amount > current_locked + 0.005:
        raise HTTPException(status_code=400, detail=f"Only ${current_locked:.2f} is currently locked - can't unlock ${payload.amount:.2f}")

    released = await _subtract_alpaca_locked_usd(payload.amount)
    log.info(f"[dashboard] 🔓 Unlocked ${released:.2f} of Alpaca locked profit")
    return {"status": "cashed_out", "amount": round(released, 2), "new_locked_usd": round(current_locked - released, 2)}


@router.post("/alpaca-overview/trade-this/{ticker}", dependencies=[Depends(require_admin_key)])
async def manual_open_prop_position(ticker: str):
    """Manually opens a real long position on prop_bot.py's real funded-
    account evaluation - the "Trade this" action on the stock/ETF
    backtest page, per the account owner's explicit request to match the
    crypto side's. This is NOT a shortcut around the account's real
    risk rules: it reuses the EXACT same real functions the automatic
    entry path calls (get_price_rsi, validate_entry/APEX_MANDATE's
    universe check, check_kill_conditions, check_margin_safety,
    size_position, execute_futures_trade) rather than reimplementing any
    of them, so a manual entry gets the same real protection an
    automatic one does - it's just triggered on demand instead of by a
    live RSI signal. Long-only, matching everything else prop_bot.py can
    actually execute today (shorting is disabled on the real account -
    see get_account_shorting_enabled)."""
    if prop_bot_module is None:
        raise HTTPException(status_code=500, detail="prop_bot module not available")
    pb = prop_bot_module
    ticker = ticker.upper()

    if os.getenv("STOP_TRADING", "false").lower() == "true":
        raise HTTPException(status_code=400, detail="STOP_TRADING is set - all entries (manual or automatic) are paused")

    symbol_to_contract = {cfg["symbol"]: code for code, cfg in pb.FUTURES.items()}
    contract = symbol_to_contract.get(ticker)
    if contract is None:
        raise HTTPException(status_code=400, detail=f"{ticker} is not a symbol prop_bot trades")

    if contract in pb.open_prop_positions:
        raise HTTPException(status_code=409, detail=f"Already holding a position in {contract} ({ticker})")

    approved_universe = (
        pb.APEX_MANDATE["universe"]["futures"] +
        pb.APEX_MANDATE["universe"]["crypto"] +
        pb.APEX_MANDATE["universe"]["commodities"] +
        pb.APEX_MANDATE["universe"]["inverse_etfs"]
    )
    if contract not in approved_universe:
        raise HTTPException(status_code=400, detail=f"{contract} ({ticker}) is not in the approved trading universe")

    excluded_symbols = await pb.get_effective_excluded_symbols()
    if ticker in excluded_symbols:
        raise HTTPException(
            status_code=400,
            detail=f"{ticker} is currently auto-excluded - its last {pb.AUTO_EXCLUDE_RUN_WINDOW} real backtest runs were all negative ROI",
        )

    async with aiohttp.ClientSession() as session:
        equity = await pb.get_account_equity(session)
        if equity is None:
            raise HTTPException(status_code=503, detail="Could not fetch real account equity - try again")
        buying_power = await pb.get_account_buying_power(session)

        should_halt, halt_reason = pb.check_kill_conditions(
            buying_power=buying_power, equity=equity, daily_loss=pb.daily_pnl,
            open_position_count=len(pb.open_prop_positions),
        )
        if should_halt:
            raise HTTPException(status_code=400, detail=f"Trading halted by kill condition: {halt_reason}")

        price_data = await pb.get_price_rsi(session, ticker)
        if price_data is None:
            reason = pb._price_rsi_last_failure.get(ticker, "unknown reason")
            raise HTTPException(status_code=503, detail=f"Could not fetch a live price/RSI for {ticker}: {reason} - try again")
        price, rsi, trend = price_data["price"], price_data["rsi"], price_data["trend"]

        total_notional = sum(p.get("qty", 0) * p.get("entry", 0) for p in pb.open_prop_positions.values())
        is_valid, mandate_reason = pb.validate_entry(
            bot_name="prop_bot", symbol=contract, rsi=rsi, volume_ratio=1.0,
            buying_power=buying_power, open_positions=len(pb.open_prop_positions),
            total_notional=total_notional, equity=equity,
        )
        if not is_valid:
            raise HTTPException(status_code=400, detail=f"Mandate check failed: {mandate_reason}")

        is_safe, safety_reason = pb.check_margin_safety(buying_power, equity, len(pb.open_prop_positions))
        if not is_safe:
            raise HTTPException(status_code=400, detail=f"Margin safety check failed: {safety_reason}")

        scale = pb._safe_float_env("POSITION_SCALE_MULTIPLIER", "1.0")
        max_positions = pb.get_dynamic_max_positions(scale)
        slots_remaining = max(1, max_positions - len(pb.open_prop_positions))
        qty = pb.size_position(buying_power, slots_remaining, price, account_equity=equity)
        if qty is None:
            raise HTTPException(status_code=400, detail="Position size would be below the minimum notional - not enough real buying power")

        filled = await pb.execute_futures_trade(
            session, contract, "BUY", qty, price, rsi, trend,
            stop_loss=price * 0.98, target=price * 1.03,
        )
        if not filled:
            raise HTTPException(status_code=502, detail="Alpaca order failed - see server logs")

        pb.open_prop_positions[contract] = {"side": "long", "entry": price, "qty": qty, "open_time": datetime.now(pb.ET)}
        await pb._db_save_open(contract, "long", price, qty)

    log.info(f"[dashboard] 🌱 Manually opened LONG {qty} {contract} ({ticker}) @ ${price:.2f}")
    return {
        "status": "opened", "contract": contract, "symbol": ticker,
        "qty": qty, "entry_price": round(price, 4), "rsi": rsi,
    }


@router.get("/alpaca-overview/entry-eligibility", dependencies=[Depends(require_admin_key)])
async def alpaca_entry_eligibility():
    """Per the account owner's real request after "Trade this" refused USO
    with "RSI 58.9 not oversold" - rather than finding out only after
    clicking, this shows which symbols are ACTUALLY clickable right now.
    Deliberately reuses the exact same real checks manual_open_prop_position
    (this file) runs, in the same order, minus the final size_position/
    execute_futures_trade - a read-only dry run of the same real gate, not
    a second, looser copy of it that could drift out of sync or (worse)
    quietly become the real bypass the account owner explicitly said they
    did NOT want built. Every symbol still goes through get_price_rsi and
    validate_entry for real, live data - nothing here is cached or
    estimated.

    Kill-condition and margin-safety are account-wide, not per-symbol, so
    they're checked once: if either fails, every symbol is reported
    ineligible with that one shared reason, matching how "Trade this"
    itself would fail identically on every symbol in that state."""
    if prop_bot_module is None:
        raise HTTPException(status_code=500, detail="prop_bot module not available")
    pb = prop_bot_module

    if os.getenv("STOP_TRADING", "false").lower() == "true":
        return {"tickers": {c["symbol"]: {"eligible": False, "reason": "STOP_TRADING is set - all entries paused", "rsi": None} for c in pb.FUTURES.values()}}

    excluded_symbols = await pb.get_effective_excluded_symbols()
    approved_universe = (
        pb.APEX_MANDATE["universe"]["futures"] +
        pb.APEX_MANDATE["universe"]["crypto"] +
        pb.APEX_MANDATE["universe"]["commodities"] +
        pb.APEX_MANDATE["universe"]["inverse_etfs"]
    )

    async with aiohttp.ClientSession() as session:
        equity = await pb.get_account_equity(session)
        buying_power = await pb.get_account_buying_power(session) if equity is not None else None

        shared_block_reason = None
        if equity is None:
            shared_block_reason = "Could not fetch real account equity right now"
        else:
            should_halt, halt_reason = pb.check_kill_conditions(
                buying_power=buying_power, equity=equity, daily_loss=pb.daily_pnl,
                open_position_count=len(pb.open_prop_positions),
            )
            if should_halt:
                shared_block_reason = f"Trading halted by kill condition: {halt_reason}"
            else:
                is_safe, safety_reason = pb.check_margin_safety(buying_power, equity, len(pb.open_prop_positions))
                if not is_safe:
                    shared_block_reason = f"Margin safety check failed: {safety_reason}"

        results = {}
        for contract, config in pb.FUTURES.items():
            ticker = config["symbol"]
            if shared_block_reason:
                results[ticker] = {"eligible": False, "reason": shared_block_reason, "rsi": None}
                continue
            if contract in pb.open_prop_positions:
                results[ticker] = {"eligible": False, "reason": f"Already holding a position in {contract}", "rsi": None}
                continue
            if contract not in approved_universe:
                results[ticker] = {"eligible": False, "reason": "Not in the approved trading universe", "rsi": None}
                continue
            if ticker in excluded_symbols:
                results[ticker] = {"eligible": False, "reason": f"Auto-excluded - last {pb.AUTO_EXCLUDE_RUN_WINDOW} real backtest runs were all negative ROI", "rsi": None}
                continue

            price_data = await pb.get_price_rsi(session, ticker)
            if price_data is None:
                reason = pb._price_rsi_last_failure.get(ticker, "unknown reason")
                results[ticker] = {"eligible": False, "reason": f"Could not fetch a live price/RSI: {reason}", "rsi": None}
                continue

            rsi = price_data["rsi"]
            total_notional = sum(p.get("qty", 0) * p.get("entry", 0) for p in pb.open_prop_positions.values())
            is_valid, mandate_reason = pb.validate_entry(
                bot_name="prop_bot", symbol=contract, rsi=rsi, volume_ratio=1.0,
                buying_power=buying_power, open_positions=len(pb.open_prop_positions),
                total_notional=total_notional, equity=equity,
            )
            results[ticker] = {"eligible": is_valid, "reason": None if is_valid else mandate_reason, "rsi": rsi}

    return {"tickers": results}


async def get_alpaca_locked_usd() -> float:
    """Running total of profit skimmed by check_and_auto_close_positions -
    same generic per-key bucket table (TradingBotState) the bot-bucket
    tracking and crypto_family_tree_bot.py's own locked ledger both use,
    just a different key so the two accounts' locked profit never mixes."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == ALPACA_LOCKED_PROFIT_KEY))
        row = result.scalar_one_or_none()
        return row.base_capital if row else 0.0


async def _subtract_alpaca_locked_usd(amount: float) -> float:
    """Reverse of the skim in check_and_auto_close_positions. Per the
    account owner's explicit choice: unlike the crypto side, this is
    cash-out ONLY - no "add to a specific bucket" mode. The 8 bot_N
    buckets aren't independent principal pools the way crypto branches
    are; they're proportional SHARES of one real Alpaca equity, and
    _rebalance_bots() re-derives every bucket's share from the real
    account balance on every load. Manually bumping one bucket's
    base_capital would just get smeared back across all 8 on the very
    next rebalance, so there's nothing meaningful an "add to a bucket"
    mode could do here. Clamps to whatever's actually there and returns
    the real amount released."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == ALPACA_LOCKED_PROFIT_KEY))
        row = result.scalar_one_or_none()
        current = row.base_capital if row else 0.0
        released = min(max(amount, 0.0), current)
        if row:
            row.base_capital = current - released
        await db.commit()
        return released


async def _is_market_open(session: aiohttp.ClientSession) -> bool:
    """Real check against Alpaca's own clock, not a guess - defaults to
    False (closed) on any failure, since the two possible mistakes here
    aren't symmetric: wrongly skipping a close just waits one more cycle,
    but wrongly attempting one performs a real, hard-to-undo cancel (see
    check_and_auto_close_positions' docstring) for nothing."""
    try:
        async with session.get(f"{ALPACA_BASE_URL}/v2/clock", headers=ALPACA_HEADERS) as r:
            if r.status != 200:
                return False
            data = await r.json()
            return bool(data.get("is_open", False))
    except Exception as e:
        log.warning(f"[AUTO-CLOSE] Market clock check failed, assuming closed: {e}")
        return False


async def check_and_auto_close_positions():
    """Real, unattended enforcement, per the account owner's explicit
    request: closes any open Alpaca position once it either hits
    ALPACA_AUTO_CLOSE_PROFIT_PCT unrealized gain, or has been open
    ALPACA_AUTO_CLOSE_MAX_HOLD_DAYS or longer, whichever comes first.
    Skims ALPACA_PROFIT_SKIM_PCT of realized gain into the locked ledger
    on every close (never on a loss) - the rest returns to the account's
    real buying power automatically on close; nothing here decides what
    to buy next, that's a separate, unbuilt decision.

    Runs from a single asyncio task (see run_auto_close_periodically,
    started once from main.py) rather than per-bot, since this acts on
    every real open position account-wide regardless of which bot (if
    any) is nominally trading that symbol.

    Only ever runs while the market is actually open (see
    _is_market_open). The close request uses cancel_orders=true, which
    cancels any existing protective order (stop-loss/take-profit) BEFORE
    placing the new closing order - real production symptom this
    discovered: if the market is closed, that cancel can succeed while
    the replacement closing order can't actually fill, leaving the
    position with no protection at all until the next session. Skipping
    the whole attempt while closed is the only way to guarantee that
    never happens; the position just waits, still protected by whatever
    order it already had, until the next check after market open."""
    if not (ALPACA_KEY and ALPACA_SECRET):
        return

    async with aiohttp.ClientSession() as session:
        if not await _is_market_open(session):
            return

        positions = await _fetch_alpaca_positions(session)
        if not positions:
            return

        for p in positions:
            symbol = p.get("symbol")
            if not symbol:
                continue
            try:
                qty = float(p.get("qty", 0))
                entry_price = float(p.get("avg_entry_price", 0))
                current_price = float(p.get("current_price", entry_price))
                unrealized_plpc = float(p.get("unrealized_plpc", 0) or 0)
            except (TypeError, ValueError):
                continue

            age_days = None
            opened_at_iso = await _fetch_position_opened_at(session, symbol)
            if opened_at_iso:
                try:
                    opened_dt = datetime.fromisoformat(opened_at_iso.replace("Z", "+00:00"))
                    age_days = (datetime.now(timezone.utc) - opened_dt).total_seconds() / 86400.0
                except ValueError:
                    age_days = None

            hit_profit_target = unrealized_plpc >= ALPACA_AUTO_CLOSE_PROFIT_PCT
            hit_max_hold = age_days is not None and age_days >= ALPACA_AUTO_CLOSE_MAX_HOLD_DAYS
            if not (hit_profit_target or hit_max_hold):
                continue
            reason = "profit target" if hit_profit_target else "max hold"

            # cancel_orders=true - see close_alpaca_position's comment above;
            # this is the exact real failure this feature hit on its first
            # live run (AMZD/YUM both 403'd with an "available"/"existing_qty"/
            # "held_for_orders" body, meaning another open order was holding
            # the shares).
            async with session.delete(f"{ALPACA_BASE_URL}/v2/positions/{symbol}?cancel_orders=true", headers=ALPACA_HEADERS) as r:
                if r.status not in (200, 207):
                    body = await r.text()
                    log.warning(f"[AUTO-CLOSE] {symbol} close failed ({reason}): HTTP {r.status} {body[:200]}")
                    continue

            pnl = (current_price - entry_price) * qty
            skim = round(pnl * ALPACA_PROFIT_SKIM_PCT, 2) if pnl > 0 else 0.0
            age_note = f" | aged {age_days:.1f}d" if age_days is not None else ""
            log.info(f"[AUTO-CLOSE] {symbol} closed ({reason}) | qty={qty} | unrealized {unrealized_plpc*100:+.1f}% | "
                     f"realized_pnl=${pnl:.2f}{age_note}")

            try:
                async with AsyncSessionLocal() as db:
                    payment = Payment(
                        id=f"auto_close_{uuid.uuid4().hex[:8]}",
                        job_id=f"auto_close_{symbol}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
                        worker_id="bot@pgusa.local",
                        client_id="alpaca_auto_close",
                        gross_amount=pnl,
                        worker_amount=(pnl - skim) if pnl > 0 else pnl,
                        platform_amount=skim,
                        payout_status="pending" if pnl > 0 else "completed",
                    )
                    db.add(payment)

                    if skim > 0:
                        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == ALPACA_LOCKED_PROFIT_KEY))
                        row = result.scalar_one_or_none()
                        if row:
                            row.base_capital += skim
                        else:
                            db.add(TradingBotState(bot_name=ALPACA_LOCKED_PROFIT_KEY, base_capital=skim, starting_capital=0.0))
                        log.info(f"[AUTO-CLOSE] 🔒 Locked ${skim:.2f} (10% of {symbol}'s ${pnl:.2f} profit)")

                    await db.commit()
            except Exception as e:
                log.warning(f"[AUTO-CLOSE] Failed to record earnings for {symbol}: {e}")


async def run_auto_close_periodically():
    log.info(f"Alpaca auto-close loop started: profit target {ALPACA_AUTO_CLOSE_PROFIT_PCT*100:.0f}%, "
             f"max hold {ALPACA_AUTO_CLOSE_MAX_HOLD_DAYS:.0f}d, checking every {ALPACA_AUTO_CLOSE_CHECK_INTERVAL_SECONDS}s")
    while True:
        try:
            await check_and_auto_close_positions()
        except Exception as e:
            log.warning(f"Alpaca auto-close cycle failed: {e}")
        await asyncio.sleep(ALPACA_AUTO_CLOSE_CHECK_INTERVAL_SECONDS)


# Chart-eligible symbols only - an explicit allowlist, checked before the
# symbol is ever interpolated into an outbound URL, so this endpoint can
# never be turned into an open SSRF proxy via an arbitrary path param.
# Same tickers prop_bot.py/crypto_coinbase_bot.py already trade.
CHART_STOCK_SYMBOLS = {"SPY", "QQQ", "DIA", "IWM", "GLD", "USO", "SLV", "SH", "PSQ", "DOG", "RWM"}
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


@router.get("/account/balance")
async def get_account_balance():
    """Get real Alpaca account balance - cash available, buying power, equity.
    Shows how much you can withdraw or use for trading."""
    if not (ALPACA_KEY and ALPACA_SECRET):
        raise HTTPException(status_code=500, detail="Alpaca credentials not configured")

    async with aiohttp.ClientSession() as session:
        try:
            account = await _fetch_alpaca_account(session)
        except Exception as e:
            log.error(f"Failed to fetch account balance: {e}")
            raise HTTPException(status_code=502, detail="Failed to fetch account balance")

    if not account:
        raise HTTPException(status_code=502, detail="No account data returned")

    return {
        "cash": round(float(account.get("cash", 0)), 2),
        "buying_power": round(float(account.get("buying_power", 0)), 2),
        "equity": round(float(account.get("equity", 0)), 2),
        "account_value": round(float(account.get("portfolio_value", 0)), 2),
        "status": account.get("status", "unknown"),
        "day_trading_buying_power": round(float(account.get("daytrading_buying_power", 0)), 2),
        "cash_withdrawable": round(float(account.get("cash", 0)), 2),
    }


@router.get("/coinbase/balances")
async def get_coinbase_balances():
    """Get real Coinbase account balances with unrealized P&L per position.
    Shows holdings, entry prices, current prices, and profit per coin."""
    try:
        import crypto_coinbase_bot
    except ImportError:
        raise HTTPException(status_code=503, detail="Crypto bot not available")

    try:
        async with aiohttp.ClientSession() as session:
            # Fetch current prices for all holdings
            holdings = {}
            for symbol in crypto_coinbase_bot.CRYPTO_PAIRS:
                pair = symbol.replace("/", "-")
                url = f"https://api.exchange.coinbase.com/products/{pair}/ticker"
                try:
                    async with session.get(url, headers={"Accept": "application/json"}) as r:
                        if r.status == 200:
                            data = await r.json()
                            current_price = float(data.get("price", 0))
                            holdings[symbol] = {"current_price": current_price}
                except Exception:
                    holdings[symbol] = {"current_price": 0}
                await asyncio.sleep(0.05)  # Rate limit

            # Get open positions from bot's in-memory dict
            positions = crypto_coinbase_bot.open_crypto_positions

            result = []
            total_value = 0
            total_unrealized_pnl = 0

            for symbol, pos in positions.items():
                entry_price = pos.get("entry_price", 0)
                qty = pos.get("qty", 0)
                current_price = holdings.get(symbol, {}).get("current_price", entry_price)

                entry_value = entry_price * qty
                current_value = current_price * qty
                unrealized_pnl = current_value - entry_value
                unrealized_pct = (unrealized_pnl / entry_value * 100) if entry_value > 0 else 0

                total_value += current_value
                total_unrealized_pnl += unrealized_pnl

                result.append({
                    "symbol": symbol,
                    "qty": round(qty, 8),
                    "entry_price": round(entry_price, 4),
                    "current_price": round(current_price, 4),
                    "entry_value": round(entry_value, 2),
                    "current_value": round(current_value, 2),
                    "unrealized_pnl": round(unrealized_pnl, 2),
                    "unrealized_pct": round(unrealized_pct, 2),
                    "targets": pos.get("targets", {}),
                })

            return {
                "positions": result,
                "total_value": round(total_value, 2),
                "total_unrealized_pnl": round(total_unrealized_pnl, 2),
                "position_count": len(result),
            }
    except Exception as e:
        log.error(f"Failed to fetch Coinbase balances: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to fetch balances: {str(e)}")


class CoinbaseSellRequest(BaseModel):
    symbol: str
    qty: float = None  # If None, sell full position


@router.post("/coinbase/sell")
async def manual_sell_coinbase(req: CoinbaseSellRequest):
    """Manually sell a Coinbase position at market price.
    Keep stop loss active, but user decides when to lock in profit."""
    try:
        import crypto_coinbase_bot
    except ImportError:
        raise HTTPException(status_code=503, detail="Crypto bot not available")

    symbol = req.symbol
    qty_to_sell = req.qty

    # Check if position exists
    if symbol not in crypto_coinbase_bot.open_crypto_positions:
        raise HTTPException(status_code=404, detail=f"No open position for {symbol}")

    position = crypto_coinbase_bot.open_crypto_positions[symbol]
    if qty_to_sell is None:
        qty_to_sell = position["qty"]

    if qty_to_sell > position["qty"]:
        raise HTTPException(status_code=400, detail=f"Cannot sell {qty_to_sell}, only {position['qty']} available")

    try:
        async with aiohttp.ClientSession() as session:
            # Get current price
            pair = symbol.replace("/", "-")
            url = f"https://api.exchange.coinbase.com/products/{pair}/ticker"
            async with session.get(url) as r:
                data = await r.json()
                current_price = float(data.get("price", 0))

            # Place market sell order (use market_ioc for immediate execution)
            product_id = symbol.replace("/", "-")
            path = "/api/v3/brokerage/orders"
            order_config = {
                "market_ioc": {
                    "base_size": f"{qty_to_sell:.8f}"
                }
            }
            order = {
                "client_order_id": str(uuid.uuid4()),
                "product_id": product_id,
                "side": "SELL",
                "order_configuration": order_config,
            }

            headers = crypto_coinbase_bot._auth_headers("POST", path)
            async with session.post(crypto_coinbase_bot.COINBASE_BASE_URL + path, headers=headers, json=order) as r:
                result = await r.json()
                if r.status not in (200, 201) or not result.get("success", True):
                    raise HTTPException(status_code=502, detail=f"Order failed: {result.get('error_response', result)}")

            # Update position in bot
            entry_price = position["entry_price"]
            realized_pnl = (current_price - entry_price) * qty_to_sell

            if qty_to_sell >= position["qty"]:
                # Full close
                crypto_coinbase_bot.open_crypto_positions.pop(symbol, None)
                return {
                    "status": "closed",
                    "symbol": symbol,
                    "qty_sold": round(qty_to_sell, 8),
                    "sell_price": round(current_price, 4),
                    "entry_price": round(entry_price, 4),
                    "realized_pnl": round(realized_pnl, 2),
                    "realized_pct": round((realized_pnl / (entry_price * qty_to_sell) * 100), 2) if entry_price > 0 else 0,
                }
            else:
                # Partial close
                position["qty"] -= qty_to_sell
                return {
                    "status": "partial",
                    "symbol": symbol,
                    "qty_sold": round(qty_to_sell, 8),
                    "qty_remaining": round(position["qty"], 8),
                    "sell_price": round(current_price, 4),
                    "entry_price": round(entry_price, 4),
                    "realized_pnl": round(realized_pnl, 2),
                    "realized_pct": round((realized_pnl / (entry_price * qty_to_sell) * 100), 2) if entry_price > 0 else 0,
                }
    except Exception as e:
        log.error(f"Manual sell failed for {symbol}: {e}")
        raise HTTPException(status_code=502, detail=f"Sell failed: {str(e)}")


@router.get("/coinbase/usd-balance")
async def get_coinbase_usd_balance():
    """Get real-time Coinbase USD cash balance (not holdings, just cash).
    This is the trading capital available for entries."""
    import jwt
    import time
    import base64

    coinbase_key_name = os.getenv("COINBASE_API_KEY_NAME", "")
    coinbase_private_key = os.getenv("COINBASE_API_PRIVATE_KEY", "").replace("\\n", "\n")

    if not (coinbase_key_name and coinbase_private_key):
        return {"usd_balance": 0, "status": "unconfigured"}

    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.backends import default_backend

        raw = coinbase_private_key.strip()
        if raw.startswith("-----BEGIN"):
            private_key = serialization.load_pem_private_key(raw.encode(), password=None, backend=default_backend())
            algorithm = "ES256"
        else:
            decoded = base64.b64decode(raw)
            private_key = Ed25519PrivateKey.from_private_bytes(decoded[:32])
            algorithm = "EdDSA"

        now = int(time.time())
        payload = {
            "sub": coinbase_key_name,
            "iss": "cdp",
            "nbf": now,
            "exp": now + 120,
            "uri": "GET api.coinbase.com/api/v3/brokerage/accounts",
        }
        import secrets
        headers_for_jwt = {"kid": coinbase_key_name, "nonce": secrets.token_hex(16)}
        jwt_token = jwt.encode(payload, private_key, algorithm=algorithm, headers=headers_for_jwt)

        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.coinbase.com/api/v3/brokerage/accounts",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    log.error(f"Coinbase API returned {resp.status}")
                    return {"usd_balance": 0, "status": "error", "detail": f"HTTP {resp.status}"}

                data = await resp.json()
                accounts = data.get("accounts", [])

                usd_account = next(
                    (a for a in accounts if a.get("currency") == "USD"),
                    None
                )

                balance = round(float(usd_account.get("available_balance", {}).get("value", 0)), 2) if usd_account else 0.0
                return {
                    "usd_balance": balance,
                    "status": "ok",
                    "currency": "USD",
                    "account_type": "Coinbase Advanced Trade"
                }

    except Exception as e:
        log.error(f"Coinbase USD balance fetch failed: {e}")


def _get_utc_timestamp():
    """Helper to avoid datetime scoping issues."""
    try:
        return datetime.now(timezone.utc).isoformat()
    except NameError as e:
        log.error(f"Datetime error in helper: {e}")
        raise


@router.get("/live-dashboard-data")
async def get_live_dashboard_data_v2(db: AsyncSession = Depends(get_db)):
    """Comprehensive endpoint for the Empire trading dashboard.
    Returns: balance, open positions, recent trades, daily P&L, bot status, win rate."""
    timestamp_str = _get_utc_timestamp()

    try:
        import crypto_coinbase_bot
    except ImportError:
        raise HTTPException(status_code=503, detail="Crypto bot not available")

    try:
        # Fetch Coinbase USD balance
        coinbase_balance = 0
        try:
            if crypto_coinbase_bot.COINBASE_API_KEY_NAME and crypto_coinbase_bot.COINBASE_API_PRIVATE_KEY:
                import jwt as pyjwt
                import uuid

                key_name = crypto_coinbase_bot.COINBASE_API_KEY_NAME
                private_key_str = crypto_coinbase_bot.COINBASE_API_PRIVATE_KEY

                # Build JWT
                dt_obj = globals()['datetime']
                tz_obj = globals()['timezone']
                td_obj = globals()['timedelta']
                now = dt_obj.now(tz_obj.utc)
                expiry = now + td_obj(minutes=1)
                payload = {
                    "sub": key_name,
                    "iss": "cdp_service",
                    "nbf": int(now.timestamp()),
                    "exp": int(expiry.timestamp()),
                    "iat": int(now.timestamp()),
                    "uri": "/api/v3/brokerage/accounts"
                }

                try:
                    # Try ES256 first (ECDSA)
                    token = pyjwt.encode(payload, private_key_str, algorithm="ES256", headers={"alg": "ES256", "kid": key_name, "nonce": str(uuid.uuid4())})
                except Exception:
                    # Fallback to EdDSA
                    token = pyjwt.encode(payload, private_key_str, algorithm="EdDSA", headers={"alg": "EdDSA", "kid": key_name, "nonce": str(uuid.uuid4())})

                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                }

                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        "https://api.coinbase.com/api/v3/brokerage/accounts",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            usd_account = next(
                                (a for a in data.get("accounts", []) if a.get("currency") == "USD"),
                                None
                            )
                            if usd_account:
                                coinbase_balance = round(float(usd_account.get("available_balance", {}).get("value", 0)), 2)
        except Exception as e:
            log.warning(f"Coinbase balance fetch failed, using cached: {e}")
            coinbase_balance = getattr(crypto_coinbase_bot, 'LAST_KNOWN_BALANCE', 483.00)

        # Fetch Alpaca account data
        alpaca_buying_power = 0
        alpaca_equity = 0
        try:
            import aiohttp
            alpaca_key = os.getenv("ALPACA_API_KEY", "")
            alpaca_secret = os.getenv("ALPACA_SECRET_KEY", "")
            if alpaca_key and alpaca_secret:
                headers = {
                    "APCA-API-KEY-ID": alpaca_key,
                    "APCA-API-SECRET-KEY": alpaca_secret
                }
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        "https://paper-api.alpaca.markets/v2/account",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        if resp.status == 200:
                            account = await resp.json()
                            alpaca_buying_power = round(float(account.get("buying_power", 0)), 2)
                            alpaca_equity = round(float(account.get("equity", 0)), 2)
        except Exception as e:
            log.warning(f"Alpaca account fetch failed: {e}")

        # Get open positions from bot
        open_positions = []
        try:
            positions = getattr(crypto_coinbase_bot, 'open_crypto_positions', {})
            for symbol, pos_data in list(positions.items())[:5]:  # Max 5 displayed
                open_positions.append({
                    "symbol": symbol,
                    "entry_price": pos_data.get("entry_price", 0),
                    "qty": pos_data.get("qty", 0),
                    "entry_time": pos_data.get("entry_time", "unknown"),
                    "current_price": pos_data.get("current_price", pos_data.get("entry_price", 0)),
                    "unrealized_pnl": round((pos_data.get("current_price", pos_data.get("entry_price", 0)) - pos_data.get("entry_price", 0)) * pos_data.get("qty", 0), 2)
                })
        except Exception as e:
            log.warning(f"Open positions fetch failed: {e}")

        # Query database for closed trades (where exit_at is not None)
        from models import CryptoTradeLog
        recent_trades = []
        total_profit = 0.0
        win_count = 0
        try:
            result = await db.execute(
                select(CryptoTradeLog)
                .where(CryptoTradeLog.exit_at != None)
                .order_by(CryptoTradeLog.exit_at.desc())
                .limit(10)
            )
            trades_from_db = result.scalars().all()
            for trade in trades_from_db:
                profit = trade.net_pnl if trade.net_pnl else (trade.gross_pnl if trade.gross_pnl else 0)
                if profit > 0:
                    win_count += 1
                total_profit += profit
                profit_pct = 0
                if trade.entry_price and trade.entry_price > 0:
                    profit_pct = round(((trade.exit_price - trade.entry_price) / trade.entry_price * 100), 2) if trade.exit_price else 0
                recent_trades.append({
                    "symbol": trade.symbol or "unknown",
                    "entry_price": round(trade.entry_price, 2) if trade.entry_price else 0,
                    "exit_price": round(trade.exit_price, 2) if trade.exit_price else 0,
                    "qty": round(trade.position_size, 4) if trade.position_size else 0,
                    "profit": round(profit, 2),
                    "profit_pct": profit_pct,
                    "close_time": trade.exit_at.isoformat() if trade.exit_at else "unknown"
                })
        except Exception as e:
            log.warning(f"Trade history query failed: {e}")

        # Calculate stats from trades
        daily_trades = len(recent_trades)
        win_rate = round((win_count / daily_trades * 100), 1) if daily_trades > 0 else 0

        # Bot status
        crypto_bot_active = getattr(crypto_coinbase_bot, 'BOT_RUNNING', True)
        alpaca_bot_active = True  # Assume active; could check via prop_bot

        return {
            "timestamp": timestamp_str,
            "accounts": {
                "coinbase": {
                    "name": "Coinbase (Crypto 24/7)",
                    "balance": coinbase_balance,
                    "starting_balance": 483.00,
                    "daily_profit": round(sum(t.get("profit", 0) for t in recent_trades), 2),
                    "total_profit": round(total_profit, 2),
                    "growth_percent": round((total_profit / 483.00 * 100), 2) if total_profit > 0 else 0
                },
                "alpaca": {
                    "name": "Alpaca (Stocks & Futures)",
                    "buying_power": alpaca_buying_power,
                    "equity": alpaca_equity,
                    "daily_profit": 0,  # TODO: Calculate from Alpaca trade logs when database logging added
                    "total_profit": 0,  # TODO: Calculate from Alpaca trade logs when database logging added
                    "growth_percent": 0
                }
            },
            "positions": {
                "open": open_positions,
                "count": len(open_positions),
                "max": 3
            },
            "trading": {
                "recent_trades": recent_trades[::-1],  # Newest first
                "trades_today": daily_trades,
                "win_rate": win_rate,
                "win_count": win_count
            },
            "bots": {
                "crypto": {
                    "status": "active" if crypto_bot_active else "inactive",
                    "name": "Coinbase (24/7)",
                    "pairs": getattr(crypto_coinbase_bot, 'CRYPTO_PAIRS', [])[:10]
                },
                "alpaca": {
                    "status": "active" if alpaca_bot_active else "inactive",
                    "name": "Alpaca Hybrid (Stocks + Futures)",
                    "strategy": "Day trading + 24/5 futures"
                }
            }
        }

    except Exception as e:
        import traceback
        log.error(f"Dashboard data fetch failed: {e}")
        log.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/trading-dashboard/logs")
async def get_trading_logs(limit: int = 50, event_type: str = None):
    """
    Get recent trading activity logs with optional filtering.

    Query params:
    - limit: Max events to return (default 50)
    - event_type: Filter by type (profit_lock, trade_alert, status, all)
    """
    try:
        from log_monitor import monitor

        if event_type and event_type != "all":
            events = monitor.get_recent_events(limit=limit, event_type=event_type)
        else:
            events = monitor.get_recent_events(limit=limit)

        return {
            "success": True,
            "event_count": len(events),
            "events": events,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except ImportError:
        return {
            "success": False,
            "error": "Log monitor not available",
            "events": []
        }


@router.get("/api/trading-dashboard/bot-activity")
async def get_bot_activity():
    """
    Get real-time bot activity metrics:
    - Bot status (active/inactive)
    - Trade count today
    - Profit locks today/week
    - Last profit lock event
    - Last trade alert
    """
    try:
        from log_monitor import monitor

        return {
            "success": True,
            "activity": monitor.get_bot_status(),
            "summary": monitor.get_summary(),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except ImportError:
        return {
            "success": False,
            "error": "Log monitor not available",
            "activity": {
                "crypto_bot": {"status": "unknown"},
                "alpaca_bot": {"status": "unknown"}
            }
        }


@router.get("/api/trading-dashboard/profit-locks")
async def get_profit_locks():
    """
    Get all profit-lock events from today and this week.
    Used for monitoring when trades are being closed and profits locked.
    """
    try:
        from log_monitor import monitor

        profit_lock_events = monitor.get_recent_events(limit=100, event_type="profit_lock")

        return {
            "success": True,
            "locks_today": monitor.profit_locks_today,
            "locks_week": monitor.profit_locks_week,
            "recent_locks": profit_lock_events,
            "last_lock": monitor.last_profit_lock.to_dict() if monitor.last_profit_lock else None,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except ImportError:
        return {
            "success": False,
            "error": "Log monitor not available",
            "locks_today": 0,
            "locks_week": 0,
            "recent_locks": []
        }
