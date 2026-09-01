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
import random
import uuid
from datetime import datetime, timezone, timedelta

import aiohttp
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func, case, text, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, AsyncSessionLocal
from admin_auth import require_admin_key
from models import TradingBotState, WithdrawalRequest, CryptoTreeBranch, BotPosition, Payment, CryptoCoinTradeHistory, PricePredictionCalibration, PricePredictionLog, BtcTickerWindowAnchor, AlpacaBranch, CombinedEquitySnapshot, AlpacaBacktestRun

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

try:
    import macro_event_backtest as macro_event_backtest_module
except Exception as e:
    log.warning(f"macro_event_backtest not importable, /macro-event-backtest will report unavailable: {e}")
    macro_event_backtest_module = None

try:
    import btc_price_projection as btc_price_projection_module
except Exception as e:
    log.warning(f"btc_price_projection not importable, /family-tree-status/btc-projection will report unavailable: {e}")
    btc_price_projection_module = None

try:
    import crypto_grid_bot as crypto_grid_bot_module
except Exception as e:
    log.warning(f"crypto_grid_bot not importable, /grid-status will report unavailable: {e}")
    crypto_grid_bot_module = None

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
# Defaulted to 0.0 (no skim at all), matching crypto_family_tree_bot.py's
# own PROFIT_SKIM_PCT change, per the account owner's explicit request:
# "take away the lock profit, I don't want that anymore for any of my
# stuff, I want all my money to be making money." A real closed position's
# full profit now returns to the account's real buying power on close -
# nothing is walled off into the locked ledger. Still env-overridable
# (ALPACA_PROFIT_SKIM_PCT) if a skim is ever wanted again.
ALPACA_PROFIT_SKIM_PCT = float(os.getenv("ALPACA_PROFIT_SKIM_PCT", "0.0"))
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

TICKER_CRYPTO_PRODUCTS = ["BTC-USD", "ETH-USD", "XRP-USD", "DOGE-USD", "SOL-USD"]
TICKER_STOCK_SYMBOLS = ["SPY", "QQQ"]


@router.get("/ticker", dependencies=[Depends(require_admin_key)])
async def get_ticker():
    """Real live price ticker for the dashboards - per the account owner's
    explicit request for a scrolling price strip like the one on Fortune's
    site. Real data only, no trading involved (read-only):
    - Crypto: Coinbase's public, unauthenticated candles endpoint (the
      exact same real fetch crypto_btc_compound_bot.py's own ATR/RSI
      calcs already use - engine._fetch_candles), ~25 hours of 5-min
      candles per coin. % change is the real move from the oldest candle
      in that window to the latest.
    - Stocks: Alpaca's real market-data bars API (same feed=iex pattern
      alpaca_selection_backtest.py's _fetch_bars already uses), 15-min
      bars over the last day. Same real % change calc.

    NOT "Powered by Binance" like the reference screenshot - this account
    has no Binance integration or credentials anywhere in this codebase;
    the real data sources here are Coinbase and Alpaca, the same ones
    every other real number on these dashboards already comes from."""
    items = []

    async with aiohttp.ClientSession() as session:
        if crypto_family_tree_bot_module is not None:
            crypto_engine = crypto_family_tree_bot_module.engine
            for product_id in TICKER_CRYPTO_PRODUCTS:
                candles = await crypto_engine._fetch_candles(session, product_id)
                if candles is None:
                    continue
                closes, _highs, _lows = candles
                price = closes[-1]
                change_pct = round((closes[-1] - closes[0]) / closes[0] * 100, 2) if closes[0] else 0.0
                items.append({
                    "symbol": product_id.replace("-USD", ""),
                    "price": round(price, 6 if price < 1 else 2),
                    "change_pct": change_pct,
                    "kind": "crypto",
                })

        if prop_bot_module is not None and ALPACA_KEY and ALPACA_SECRET:
            for symbol in TICKER_STOCK_SYMBOLS:
                start = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
                url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=15Min&start={start}&limit=1000&feed=iex"
                try:
                    async with session.get(url, headers=prop_bot_module.get_headers(), timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status != 200:
                            continue
                        data = await r.json()
                        bars = data.get("bars", [])
                        if len(bars) < 2:
                            continue
                        closes = [b["c"] for b in bars]
                        price = closes[-1]
                        change_pct = round((closes[-1] - closes[0]) / closes[0] * 100, 2) if closes[0] else 0.0
                        items.append({"symbol": symbol, "price": round(price, 2), "change_pct": change_pct, "kind": "stock"})
                except Exception as e:
                    log.warning(f"[ticker] failed to fetch {symbol}: {e}")

    return {"items": items, "generated_at": datetime.utcnow().isoformat()}


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
    real_usdc_balance = None
    if crypto_family_tree_bot_module is not None:
        engine = crypto_family_tree_bot_module.engine
        async with engine.aiohttp.ClientSession() as session:
            for bot_name, pos in positions_by_bot.items():
                price, _atr_pct = await engine.get_price_and_volatility(session, pos.symbol)
                if price is not None:
                    current_price_by_bot[bot_name] = price
            real_balance, _err = await engine.get_usd_balance(session)
            # Real, read-only visibility into a confirmed-live confusion:
            # get_usd_balance() (and therefore spendable_for_spawn below)
            # only ever sees the literal USD account - a real balance
            # sitting in USDC (Coinbase's own "Earn APY by converting USD
            # to USDC" feature can put it there) is invisible to it and
            # can make a genuinely healthy account look like it has $0 or
            # negative real spendable cash. Never folded into
            # spendable_for_spawn or any order-execution path - whether a
            # BTC-USD order can be funded directly from USDC is
            # unconfirmed from this sandbox, and the account owner's own
            # documented choice for this exact scenario is to convert it
            # back to USD by hand. This is purely so that choice can be
            # made with the real number in front of them.
            real_usdc_balance, _usdc_err = await engine.get_usdc_balance(session)

    # Fetched ONCE per request (a real DB read), not per-branch inside the
    # loop below - the same real, live, dashboard-switchable trailing-stop
    # width run_branch_cycle() itself reads every cycle, so compute_sell_advice()
    # can never show a different trail than what the bot is actually
    # protecting a position with right now.
    live_trail_pct = (
        await crypto_family_tree_bot_module.get_live_trailing_stop_pct()
        if crypto_family_tree_bot_module is not None else None
    )

    out = []
    total_equity_now = 0.0
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
        # Real, live equity - same formula run_branch_cycle() uses to
        # decide the drawdown breach (allocated_usd + unrealized P&L while
        # holding) - so the dashboard's own drawdown% can never disagree
        # with what actually pauses the branch. peak_equity can be NULL on
        # a row from before this column existed - treat that the same
        # "not yet initialized, self-heals to today's equity" way the
        # live bot's own read does, rather than showing a fabricated 0%.
        equity_now = b.allocated_usd
        if pos is not None and current_price is not None:
            equity_now = b.allocated_usd + pos.qty * (current_price - pos.entry_price)
        total_equity_now += equity_now
        peak_equity = b.peak_equity if b.peak_equity else equity_now
        drawdown_pct = ((peak_equity - equity_now) / peak_equity * 100) if peak_equity > 0 else 0.0
        drawdown_breaker_pct = (
            crypto_family_tree_bot_module.DRAWDOWN_BREAKER_PCT * 100 if crypto_family_tree_bot_module is not None else None
        )

        out.append({
            "bot_name": b.bot_name,
            "product_id": b.product_id,
            "parent_bot_name": b.parent_bot_name,
            "allocated_usd": round(b.allocated_usd, 2),
            "equity_floor": round(b.equity_floor, 2),
            "next_unlock_tier": round(b.next_unlock_tier, 2),
            "peak_equity": round(peak_equity, 2),
            "drawdown_pct": round(drawdown_pct, 1),
            "drawdown_breaker_pct": round(drawdown_breaker_pct, 1) if drawdown_breaker_pct is not None else None,
            "drawdown_breached": drawdown_pct >= drawdown_breaker_pct if drawdown_breaker_pct is not None else False,
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
                # bot's own real STOP/TRAILING-STOP exit check (see
                # compute_sell_advice) so the advice can never disagree with
                # what the bot is actually about to do on its own.
                "sell_advice": crypto_family_tree_bot_module.compute_sell_advice(
                    pos.entry_price, pos.qty, pos.target_price, pos.stop_price,
                    current_price, pos.peak_pct, live_trail_pct,
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
        # Grid Bot draws from this exact same shared real Coinbase wallet -
        # its own committed capital has to come off this figure too, or
        # this endpoint's own "real free cash" number would silently
        # disagree with Grid Bot's (see crypto_grid_bot.get_real_free_cash_usd,
        # the same real subtraction, kept in sync with this one).
        grid_allocated_sum = (
            await crypto_grid_bot_module.get_grid_allocated_total()
        ) if crypto_grid_bot_module is not None else 0.0
        spendable_for_spawn = round(real_balance - locked_usd - flat_allocated_sum - grid_allocated_sum, 2)
        can_spawn = seed_usd is not None and spendable_for_spawn >= seed_usd

    crypto_passive_mode = await crypto_family_tree_bot_module.is_crypto_passive_mode() if crypto_family_tree_bot_module else False
    rolling_expectancy = await crypto_family_tree_bot_module.get_rolling_expectancy() if crypto_family_tree_bot_module else None
    exit_mode = await crypto_family_tree_bot_module.get_live_exit_mode() if crypto_family_tree_bot_module else "trailing_stop"
    trailing_stop_pct = await crypto_family_tree_bot_module.get_live_trailing_stop_pct() if crypto_family_tree_bot_module else None
    reversal_trade_active = await crypto_family_tree_bot_module.get_reversal_trade_active() if crypto_family_tree_bot_module else False

    return {
        "branches": out,
        "branch_count": len(out),
        "total_allocated_usd": round(sum(b["allocated_usd"] for b in out), 2),
        # Real total equity across every branch (each branch's own tracked
        # capital PLUS its real unrealized P&L while holding, the exact
        # same equity_now formula run_branch_cycle()'s own drawdown-breach
        # check uses) plus locked_usd - real money already skimmed off a
        # winning sell, still very much part of the account's real net
        # worth, just earmarked out of the compounding loop. Backs the
        # combined $1M-goal tracker (see get_combined_equity_progress
        # below) - a real, useful aggregate on its own, not built solely
        # for that feature.
        "total_equity_usd": round(total_equity_now + locked_usd, 2),
        "locked_usd": locked_usd,
        "spendable_for_spawn": spendable_for_spawn,
        "seed_usd": seed_usd,
        "can_spawn": can_spawn,
        "crypto_passive_mode": crypto_passive_mode,
        "rolling_expectancy": rolling_expectancy,
        "exit_mode": exit_mode,
        "trailing_stop_pct": trailing_stop_pct,
        "reversal_trade_active": reversal_trade_active,
        "real_usd_balance": round(real_balance, 2) if real_balance is not None else None,
        "real_usdc_balance": round(real_usdc_balance, 2) if real_usdc_balance is not None else None,
    }


COMBINED_GOAL_USD = 1_000_000.0
# Throttles how often a real CombinedEquitySnapshot row is written -
# hourly is plenty of real resolution for the account owner's own stated
# use ("visualize monthly down the line how close we can get to it"),
# and keeps a month of real history to a small, cheap table (~720 rows)
# rather than growing unbounded from every dashboard poll.
COMBINED_EQUITY_SNAPSHOT_INTERVAL_MINUTES = float(os.getenv("COMBINED_EQUITY_SNAPSHOT_INTERVAL_MINUTES", "60"))


async def _log_combined_equity_snapshot_if_due(db: AsyncSession, alpaca_equity, crypto_equity, combined_equity):
    """Best-effort, throttled snapshot write - piggybacks on whichever
    dashboard happens to poll /combined-equity-progress next, the same
    "log if due" pattern already validated by the BTC 15-minute
    prediction log (_log_new_btc_prediction_if_due). Wrapped in its own
    try/except so a real logging hiccup can never break the live numbers
    this same endpoint also returns."""
    try:
        result = await db.execute(
            select(CombinedEquitySnapshot).order_by(CombinedEquitySnapshot.created_at.desc()).limit(1)
        )
        last = result.scalar_one_or_none()
        if last is not None:
            elapsed_minutes = (datetime.utcnow() - last.created_at).total_seconds() / 60.0
            if elapsed_minutes < COMBINED_EQUITY_SNAPSHOT_INTERVAL_MINUTES:
                return
        db.add(CombinedEquitySnapshot(
            alpaca_equity=alpaca_equity, crypto_equity=crypto_equity, combined_equity=combined_equity,
        ))
        await db.commit()
    except Exception as exc:
        log.warning(f"[dashboard] combined-equity snapshot logging failed (non-fatal): {exc}")


def _project_years_to_goal(history_rows, combined_equity: float, goal: float):
    """Real, honest linear extrapolation from the same real momentum the
    dashboard's own momentum line already shows - "at this pace, how long
    to $1M" - per the account owner's explicit request for a report that
    "makes sense of this" and "let us know how to move forward." NOT a
    promise or a trading-performance grade: the real delta between the
    first and last real snapshot reflects everything that happened in
    that window, real trading gains AND any new cash added - the caller
    is expected to caveat it that way.

    Returns (years, basis_days) - years is None when there isn't enough
    real history yet (fewer than 2 snapshots) OR when the real recent
    trend is flat/negative (extrapolating a falling or flat line to a
    HIGHER goal is meaningless - reported as None, not a nonsensical
    negative or infinite number). basis_days is returned even when years
    is None so the caller can still say how much real history the "no
    projection yet" verdict itself is based on."""
    if not history_rows or len(history_rows) < 2:
        return None, None
    first, last = history_rows[0], history_rows[-1]
    span_days = (last.created_at - first.created_at).total_seconds() / 86400.0
    if span_days <= 0:
        return None, None
    delta = last.combined_equity - first.combined_equity
    if delta <= 0:
        return None, round(span_days, 2)
    daily_rate = delta / span_days
    remaining = goal - combined_equity
    if remaining <= 0:
        return 0.0, round(span_days, 2)
    years = (remaining / daily_rate) / 365.25
    return round(years, 1), round(span_days, 2)


def _build_progress_observations(alpaca_data, crypto_data):
    """Real, concrete observations about what's currently helping or
    hurting progress toward the combined goal - per the account owner's
    explicit ask for "how we can get there and keep moving forward."
    Built entirely from data alpaca_data/crypto_data already computed
    this same poll (get_alpaca_overview/get_family_tree_status, no new
    live API calls) - never fabricates a prediction or a dollar-amount
    promise, only reports real, already-verified system state and points
    at real, already-built levers (a paused branch, idle cash, a paused
    rolling-expectancy gate) the account owner can actually act on right
    now. Returns a list of {icon, tone, text} - tone is 'warn' (orange),
    'info' (navy), or 'good' (green), for the dashboard to color-code."""
    observations = []

    if crypto_data:
        crypto_retired = bool(crypto_data.get("crypto_passive_mode"))
        if crypto_retired:
            observations.append({
                "icon": "🔒", "tone": "warn",
                "text": "Crypto family tree is retired (passive mode) - no new entries or exits are happening on that side at all.",
            })
        # Once retired, no branch can ever open a new position for ANY
        # reason - the rolling-expectancy pause and the drawdown-breach
        # pause both become moot real explanations for something that's
        # already fully explained by retirement. Showing them anyway is
        # genuinely confusing, not informative - a real bug the account
        # owner's own screenshot surfaced (three banners, two of them
        # giving different reasons for the same already-explained fact).
        rolling = crypto_data.get("rolling_expectancy")
        if not crypto_retired and rolling and rolling.get("negative"):
            win_rate = rolling.get("win_rate")
            win_count = rolling.get("win_count")
            loss_count = rolling.get("loss_count")
            avg_win = rolling.get("avg_win")
            avg_loss = rolling.get("avg_loss")
            total_pnl = rolling.get("total_pnl")
            breakdown = ""
            if win_rate is not None:
                breakdown = (
                    f" Breakdown: {win_count} win(s) averaging ${avg_win:.2f} each, {loss_count} loss(es) "
                    f"averaging ${avg_loss:.2f} each ({win_rate:.1f}% win rate) - real total across the window: "
                    f"${total_pnl:.2f}. A high win rate can still add up to a real net loss when the losses run "
                    f"bigger on average than the wins do, which is what's happening here."
                )
            total_text = f" (a real total of ${total_pnl:.2f} across the window, not just ${rolling['expectancy']:.2f})" if total_pnl is not None else ""
            observations.append({
                "icon": "🐢", "tone": "warn",
                "text": (
                    f"Crypto entries are tree-wide paused - the last {rolling['num_trades']} real trades "
                    f"averaged ${rolling['expectancy']:.2f} each{total_text}.{breakdown} Clears automatically "
                    f"once real recent wins bring the average back positive - no action needed, just something worth knowing about."
                ),
            })
        branches = crypto_data.get("branches") or []
        paused_dd = [b for b in branches if b.get("drawdown_breached")]
        if not crypto_retired and paused_dd:
            names = ", ".join(
                b["bot_name"].replace("crypto_tree_", "").replace("_usd", "").upper() for b in paused_dd[:4]
            )
            more = f" (+{len(paused_dd) - 4} more)" if len(paused_dd) > 4 else ""
            observations.append({
                "icon": "🛑", "tone": "warn",
                "text": f"{len(paused_dd)} branch(es) paused by the drawdown breaker - {names}{more}. Add real cash to resume, or leave them paused on purpose.",
            })
        spendable = crypto_data.get("spendable_for_spawn")
        if spendable is not None and spendable >= 25:
            observations.append({
                "icon": "💵", "tone": "info",
                "text": f"${spendable:,.2f} of real free crypto cash isn't deployed anywhere in the tree right now - Move Cash Between Branches or Add Cash can put it to work.",
            })

    if alpaca_data:
        if alpaca_data.get("alpaca_passive_mode"):
            observations.append({
                "icon": "🔒", "tone": "warn",
                "text": "Alpaca active trading is retired (passive mode) - only the held SPY position moves with the market, nothing new is being traded.",
            })
        elif alpaca_data.get("cash") is not None and alpaca_data["cash"] >= 25:
            observations.append({
                "icon": "💵", "tone": "info",
                "text": f"${alpaca_data['cash']:,.2f} of real Alpaca cash is sitting uninvested right now.",
            })

    if not observations:
        observations.append({
            "icon": "✅", "tone": "good",
            "text": "Nothing is currently paused or sitting idle on either side - both systems are actively working with what they have.",
        })

    return observations


@router.get("/combined-equity-progress", dependencies=[Depends(require_admin_key)])
async def get_combined_equity_progress(db: AsyncSession = Depends(get_db)):
    """Real, combined progress toward the account owner's own $1,000,000
    goal across BOTH real trading systems at once - per their explicit
    request: "link the coinbase percentage with that too... I just want
    to visualize it on one thing... [and] visualize monthly down the
    line how close we can get to it." The existing goal gauge on
    alpaca_dashboard.html only ever tracked Alpaca's own equity; this
    adds Coinbase's real total (see total_equity_usd on
    get_family_tree_status above) into one combined figure, plus a real,
    accumulating history so the combined number's own MOMENTUM (not just
    where it stands right now) becomes visible over time.

    Reuses the exact same real, already-validated functions the two
    individual dashboards already call (get_alpaca_overview,
    get_family_tree_status) rather than re-deriving either number a
    second way - this can never show a different reality than either
    dashboard's own live figures. Each side is fetched independently and
    fails OPEN on its own (a real Alpaca or Coinbase hiccup degrades that
    one side to null/0 rather than taking down the whole combined view) -
    never silently reports 0 as if that were a real, confirmed balance."""
    alpaca_data = None
    alpaca_equity = None
    alpaca_error = None
    try:
        alpaca_data = await get_alpaca_overview(db)
        alpaca_equity = alpaca_data["equity"]
    except Exception as exc:
        alpaca_error = str(exc)
        log.warning(f"[dashboard] combined-equity: Alpaca side unavailable this poll: {exc}")

    crypto_data = None
    crypto_equity = None
    crypto_error = None
    try:
        crypto_data = await get_family_tree_status(db)
        crypto_equity = crypto_data["total_equity_usd"]
    except Exception as exc:
        crypto_error = str(exc)
        log.warning(f"[dashboard] combined-equity: crypto side unavailable this poll: {exc}")

    combined_equity = (alpaca_equity or 0.0) + (crypto_equity or 0.0)
    combined_progress_pct = round(min(100.0, (combined_equity / COMBINED_GOAL_USD) * 100), 4)

    # Only ever logs a real snapshot when BOTH real sides are actually
    # available this poll - a snapshot with one side silently zeroed out
    # (a real Alpaca or Coinbase outage) would permanently understate
    # that moment in the real history forever; better to skip the write
    # and simply catch it on the next successful poll instead.
    if alpaca_equity is not None and crypto_equity is not None:
        await _log_combined_equity_snapshot_if_due(db, alpaca_equity, crypto_equity, combined_equity)

    history_result = await db.execute(
        select(CombinedEquitySnapshot).order_by(CombinedEquitySnapshot.created_at.desc()).limit(800)
    )
    history = list(reversed(history_result.scalars().all()))

    projected_years_to_goal, projection_basis_days = _project_years_to_goal(history, combined_equity, COMBINED_GOAL_USD)
    observations = _build_progress_observations(alpaca_data, crypto_data)

    return {
        "alpaca_equity": round(alpaca_equity, 2) if alpaca_equity is not None else None,
        "alpaca_error": alpaca_error,
        "crypto_equity": round(crypto_equity, 2) if crypto_equity is not None else None,
        "crypto_error": crypto_error,
        "combined_equity": round(combined_equity, 2),
        "goal": COMBINED_GOAL_USD,
        "combined_progress_pct": combined_progress_pct,
        "history": [h.to_dict() for h in history],
        "projected_years_to_goal": projected_years_to_goal,
        "projection_basis_days": projection_basis_days,
        "observations": observations,
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


@router.get("/family-tree-status/activity-feed", dependencies=[Depends(require_admin_key)])
async def get_activity_feed(limit: int = 50):
    """Real, live feed of what the bot has actually just done - per the
    account owner's explicit request to SEE it working (buying, selling,
    spawning, reinforcing) without digging through Railway's own logs.
    Backed by CryptoActivityEvent, written at the exact same real moment
    as the matching Railway log line in crypto_family_tree_bot.py (BUY in
    run_branch_cycle, SELL in _branch_sell_and_settle, SPAWN/REINFORCE in
    _maybe_spawn_child) - the dashboard can never show something
    different from what actually happened. Read-only."""
    if crypto_family_tree_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_family_tree_bot module not available")
    events = await crypto_family_tree_bot_module.get_activity_feed(limit=limit)
    return {"events": events, "event_count": len(events)}


BTC_PREDICTION_LOG_INTERVAL_MINUTES = 15  # don't log a new individual prediction more often than this real interval


def _current_prediction_window(now: datetime = None):
    """Real wall-clock-aligned 15-minute window boundaries (:00/:15/:30/:45
    past the hour, UTC) - per the account owner's explicit request to match
    a real third-party prediction-market app's own countdown, so pushing
    the button "no matter what" lands on the same window that app is
    already partway through, instead of a phase that drifts based on
    whenever this dashboard happened to first get polled. This can't be
    verified against that app's own real internal clock from this sandbox
    (no live access to it) - quarter-hour UTC alignment is the standard,
    near-universal convention these "N-minute" markets use, and for any
    real-world timezone offset in whole or half hours (true for the US and
    almost everywhere else), it lines up with the same boundaries on a
    local wall clock too."""
    now = now or datetime.utcnow()
    minute_bucket = (now.minute // BTC_PREDICTION_LOG_INTERVAL_MINUTES) * BTC_PREDICTION_LOG_INTERVAL_MINUTES
    window_start = now.replace(minute=minute_bucket, second=0, microsecond=0)
    window_end = window_start + timedelta(minutes=BTC_PREDICTION_LOG_INTERVAL_MINUTES)
    return window_start, window_end


async def _resolve_due_btc_predictions(db, current_price: float, product_id: str):
    """Resolves every real, unresolved individual prediction (see
    PricePredictionLog) whose real 15-minute window has actually passed,
    using the current real price already fetched for this same live
    call - no extra API request. resolution_delay_seconds records how
    late relative to resolve_at the real check landed, so a stale
    resolution (e.g. the dashboard sat closed for a while) stays
    honestly visible in the data instead of hidden."""
    now = datetime.utcnow()
    result = await db.execute(
        select(PricePredictionLog).where(
            PricePredictionLog.product_id == product_id,
            PricePredictionLog.resolved == False,
            PricePredictionLog.resolve_at <= now,
        )
    )
    due = result.scalars().all()
    for row in due:
        row.actual_price = current_price
        row.resolved = True
        row.hit_1sigma = row.band_1sigma_low <= current_price <= row.band_1sigma_high
        row.hit_2sigma = row.band_2sigma_low <= current_price <= row.band_2sigma_high
        row.abs_error_pct = round(abs(current_price - row.projected_price) / row.price_at_prediction * 100, 4)
        row.resolution_delay_seconds = (now - row.resolve_at).total_seconds()
    if due:
        await db.commit()


_btc_prediction_log_migrated = False


async def _ensure_btc_prediction_log_dedupe_and_unique_index():
    """One-time, safe-to-call-repeatedly startup migration (guarded by the
    module-level flag below so it only actually runs once per process).

    Real bug, confirmed live on the account owner's own dashboard: the
    same "06:50 AM predicted $78,851.64" window was logged FOUR times.
    Root cause is the same shape of race already fixed elsewhere in this
    codebase for TradingBotState.bot_name and BotPosition
    (crypto_family_tree_bot.py) - _log_new_btc_prediction_if_due does a
    plain "check if a row exists for this window, then insert" with no
    real DB-level uniqueness backing it, and with the dashboard commonly
    polled from more than one place at once (the ticker at 10s, the
    projection panel at 30s, possibly more than one open browser tab),
    two calls landing close together can both see "no row yet" and both
    insert - PricePredictionLog.predicted_at was never declared
    unique=True, and even if it had been, Base.metadata.create_all()
    only applies that to a table at CREATE time, never retroactively to
    one that already existed.

    Fixed the same two-part way as every other duplicate-row race in
    this codebase: dedupe any real duplicates that already exist (a
    unique index can't be created while they do), then add the real
    DB-level constraint so this exact race can't recur. When two
    duplicate rows differ (one resolved, one still pending), the
    resolved one survives - its real hit/miss data is never thrown away
    in favor of a still-pending duplicate."""
    global _btc_prediction_log_migrated
    if _btc_prediction_log_migrated:
        return
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(PricePredictionLog).order_by(
                    PricePredictionLog.product_id, PricePredictionLog.predicted_at, PricePredictionLog.id
                )
            )
            rows = result.scalars().all()
            seen = {}
            removed = 0
            for row in rows:
                key = (row.product_id, row.predicted_at)
                survivor = seen.get(key)
                if survivor is None:
                    seen[key] = row
                    continue
                if row.resolved and not survivor.resolved:
                    await db.delete(survivor)
                    seen[key] = row
                else:
                    await db.delete(row)
                removed += 1
            if removed:
                await db.commit()
                log.warning(f"[btc-projection] removed {removed} duplicate PricePredictionLog row(s) from a real concurrent-poll race")

            await db.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_price_prediction_log_product_predicted_unique "
                "ON price_prediction_log (product_id, predicted_at)"
            ))
            await db.commit()
        _btc_prediction_log_migrated = True
        log.info("[btc-projection] price_prediction_log (product_id, predicted_at) uniqueness enforced at the DB level")
    except Exception as e:
        log.warning(f"[btc-projection] could not dedupe/enforce uniqueness on price_prediction_log: {e}")


async def _log_new_btc_prediction_if_due(db, projection: dict):
    """Logs a new real, individual prediction for the live "did it hit"
    track record - keyed to the real, wall-clock-aligned window
    (_current_prediction_window above) rather than "15 minutes since the
    last one," so every poll - from either the projection panel or the
    live ticker - converges on the exact same window and its exact same
    real countdown, matching a real external prediction-market app's own
    fixed :00/:15/:30/:45 boundaries. A no-op if a row for the current
    window already exists (from an earlier poll within the same window).

    Also self-heals the real duplicate-row race documented on
    _ensure_btc_prediction_log_dedupe_and_unique_index above, and treats
    a genuine race caught by that real DB-level constraint as a no-op
    (another concurrent poll already logged this exact window) rather
    than letting it raise."""
    await _ensure_btc_prediction_log_dedupe_and_unique_index()
    product_id = projection["product_id"]
    window_start, window_end = _current_prediction_window()
    result = await db.execute(
        select(PricePredictionLog).where(
            PricePredictionLog.product_id == product_id,
            PricePredictionLog.predicted_at == window_start,
        )
    )
    if result.scalar_one_or_none() is not None:
        return
    db.add(PricePredictionLog(
        product_id=product_id,
        predicted_at=window_start,
        resolve_at=window_end,
        price_at_prediction=projection["current_price"],
        method=projection["method"],
        projected_price=projection["projected_price"],
        band_1sigma_low=projection["band_1sigma_low"],
        band_1sigma_high=projection["band_1sigma_high"],
        band_2sigma_low=projection["band_2sigma_low"],
        band_2sigma_high=projection["band_2sigma_high"],
        resolved=False,
    ))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()


async def _latest_btc_calibration_and_method(db, bpp):
    """Shared by both the projection panel and the live ticker's own
    window-logging: reads the most recent real backtest calibration and
    picks whichever of naive/trend it actually showed winning (naive by
    default, with no real evidence yet). One real function so the two
    endpoints can never disagree about which method is "live" right now."""
    result = await db.execute(
        select(PricePredictionCalibration)
        .where(PricePredictionCalibration.product_id == bpp.PRODUCT_ID)
        .order_by(PricePredictionCalibration.run_at.desc())
        .limit(1)
    )
    latest_calibration = result.scalar_one_or_none()
    method = "naive"
    if latest_calibration and latest_calibration.trend_mae_pct is not None and latest_calibration.naive_mae_pct is not None:
        if latest_calibration.trend_mae_pct < latest_calibration.naive_mae_pct:
            method = "trend"
    return latest_calibration, method


@router.get("/family-tree-status/btc-projection", dependencies=[Depends(require_admin_key)])
async def get_btc_price_projection():
    """Real, live 15-minute-ahead price projection for BTC - per the
    account owner's explicit request: "can we set up a system that can
    predict what the coin will hit in 15 minutes." Purely informational
    (never touches live trading, places no order) per their own explicit
    scoping choice. Reads the most recent real backtest (see the
    /btc-projection/backtest endpoint below) to decide which of the two
    point estimates (naive vs trend) to surface as the headline number -
    whichever real evidence showed was actually more accurate - and
    always returns the calibration numbers alongside the live projection
    so the dashboard never shows a number with no real track record
    attached."""
    if btc_price_projection_module is None:
        raise HTTPException(status_code=500, detail="btc_price_projection module not available")
    bpp = btc_price_projection_module

    async with AsyncSessionLocal() as db:
        latest_calibration, method = await _latest_btc_calibration_and_method(db, bpp)

    async with aiohttp.ClientSession() as session:
        projection = await bpp.get_live_projection(session, method=method)
    if projection is None:
        raise HTTPException(status_code=503, detail="Could not fetch a live BTC price right now - try again")

    projection["calibration"] = latest_calibration.to_dict() if latest_calibration else None

    # Best-effort, real individual prediction-tracking - per the account
    # owner's explicit follow-up: the aggregate calibration above answers
    # "how did this do on past history," this builds the LIVE, ongoing
    # "is it actually hitting, one real prediction at a time" record.
    # Wrapped so a bookkeeping failure here can never break the live
    # panel itself - same defensive pattern _log_activity() already uses
    # elsewhere in this codebase.
    try:
        async with AsyncSessionLocal() as db:
            await _resolve_due_btc_predictions(db, projection["current_price"], bpp.PRODUCT_ID)
            await _log_new_btc_prediction_if_due(db, projection)
    except Exception as e:
        log.warning(f"[btc-projection] prediction-log bookkeeping failed (non-fatal): {e}")

    return projection


@router.get("/family-tree-status/btc-projection/log", dependencies=[Depends(require_admin_key)])
async def get_btc_prediction_log(limit: int = 20):
    """Real, individual prediction-by-prediction track record for the BTC
    15-minute projection - per the account owner's explicit follow-up
    ("I need to know that too... did it hit it or did it [not]"). Read-
    only; resolution itself happens inside get_btc_price_projection()
    above, piggybacked on the dashboard's own live poll.

    Real gap found and fixed: this endpoint never called the dedupe/
    unique-index migration below - only the two endpoints that WRITE a
    new prediction did. If this panel's own poll landed before the
    ticker/projection panel's first poll after a fresh deploy (or before
    either had fired at all), a real pre-existing duplicate could still
    be shown here even though the write-side race that created it was
    already fixed. Calling it here too - it's a cheap no-op once it's
    already run once in this process - closes that gap so this list is
    never stale regardless of poll ordering."""
    if btc_price_projection_module is None:
        raise HTTPException(status_code=500, detail="btc_price_projection module not available")
    product_id = btc_price_projection_module.PRODUCT_ID
    await _ensure_btc_prediction_log_dedupe_and_unique_index()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(PricePredictionLog)
            .where(PricePredictionLog.product_id == product_id)
            .order_by(PricePredictionLog.predicted_at.desc())
            .limit(limit)
        )
        rows = result.scalars().all()

        resolved_result = await db.execute(
            select(PricePredictionLog).where(
                PricePredictionLog.product_id == product_id,
                PricePredictionLog.resolved == True,
            )
        )
        resolved_rows = resolved_result.scalars().all()

    hit_1sigma_count = sum(1 for r in resolved_rows if r.hit_1sigma)
    live_hit_rate_1sigma = round(hit_1sigma_count / len(resolved_rows) * 100, 1) if resolved_rows else None
    # Real, already-computed +/-2sigma hit rate (hit_2sigma is stored on
    # every resolved row by _resolve_due_btc_predictions - this was never
    # surfaced before). Per the account owner's direct request for a
    # higher real hit-rate number: this is the honest way to get one -
    # the wider band was already being computed and shown in the range
    # bar, just never reported as its own real hit-rate percentage. Never
    # a fabricated number - both rates are real, from the same resolved
    # rows, just measuring against two different real widths.
    hit_2sigma_count = sum(1 for r in resolved_rows if r.hit_2sigma)
    live_hit_rate_2sigma = round(hit_2sigma_count / len(resolved_rows) * 100, 1) if resolved_rows else None

    return {
        "predictions": [r.to_dict() for r in rows],
        "resolved_count": len(resolved_rows),
        "live_hit_rate_1sigma": live_hit_rate_1sigma,
        "live_hit_rate_2sigma": live_hit_rate_2sigma,
    }


@router.post("/family-tree-status/btc-projection/log/reset", dependencies=[Depends(require_admin_key)])
async def reset_btc_prediction_log():
    """Per the account owner's explicit request, after spotting a real
    duplicate row in their own "Recent Predictions" list (the exact
    concurrent-poll race documented above - fixed for new rows, but this
    wipes out any stale/duplicate history already sitting in the table
    so the log the account owner is about to rely on for real percentage
    questions is provably clean going forward). Deletes every real
    PricePredictionLog row for BTC-USD - purely a diagnostic log, never
    real trading data or money, and never read by anything that trades.
    A fresh prediction gets logged again on the very next live poll,
    same as any other cold start."""
    if btc_price_projection_module is None:
        raise HTTPException(status_code=500, detail="btc_price_projection module not available")
    product_id = btc_price_projection_module.PRODUCT_ID
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            delete(PricePredictionLog).where(PricePredictionLog.product_id == product_id)
        )
        await db.commit()
    return {"deleted": result.rowcount}


HOURLY_TICKER_WINDOW_MINUTES = 60


async def _get_or_create_hourly_window_anchor(db, product_id: str, live_price: float):
    """Real, persisted 'price to beat' for an hourly ticker window - the
    direct hourly counterpart to the existing 15-minute window bookkeeping
    (_current_prediction_window/_log_new_btc_prediction_if_due), per the
    account owner's explicit request to add a second countdown matching a
    real third-party app's own "Hourly BTC" market. Aligned to the top of
    the current real UTC hour (unlike the 15-minute window, 60 divides an
    hour evenly, so plain hour-flooring is a real, stable, restart-safe
    boundary with no need for the quarter-hour-style modulo trick).
    Returns the real BtcTickerWindowAnchor row for the current hour,
    creating it with `live_price` as the real open price the first time
    it's ever observed - every later call within the same real hour reads
    the same anchor back rather than re-anchoring to whatever the price
    happens to be at that later poll."""
    now = datetime.utcnow()
    window_start = now.replace(minute=0, second=0, microsecond=0)
    window_end = window_start + timedelta(hours=1)
    result = await db.execute(
        select(BtcTickerWindowAnchor).where(
            BtcTickerWindowAnchor.product_id == product_id,
            BtcTickerWindowAnchor.window_minutes == HOURLY_TICKER_WINDOW_MINUTES,
            BtcTickerWindowAnchor.window_start == window_start,
        )
    )
    anchor = result.scalar_one_or_none()
    if anchor is not None:
        return anchor
    anchor = BtcTickerWindowAnchor(
        product_id=product_id, window_minutes=HOURLY_TICKER_WINDOW_MINUTES,
        window_start=window_start, window_end=window_end, open_price=live_price,
    )
    db.add(anchor)
    try:
        await db.commit()
    except IntegrityError:
        # A real concurrent poll already created this same hour's anchor -
        # same race already fixed for the 15-min ledger; just read back
        # whichever row actually won.
        await db.rollback()
        result = await db.execute(
            select(BtcTickerWindowAnchor).where(
                BtcTickerWindowAnchor.product_id == product_id,
                BtcTickerWindowAnchor.window_minutes == HOURLY_TICKER_WINDOW_MINUTES,
                BtcTickerWindowAnchor.window_start == window_start,
            )
        )
        anchor = result.scalar_one_or_none()
    return anchor


@router.get("/family-tree-status/btc-projection/chart", dependencies=[Depends(require_admin_key)])
async def get_btc_price_chart():
    """Real, live BTC ticker + countdown for the dashboard - per the
    account owner's explicit request for "a ticker and a timing... like
    this tracking Bitcoin," and a real follow-up request to have the
    countdown land on the same real wall-clock window a third-party
    prediction-market app's own "15 min Bitcoin" countdown is already
    partway through - "no matter what" they open this dashboard. Windows
    are aligned to real :00/:15/:30/:45 UTC boundaries
    (_current_prediction_window), not to whenever this dashboard happened
    to first get polled - see that function's own docstring for why this
    is the best real match achievable without live access to that other
    app's internal clock. Purely a display feed: a real recent 1-minute
    price history for a live line chart, plus the current active
    prediction window's real price_at_prediction (the "price to beat")
    and resolve_at (the countdown target) - both from the same real
    prediction ledger the panel below already tracks (_log_new_btc_
    prediction_if_due), reused here rather than tracked a second way.
    Read-only, never places an order."""
    if btc_price_projection_module is None:
        raise HTTPException(status_code=500, detail="btc_price_projection module not available")
    bpp = btc_price_projection_module

    async with aiohttp.ClientSession() as session:
        history = await bpp.fetch_recent_1min_candles_with_times(session, product_id=bpp.PRODUCT_ID, minutes=90)
        live_price = await bpp.fetch_live_ticker_price(session, product_id=bpp.PRODUCT_ID)
    if not history:
        raise HTTPException(status_code=503, detail="Could not fetch real BTC price history right now - try again")

    # Per the account owner's explicit request to tighten this closer to
    # Bitcoin's real-time price: anchor "current" on the real-time ticker
    # trade price (sub-second, not bucketed into a 1-minute candle) when
    # it's available, only falling back to the last candle close if that
    # extra fetch failed - never blocks the chart on this one non-essential
    # precision improvement.
    current_price = live_price if live_price is not None else history[-1]["price"]

    # Best-effort: make sure a row for the CURRENT real wall-clock window
    # exists, so the countdown is accurate even if the projection panel
    # below hasn't polled yet since this window opened. Never fatal to the
    # chart itself - same defensive pattern the projection endpoint uses
    # for this same ledger.
    try:
        async with AsyncSessionLocal() as db:
            _, method = await _latest_btc_calibration_and_method(db, bpp)
            proj = bpp._compute_projection([h["price"] for h in history], live_price=live_price)
            proj["product_id"] = bpp.PRODUCT_ID
            proj["method"] = method
            proj["projected_price"] = proj["trend_price"] if method == "trend" else proj["naive_price"]
            await _log_new_btc_prediction_if_due(db, proj)
    except Exception as e:
        log.warning(f"[btc-projection/chart] window bookkeeping failed (non-fatal): {e}")

    # Second, longer real countdown - an hourly "price to beat," per the
    # account owner's explicit request to match a real third-party app's
    # own "Hourly BTC" market alongside the existing 15-minute one. Kept
    # completely independent of the 15-minute ledger above - its own
    # anchor table, its own real wall-clock alignment (top of hour).
    hourly_price_to_beat = None
    hourly_resolve_at = None
    hourly_seconds_remaining = 0
    try:
        async with AsyncSessionLocal() as db:
            hourly_anchor = await _get_or_create_hourly_window_anchor(db, bpp.PRODUCT_ID, current_price)
            if hourly_anchor is not None:
                hourly_price_to_beat = hourly_anchor.open_price
                hourly_resolve_at = hourly_anchor.window_end.isoformat() + "Z"
                hourly_seconds_remaining = max(0, int((hourly_anchor.window_end - datetime.utcnow()).total_seconds()))
    except Exception as e:
        log.warning(f"[btc-projection/chart] hourly window bookkeeping failed (non-fatal): {e}")

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(PricePredictionLog)
            .where(PricePredictionLog.product_id == bpp.PRODUCT_ID)
            .order_by(PricePredictionLog.predicted_at.desc())
            .limit(1)
        )
        active = result.scalar_one_or_none()

    if active is not None:
        price_to_beat = active.price_at_prediction
        resolve_at = active.resolve_at.isoformat() + "Z"
        seconds_remaining = max(0, int((active.resolve_at - datetime.utcnow()).total_seconds()))
    else:
        # No real prediction window has ever been logged yet (e.g. right
        # after a fresh deploy) - fall back to the current price with a
        # real, honest zero countdown rather than fabricating a window.
        price_to_beat = current_price
        resolve_at = None
        seconds_remaining = 0

    pct_change = round((current_price - price_to_beat) / price_to_beat * 100, 4) if price_to_beat else 0.0

    hourly_pct_change = (
        round((current_price - hourly_price_to_beat) / hourly_price_to_beat * 100, 4)
        if hourly_price_to_beat else None
    )

    return {
        "product_id": bpp.PRODUCT_ID,
        "current_price": current_price,
        "price_to_beat": price_to_beat,
        "pct_change_vs_price_to_beat": pct_change,
        "resolve_at": resolve_at,
        "seconds_remaining": seconds_remaining,
        "hourly_price_to_beat": hourly_price_to_beat,
        "hourly_pct_change_vs_price_to_beat": hourly_pct_change,
        "hourly_resolve_at": hourly_resolve_at,
        "hourly_seconds_remaining": hourly_seconds_remaining,
        "history": history,
    }


@router.post("/family-tree-status/btc-projection/backtest", dependencies=[Depends(require_admin_key)])
async def run_btc_price_projection_backtest(days: float = 3.0):
    """SHADOW-MODE - never touches live trading, places no order. Runs a
    real backtest of the 15-minute-ahead projection above against real
    historical Coinbase 1-minute candles, persists the result (so the
    live panel always has a real track record to show), and returns it.
    Pulls ~days*1440 real 1-minute candles in paginated real API calls -
    can take a real ~10-40 seconds depending on the window."""
    if btc_price_projection_module is None:
        raise HTTPException(status_code=500, detail="btc_price_projection module not available")
    result = await btc_price_projection_module.run_price_projection_backtest(days=days)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])

    async with AsyncSessionLocal() as db:
        row = PricePredictionCalibration(
            product_id=result["product_id"],
            window_days=result["window_days"],
            num_samples=result["num_samples"],
            naive_mae_pct=result["naive_mae_pct"],
            trend_mae_pct=result["trend_mae_pct"],
            pct_within_1sigma=result["pct_within_1sigma"],
            pct_within_2sigma=result["pct_within_2sigma"],
        )
        db.add(row)
        await db.commit()

    return result


@router.post("/family-tree-status/btc-projection/directional-backtest", dependencies=[Depends(require_admin_key)])
async def run_btc_directional_signal_backtest(days: float = 3.0):
    """SHADOW-MODE - never touches live trading, places no order, and this
    result is never read by anything that trades or bets. Built as the
    real, validated alternative offered (and accepted) in place of turning
    the BTC prediction panel into a real-money betting-confidence
    mechanism - a real test of whether any simple signal predicts BTC's
    real 15-minute DIRECTION better than a real 50/50 coin flip, on real
    historical Coinbase 1-minute candles. Not persisted (unlike the
    price-level backtest above) - this is a one-off diagnostic run on
    demand, not something the live panel's calibration depends on."""
    if btc_price_projection_module is None:
        raise HTTPException(status_code=500, detail="btc_price_projection module not available")
    result = await btc_price_projection_module.run_directional_signal_backtest(days=days)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


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


class RootPartialSellRequest(BaseModel):
    amount_usd: float


@router.post("/family-tree-status/root-partial-sell", dependencies=[Depends(require_admin_key)])
async def root_partial_sell_endpoint(payload: RootPartialSellRequest):
    """Sells a specific real dollar amount out of root's BTC-USD position,
    leaving the rest untouched - per the account owner's own explicit,
    informed choice (weighed directly against the alternative of a new
    deposit or waiting for real profit) to fund new Grid Bot branches from
    part of the consolidated BTC position rather than new cash. This is a
    real, deliberate reopening of root's manual-sell path specifically for
    a PARTIAL amount - the existing close endpoint only ever sold root's
    ENTIRE position. Per the account owner's own direct follow-up ("don't
    let me sale at a loss ever"), this is refused server-side (a real,
    fee-aware check, not just a UI warning) whenever it would realize a
    real loss. See crypto_family_tree_bot.root_partial_sell() for the
    full real mechanics (real fee-adjusted proceeds, real trade-history
    record, never strands unsellable dust)."""
    if crypto_family_tree_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_family_tree_bot module not available")
    try:
        return await crypto_family_tree_bot_module.root_partial_sell(payload.amount_usd)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


async def _place_buy_with_retry(engine, session, amount: float, product_id: str, attempts: int = 3):
    """Retries a manual real market buy through a transient real-balance
    race, before surfacing a raw rejection to a human waiting on a click.

    place_market_buy() already clamps to the real Coinbase USD balance
    right before submitting, but its own docstring is explicit that this
    "doesn't eliminate the race outright (two branches could still both
    clamp against the real balance before either order lands)" - real,
    confirmed live evidence of exactly this: a manual "Move cash" from a
    real, genuinely-flat LINK branch ($98.99 idle, matching its own
    allocated_usd) into BTC failed with a raw real INSUFFICIENT_FUND,
    because one of the ~20+ other branches' own independent ~30s cycles
    spent the real shared cash pool in the gap between this call's
    balance-fetch and the order actually landing at Coinbase. The
    automatic per-cycle paths already tolerate this by just waiting for
    their own next cycle; a one-off manual dashboard click has no "next
    cycle" to fall back on, so it deserves a couple of real retries
    first instead of immediately failing the person's click.

    Never retries a PERMANENT rejection (PERMISSION_DENIED, invalid
    product, unsupported order config, via engine's own
    _is_permanent_order_rejection) - retrying an identical doomed order
    can never fix those, so it fails fast on the first attempt instead
    of burning real API calls and the user's time.

    Returns (fill, None) on success, or (None, last_real_reason) once
    every attempt is exhausted or a permanent rejection is hit."""
    last_reason = "unknown reason"
    for attempt in range(attempts):
        fill = await engine.place_market_buy(session, amount, product_id)
        if fill:
            return fill, None
        last_reason = engine._last_order_error.get(product_id, "unknown reason")
        if engine._is_permanent_order_rejection(last_reason):
            break
        if attempt < attempts - 1:
            await asyncio.sleep(random.uniform(0.4, 1.2))
    return None, last_reason


class AddCashRequest(BaseModel):
    amount: float


@router.post("/family-tree-status/add-cash/{bot_name}", dependencies=[Depends(require_admin_key)])
async def add_cash_to_branch(bot_name: str, payload: AddCashRequest, db: AsyncSession = Depends(get_db)):
    """Manually deploys real, currently-unallocated cash directly into ANY
    branch's position right now - originally built scoped to root only
    (BTC can never have a sibling branch, unlike every other coin where
    "Trade this" on an already-held coin effectively adds capital by
    starting a second branch on it), then opened up to every branch per
    the account owner's explicit follow-up ("put that add cash button on
    all of them why not it won't hurt it's up to me to use it or not") -
    the underlying real buy/blend/recompute logic never actually depended
    on being root, so this generalizes cleanly.

    Places a real market buy for the requested amount via the exact same
    engine.place_market_buy() every automatic entry already uses, then
    blends it into the branch's existing position with a real
    quantity-weighted average entry price (or opens a fresh position if
    the branch happens to be flat), and recomputes target/stop off that
    new blended entry using the same real ATR-based formula every fresh
    buy already uses - so the breakeven ratchet and peak-profit giveback
    tracking stay correctly anchored to the branch's true cost basis
    afterward, not a stale pre-add-cash entry.

    Refused (400) if the amount isn't positive, if bot_name doesn't exist,
    or if the amount exceeds the real free spendable cash currently
    sitting outside every branch's own allocated balance - the same real
    "spendable_for_spawn" figure the dashboard's "Start new $50 branch"
    button is gated on, computed the same way here (real Coinbase cash
    balance, minus locked profit, minus every FLAT branch's own
    allocated_usd, INCLUDING this branch's own if it's currently flat) -
    so this can only ever deploy real money that isn't already working
    somewhere else in the tree."""
    if crypto_family_tree_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_family_tree_bot module not available")
    if os.getenv("STOP_TRADING", "false").lower() == "true":
        raise HTTPException(status_code=400, detail="STOP_TRADING is set - new capital deployment is paused")
    tree = crypto_family_tree_bot_module
    engine = tree.engine

    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")

    branch_result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == bot_name))
    branch = branch_result.scalar_one_or_none()
    if branch is None:
        raise HTTPException(status_code=404, detail=f"No branch named {bot_name}")

    async with engine.aiohttp.ClientSession() as session:
        real_balance, err = await engine.get_usd_balance(session)
        if real_balance is None:
            raise HTTPException(status_code=503, detail=f"Could not fetch real Coinbase cash balance right now ({err})")

        locked_usd = await tree.get_locked_usd()
        all_branches_result = await db.execute(select(CryptoTreeBranch))
        all_branches = all_branches_result.scalars().all()
        open_bots_result = await db.execute(select(BotPosition.bot))
        open_bots = {row[0] for row in open_bots_result.all()}
        flat_allocated_sum = sum(b.allocated_usd for b in all_branches if b.bot_name not in open_bots)
        spendable = max(0.0, real_balance - locked_usd - flat_allocated_sum)

        if payload.amount > spendable + 0.005:
            raise HTTPException(
                status_code=400,
                detail=f"Only ${spendable:.2f} in real free spendable cash right now - can't deploy ${payload.amount:.2f}",
            )

        price, atr_pct = await engine.get_price_and_volatility(session, branch.product_id)
        if price is None or atr_pct is None:
            raise HTTPException(status_code=503, detail=f"Could not fetch a live {branch.product_id} price/volatility right now - try again")

        fill, stuck_reason = await _place_buy_with_retry(engine, session, payload.amount, branch.product_id)
        if not fill:
            raise HTTPException(status_code=502, detail=f"Real Coinbase order did not fill after retrying: {stuck_reason}")
        filled_qty, filled_price = fill

        existing_position = await tree._load_branch_position(bot_name)
        if existing_position is not None:
            new_qty = existing_position.qty + filled_qty
            blended_entry = (
                existing_position.qty * existing_position.entry_price + filled_qty * filled_price
            ) / new_qty
        else:
            new_qty = filled_qty
            blended_entry = filled_price

        position_dollar_size = new_qty * blended_entry
        target_pct = max(engine.pick_target_pct(atr_pct), engine.min_profit_target_pct(position_dollar_size, atr_pct))
        target_price = blended_entry * (1 + target_pct)
        stop_price = blended_entry * (1 - tree.STOP_LOSS_PCT)
        await tree._save_branch_position(bot_name, branch.product_id, blended_entry, new_qty, target_price, stop_price)

    branch.allocated_usd += payload.amount
    await db.commit()

    add_cash_msg = (
        f"💰 Manually added ${payload.amount:.2f} real cash to {bot_name}'s {branch.product_id} position - "
        f"bought {filled_qty:.8f} @ ${filled_price:,.2f}, blended entry now ${blended_entry:,.2f}, "
        f"branch total now ${branch.allocated_usd:.2f}"
    )
    log.info(f"[dashboard] {add_cash_msg}")
    await tree._log_activity(bot_name, branch.product_id, "BUY", add_cash_msg)

    # Settle immediately if this deposit pushed the branch over its own
    # next spawn tier, instead of leaving it sitting at 100% for up to
    # ~30s until its next scheduled cycle picks it up - the automatic
    # per-cycle sale path already does this same immediate check
    # (_branch_sell_and_settle calls _maybe_spawn_child in the same call
    # a sale crosses the tier), this was the one real gap: a manual
    # cash deposit crossing the tier had no equivalent trigger.
    await tree._maybe_spawn_child(branch)
    return {
        "status": "cash_added",
        "bot_name": bot_name,
        "product_id": branch.product_id,
        "amount_deployed": round(payload.amount, 2),
        "filled_qty": filled_qty,
        "filled_price": round(filled_price, 2),
        "new_entry_price": round(blended_entry, 2),
        "new_qty": new_qty,
        "branch_new_balance": round(branch.allocated_usd, 2),
    }


class ReallocateCashRequest(BaseModel):
    from_bot_name: str
    to_bot_name: str
    amount: float


@router.post("/family-tree-status/reallocate-cash", dependencies=[Depends(require_admin_key)])
async def reallocate_cash_between_branches(payload: ReallocateCashRequest, db: AsyncSession = Depends(get_db)):
    """Moves real, already-bookkept cash directly from one FLAT branch's
    allocated_usd into another branch's position - built after a real
    "Add cash failed: Only $0.31 in real free spendable cash right now"
    error exposed that almost all of the account's real cash was already
    reserved for two flat branches (POL/SOL) with nothing currently
    deployed in them, even though the real Coinbase account had plenty of
    genuinely free cash sitting there. add_cash_to_branch() only ever
    draws from spendable_for_spawn (real cash NOT already reserved by any
    flat branch) - it can never touch a flat branch's own reserved
    allocation, so there was no existing way to actually put an idle
    branch's real dollars back to work in a DIFFERENT branch without first
    manually waiting for/forcing that branch's own spawn cycle.

    Source branch MUST be flat (no open BotPosition) - refused (400)
    otherwise, since pulling allocated_usd out from under a branch that's
    actively holding a real position would leave its own bookkeeping
    (and the DB-vs-Coinbase reconciliation panel) out of sync with what's
    genuinely deployed. Refused if the amount isn't positive, exceeds the
    source's own real allocated_usd, or if either bot_name doesn't exist
    or they're the same branch.

    Places a real market buy for the amount into the DESTINATION branch,
    via the exact same real buy/blend/target-stop-recompute logic
    add_cash_to_branch() already uses (quantity-weighted blended entry,
    ATR-based target/stop recompute) - reused directly here rather than
    duplicated. If the real buy fails to fill, the source's allocated_usd
    is left completely untouched (the deduction only happens after a
    confirmed fill) - no real dollars are ever debited from the source
    without a confirmed destination fill to show for it."""
    if crypto_family_tree_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_family_tree_bot module not available")
    if os.getenv("STOP_TRADING", "false").lower() == "true":
        raise HTTPException(status_code=400, detail="STOP_TRADING is set - new capital deployment is paused")
    tree = crypto_family_tree_bot_module
    engine = tree.engine

    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")
    if payload.from_bot_name == payload.to_bot_name:
        raise HTTPException(status_code=400, detail="source and destination branches must be different")

    source_result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == payload.from_bot_name))
    source = source_result.scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail=f"No branch named {payload.from_bot_name}")

    dest_result = await db.execute(select(CryptoTreeBranch).where(CryptoTreeBranch.bot_name == payload.to_bot_name))
    dest = dest_result.scalar_one_or_none()
    if dest is None:
        raise HTTPException(status_code=404, detail=f"No branch named {payload.to_bot_name}")

    source_position = await tree._load_branch_position(payload.from_bot_name)
    if source_position is not None:
        raise HTTPException(
            status_code=400,
            detail=f"{payload.from_bot_name} is currently holding a position on {source.product_id} - "
                   f"only a FLAT branch's reserved cash can be reallocated",
        )

    if payload.amount > source.allocated_usd + 0.005:
        raise HTTPException(
            status_code=400,
            detail=f"{payload.from_bot_name} only has ${source.allocated_usd:.2f} of its own real allocated cash - can't move ${payload.amount:.2f}",
        )

    async with engine.aiohttp.ClientSession() as session:
        price, atr_pct = await engine.get_price_and_volatility(session, dest.product_id)
        if price is None or atr_pct is None:
            raise HTTPException(status_code=503, detail=f"Could not fetch a live {dest.product_id} price/volatility right now - try again")

        fill, stuck_reason = await _place_buy_with_retry(engine, session, payload.amount, dest.product_id)
        if not fill:
            raise HTTPException(status_code=502, detail=f"Real Coinbase order did not fill after retrying: {stuck_reason}")
        filled_qty, filled_price = fill

        existing_position = await tree._load_branch_position(payload.to_bot_name)
        if existing_position is not None:
            new_qty = existing_position.qty + filled_qty
            blended_entry = (
                existing_position.qty * existing_position.entry_price + filled_qty * filled_price
            ) / new_qty
        else:
            new_qty = filled_qty
            blended_entry = filled_price

        position_dollar_size = new_qty * blended_entry
        target_pct = max(engine.pick_target_pct(atr_pct), engine.min_profit_target_pct(position_dollar_size, atr_pct))
        target_price = blended_entry * (1 + target_pct)
        stop_price = blended_entry * (1 - tree.STOP_LOSS_PCT)
        await tree._save_branch_position(payload.to_bot_name, dest.product_id, blended_entry, new_qty, target_price, stop_price)

    source.allocated_usd -= payload.amount
    dest.allocated_usd += payload.amount
    await db.commit()

    # Two real rows for one real action - a REALLOCATE on the source branch
    # and a BUY on the destination - deliberately worded from each
    # branch's own perspective rather than sharing one identical string.
    # Found live: the Activity feed showed two byte-identical lines back
    # to back for a single real reallocate-cash click, which reads
    # exactly like an accidental duplicate log entry even though both
    # rows are real and correctly tied to their own bot_name.
    log.info(
        f"[dashboard] 🔀 Manually moved ${payload.amount:.2f} real cash from {payload.from_bot_name} "
        f"(now ${source.allocated_usd:.2f}) into {payload.to_bot_name}'s {dest.product_id} position - "
        f"bought {filled_qty:.8f} @ ${filled_price:,.2f}, blended entry now ${blended_entry:,.2f}, "
        f"branch total now ${dest.allocated_usd:.2f}"
    )
    dest_msg = (
        f"🔀 Received ${payload.amount:.2f} manually reallocated cash from {payload.from_bot_name} - "
        f"bought {filled_qty:.8f} {dest.product_id} @ ${filled_price:,.2f}, blended entry now ${blended_entry:,.2f}, "
        f"branch total now ${dest.allocated_usd:.2f}"
    )
    source_msg = (
        f"🔀 Manually moved ${payload.amount:.2f} of its own idle real cash to {payload.to_bot_name} "
        f"({dest.product_id}) - branch total now ${source.allocated_usd:.2f}"
    )
    await tree._log_activity(payload.to_bot_name, dest.product_id, "BUY", dest_msg)
    await tree._log_activity(payload.from_bot_name, source.product_id, "REALLOCATE", source_msg)

    await tree._maybe_spawn_child(dest)
    return {
        "status": "cash_reallocated",
        "from_bot_name": payload.from_bot_name,
        "from_new_balance": round(source.allocated_usd, 2),
        "to_bot_name": payload.to_bot_name,
        "product_id": dest.product_id,
        "amount_moved": round(payload.amount, 2),
        "filled_qty": filled_qty,
        "filled_price": round(filled_price, 2),
        "new_entry_price": round(blended_entry, 2),
        "new_qty": new_qty,
        "to_new_balance": round(dest.allocated_usd, 2),
    }


@router.post("/family-tree-status/consolidate-branches", dependencies=[Depends(require_admin_key)])
async def consolidate_family_tree_branches(dry_run: bool = True):
    """Merges every real branch sharing the same coin into one - per the
    account owner's explicit request, after the shared-coin-branches
    feature let up to 15 real branches pile onto POL-USD in a single spawn
    storm, each independently tracking its own qty against one POOLED real
    Coinbase balance (the exact structural gap behind both the phantom-
    position self-heal and the DB-vs-Coinbase reconciliation SHORTFALLs
    found earlier this session). See
    crypto_family_tree_bot.consolidate_branches_by_coin() for the real
    merge math (summed allocated_usd, max next_unlock_tier, floor
    recomputed from the new combined balance, quantity-weighted blended
    entry, target/stop recomputed off it).

    dry_run=true (the default - always call this way first) computes and
    returns the full real plan without touching the database or placing
    any order. Only call with dry_run=false once you've reviewed the plan
    and want to actually execute it - that pass deletes the merged-away
    branches for real and can't be undone by calling this endpoint again."""
    if crypto_family_tree_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_family_tree_bot module not available")
    return await crypto_family_tree_bot_module.consolidate_branches_by_coin(dry_run=dry_run)


@router.post("/family-tree-status/reconcile-asset/{currency}", dependencies=[Depends(require_admin_key)])
async def reconcile_asset(currency: str, dry_run: bool = True):
    """Corrects a real SHORTFALL the Reconciliation panel flags - every
    real branch's tracked qty for this currency, summed, exceeds what
    Coinbase's own real account currently shows. See
    crypto_family_tree_bot.reconcile_asset_to_real_balance() for the real
    math (Coinbase's real balance is ground truth; the deficit is
    distributed proportionally across every branch tracking this currency,
    correcting only BotPosition.qty - allocated_usd, entry_price, target,
    and stop are all left untouched, and no Coinbase order is ever placed).

    dry_run=true (the default - always call this way first) computes and
    returns the real plan without touching the database. Only call with
    dry_run=false once you've reviewed it and want to actually apply the
    correction."""
    if crypto_family_tree_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_family_tree_bot module not available")
    return await crypto_family_tree_bot_module.reconcile_asset_to_real_balance(currency.upper(), dry_run=dry_run)


@router.post("/family-tree-status/liquidate-and-buy-btc", dependencies=[Depends(require_admin_key)])
async def liquidate_family_tree_and_buy_btc():
    """Per the account owner's explicit, real decision - the crypto-side
    counterpart to the Alpaca liquidate-and-buy-SPY action: retires the
    ENTIRE family tree and consolidates everything into one real
    buy-and-hold BTC position on the permanent root branch.

    A REAL, ONE-WAY action: sells every real position held by every
    non-root branch at market, records each fill in the real per-coin
    trade history, deletes every non-root branch row, permanently retires
    the whole tree (is_crypto_passive_mode() - every branch thread, root
    included, stops doing anything at all: no entries, no exits, no
    spawns, no reinforcement), then buys real BTC with the real freed
    cash and blends it into root's existing position. See
    crypto_family_tree_bot.liquidate_family_tree_and_buy_btc() for the
    full real mechanics.

    Root's own existing "can never be manually sold" protection is lifted
    once this runs - there's no tree left to protect, so the account
    owner can always sell the resulting real BTC holding by hand
    afterward via the normal close endpoint."""
    if crypto_family_tree_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_family_tree_bot module not available")
    return await crypto_family_tree_bot_module.liquidate_family_tree_and_buy_btc()


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
    #
    # Lifted once the tree has been retired into a real buy-and-hold BTC
    # position (see liquidate_family_tree_and_buy_btc) - "stays root,
    # never manually sold" existed to protect the tree's permanent
    # foundation while it was actively growing; once retired, there's no
    # tree left to protect, and the account owner must always be able to
    # sell their own real holding by hand, same principle the Alpaca-side
    # SPY retirement already uses for manual close.
    if bot_name == crypto_family_tree_bot_module.ROOT_BOT_NAME and not await crypto_family_tree_bot_module.is_crypto_passive_mode():
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
    if os.getenv("STOP_TRADING", "false").lower() == "true":
        raise HTTPException(status_code=400, detail="STOP_TRADING is set - new capital deployment is paused")

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
    if os.getenv("STOP_TRADING", "false").lower() == "true":
        raise HTTPException(status_code=400, detail="STOP_TRADING is set - new capital deployment is paused")

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


class SetManualCoinOverrideRequest(BaseModel):
    product_id: str
    excluded: bool


@router.post("/family-tree-status/coin-manual-override", dependencies=[Depends(require_admin_key)])
async def set_family_tree_manual_coin_override(payload: SetManualCoinOverrideRequest):
    """Real, dashboard-driven toggle of one coin's manual-exclusion status
    - per the account owner's explicit complaint that the live watchlist's
    "Manual" status badge just sat there with no way to actually press it
    and change it, forcing a code change and a redeploy to touch manual
    exclusion at all. `excluded=True` adds the coin to the effective
    manual-exclusion set (subject to the same real self-heal rule every
    other manually-excluded coin already uses - never a one-way verdict);
    `excluded=False` is an explicit decision to pull it back out right
    now, even one that's in the hardcoded starting list, without waiting
    on the same heal bar. See CryptoManualCoinOverride's own docstring for
    the full real semantics. Never places an order or force-sells an
    existing position - this only ever changes which coin a FUTURE spawn/
    reinforcement/coin-switch is allowed to pick."""
    if crypto_family_tree_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_family_tree_bot module not available")
    tree = crypto_family_tree_bot_module
    try:
        await tree.set_manual_coin_override(payload.product_id, payload.excluded)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    action = "Manually excluded" if payload.excluded else "Manually un-excluded"
    log.info(f"[dashboard] 🔀 {action} {payload.product_id} via the live watchlist")
    await tree._log_activity("dashboard", payload.product_id, "MANUAL_OVERRIDE", f"{action} {payload.product_id} from the live watchlist")

    reasons = await tree.get_effective_excluded_coins_with_reasons()
    return {
        "status": "updated",
        "product_id": payload.product_id,
        "excluded": payload.product_id in reasons,
        "exclusion_reason": reasons.get(payload.product_id),
    }


@router.get("/family-tree-status/reconciliation", dependencies=[Depends(require_admin_key)])
async def family_tree_reconciliation():
    """Real DB-vs-Coinbase reconciliation, per the account owner's direct
    request after seeing real branches get permanently stuck retrying an
    impossible sell (real balance 0.00000000 against a tracked position
    that said otherwise) - "22 branches holding positions" on its own
    proves nothing about what Coinbase actually has right now. Grouped by
    asset (not per-branch) since branches can legitimately share a coin
    and Coinbase's real balance for it is pooled - see
    crypto_family_tree_bot.get_reconciliation_report() for the full
    reasoning. Read-only, never places an order."""
    if crypto_family_tree_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_family_tree_bot module not available")
    return await crypto_family_tree_bot_module.get_reconciliation_report()


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


@router.post("/crypto-selection-backtest/real-allocations", dependencies=[Depends(require_admin_key)])
async def run_crypto_selection_backtest_real_allocations():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders.
    Per the account owner's explicit request: the main backtest above
    deliberately spends a flat $150 on every coin so they're comparable
    by quality alone - but that doesn't reflect what your REAL money
    would have done, since the real tree has very uneven real balances
    per branch ($881.76 on BTC, $797.66 on POL, $49.58 on SOL, not an
    equal $150 each). Runs the identical real target/stop/breakeven/
    giveback replay, but simulates each coin's REAL current branch dollar
    amount (summed across every branch holding it) instead of the flat
    default - a coin with no real allocation right now still gets tested,
    falling back to the same $150 default so the table stays complete.

    Pulls real historical data from Coinbase's public candles endpoint -
    can take 30-90 seconds depending on that endpoint's response time."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_full_backtest_with_real_allocations()


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


@router.post("/crypto-selection-backtest/combined-live-entry-filters", dependencies=[Depends(require_admin_key)])
async def run_combined_live_entry_filters_backtest_endpoint():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders.
    Direct answer to the account owner's own "do a backtest" request
    after seeing the real Coin Trade History table all red - would the
    family tree, with EVERY entry filter currently wired live
    (RSI-overbought, BTC-relative-strength, higher-timeframe trend, and
    the RSI(30)+support-zone entry-timing filter) applied TOGETHER,
    actually have made money over the real last 30 days - the real
    evidence needed before deciding whether to un-retire it. Each of
    these four filters has already been individually backtested and
    promoted to live; none of the existing backtest tools test what they
    do stacked together, which is how the live bot genuinely runs today.
    See crypto_selection_backtest.py's
    run_combined_live_entry_filters_backtest for the full real
    methodology and its one honest scope note (per-coin entry timing
    only, not coin selection).

    Pulls real historical data from Coinbase's public candles endpoint,
    plus one extra fetch for BTC-USD's own history - can take 30-90
    seconds depending on that endpoint's response time."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_combined_live_entry_filters_backtest()


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


@router.post("/crypto-selection-backtest/support-resistance", dependencies=[Depends(require_admin_key)])
async def run_support_resistance_backtest():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders,
    and no bot reads this result yet. Tests the account owner's own real
    proposal directly: RSI 70/30 on the 1hr chart, plus real support/
    resistance structure, to see whether it actually "boost[s] the
    accuracy" as claimed - rather than assuming it. Runs the exact same
    real target/stop/breakeven/trailing-stop replay as
    /crypto-selection-backtest, twice per coin, on the exact same real
    historical hourly candles: once with no entry filter (baseline) and
    once gated by real RSI(30, oversold) plus proximity to a real recent
    support zone (a previous low / previous breakdown level), so the two
    are directly comparable. Does not change what the live bot buys
    unless/until wired into the live selection path separately, on
    purpose - this is a read-only comparison report.

    Pulls real historical data from Coinbase's public candles endpoint -
    can take 30-90 seconds depending on that endpoint's response time."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_support_resistance_comparison()


@router.post("/crypto-selection-backtest/quick-profit-vs-trailing-stop", dependencies=[Depends(require_admin_key)])
async def run_quick_profit_vs_trailing_stop_backtest():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders.
    Built after a pasted proposal argued for letting winners run behind a
    percentage trailing stop, which directly conflicts with the real,
    live QUICK_PROFIT rule (crypto_family_tree_bot.py's run_branch_cycle)
    shipped earlier this same session at the account owner's own explicit
    request - take any real profit the instant it clears fees, never wait.
    Rather than guess which is actually better, this replays BOTH real
    exit philosophies against the exact same real historical candles for
    every coin: does QUICK_PROFIT's snap-it-fast approach make more real
    money, or does letting a winner run behind a real trailing stop once
    it reaches the same ATR-based target capture more of a sustained real
    move? Does not change what the live bot does unless/until the account
    owner sees these real numbers and explicitly decides to wire a change
    into the live path - this is a read-only comparison report.

    Pulls real historical data from Coinbase's public candles endpoint -
    can take 30-90 seconds depending on that endpoint's response time."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_quick_profit_vs_trailing_stop_comparison()


@router.post("/crypto-selection-backtest/partial-exit-vs-full-trail", dependencies=[Depends(require_admin_key)])
async def run_partial_exit_vs_full_trail_backtest():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders.
    Tests the account owner's own real proposal directly: "take most of
    your profits... take partials... and trailing the stop" - does
    selling a real partial of the position at the first ATR-based target
    (and only trailing the real remainder) actually make more money than
    the live rule, which trails the WHOLE position and exits it in one
    piece? Runs BOTH real exit philosophies against the exact same real
    historical candles for every coin - same real entry, hard stop,
    breakeven ratchet, and fee on every leg - so the comparison is fair.
    Does not change what the live bot does unless/until the account owner
    sees these real numbers and explicitly decides to wire a change into
    the live path - this is a read-only comparison report.

    Pulls real historical data from Coinbase's public candles endpoint -
    can take 30-90 seconds depending on that endpoint's response time."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_partial_exit_vs_full_trail_comparison()


@router.post("/crypto-selection-backtest/narrow-range-breakout", dependencies=[Depends(require_admin_key)])
async def run_narrow_range_breakout_backtest_endpoint():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders.
    Tests the account owner's own real trading claim directly: "the best
    opportunity come from a narrow state... if you open above a narrow
    state... 87% chance there are more upside to come... if you open
    below a narrow state... 87% chance to follow through to the
    downside." That 87% figure is their own stated number, not something
    already verified against this system's real data - this replays real
    historical Coinbase hourly candles looking for genuine narrow-range
    states (percentile-relative to each coin's own recent range history,
    not one fixed threshold) and reports the REAL hit rate the actual
    first breakout candle's follow-through produced, split by direction,
    against an honest 50% coin-flip baseline.

    Pulls real historical data from Coinbase's public candles endpoint -
    can take 30-90 seconds depending on that endpoint's response time."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_narrow_range_breakout_backtest()


@router.post("/crypto-selection-backtest/opening-bar-breakout", dependencies=[Depends(require_admin_key)])
async def run_opening_bar_breakout_backtest_endpoint():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders.
    Tests the account owner's own fully-specified real trading system,
    described directly: narrow-state coins, a real "Elephant Bar"
    (oversized green candle) or "bottoming Tail" (long lower-wick
    rejection) as the real first bar of the session, entry the instant
    the second bar's real price crosses bar 1's high + $0.01 (never
    waiting for bar 2 to close), a real stop at bar 1's own low, and a
    real exit once a second "push" (a new high after a genuine pullback)
    confirms. Crypto has no real discrete session open the way stocks
    do - uses 13:30 UTC (the real US stock market's own open) as an
    explicitly invented stand-in, per the account owner's own "do it for
    crypto too."

    Pulls real, paginated 1-minute Coinbase candles (aggregated into
    synthetic real 2-minute bars) over a deliberately short 5-day window
    - can take 60-180 seconds given the real 1-minute data volume."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_opening_bar_breakout_backtest()


@router.post("/crypto-selection-backtest/opening-bar-narrow-state-comparison", dependencies=[Depends(require_admin_key)])
async def run_opening_bar_narrow_state_comparison_endpoint():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders.
    Compares three real narrow-state definitions against the IDENTICAL
    real Elephant/Tail opening-bar trades above: no gate (baseline), the
    existing percentile-range method, and the account owner's own
    newly-described real 20/200 SMA-convergence method - transcribed
    directly: "moving averages far apart is a wide state... a tight
    narrow state [is] the 20 a little below the 200." Never places a
    real order."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_opening_bar_narrow_state_comparison()


@router.post("/crypto-selection-backtest/wide-state-contrarian", dependencies=[Depends(require_admin_key)])
async def run_wide_state_contrarian_backtest_endpoint():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders.
    The account owner's own SEPARATE real trading idea from the
    Elephant/Tail breakout-continuation system above: "you become a
    contrarian trader in the wide state... the drop brings you back to
    narrow." Real, honest scope note: only the wide_down -> LONG leg is
    executable by the live bot today (long-only in production); the
    wide_up -> SHORT leg is reported as pure diagnostic information,
    clearly labeled - neither live bot can actually short today."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_wide_state_contrarian_backtest()


@router.post("/crypto-selection-backtest/opening-bar-short-side", dependencies=[Depends(require_admin_key)])
async def run_opening_bar_short_side_backtest_endpoint():
    """SHADOW-MODE ONLY, DIAGNOSTIC ONLY - does not touch live trading,
    places no orders. The real bearish mirror of the live Elephant/Tail
    breakout system above, transcribed directly: "opens below with a
    red elephant or opens below with one of the topping tail bars...
    these bars below and these bars above the narrow state." Real RED
    Elephant Bar or topping Tail bar breaking below a narrow state, a
    real SHORT entry the instant bar 2's price crosses bar 1's low minus
    $0.01, a real stop at bar 1's own high, a real exit on a second
    downside push. Never places a real order - and never could: the
    crypto side has no real short-selling mechanism at all today."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_opening_bar_short_side_backtest()


@router.post("/crypto-selection-backtest/scaled-entry-comparison", dependencies=[Depends(require_admin_key)])
async def run_scaled_entry_comparison_backtest_endpoint():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders.
    The account owner's own real scaling-in mechanic, transcribed
    directly: "you want to go in and then in and then in... usually two
    adds... let me put half in... that add arrow is one penny above the
    high of a single red bar." Replays the IDENTICAL real qualifying
    Elephant/Tail setups two ways on the same real data - the existing
    single-shot entry vs. a real half-in-then-two-adds scaling mechanic
    - so "does scaling in actually help" gets a real, direct answer.
    Never places a real order."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_scaled_entry_comparison_backtest()


@router.post("/crypto-selection-backtest/red-bar-takeout", dependencies=[Depends(require_admin_key)])
async def run_red_bar_takeout_backtest_endpoint():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders.
    The account owner's own real THIRD, lower-conviction setup,
    transcribed directly: "even if you don't have an elephant or a
    tail... little red bar take outs [work too]." Bar 1 is a real,
    ordinary red bar (not elephant-sized, not a qualifying tail) whose
    high still gets taken out by a later real bar - same real entry/
    stop/exit mechanics as the higher-conviction Elephant/Tail system,
    just without requiring bar 1 to be anything special. Never places a
    real order."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_red_bar_takeout_backtest()


@router.post("/crypto-selection-backtest/strategy-lab", dependencies=[Depends(require_admin_key)])
async def run_strategy_lab_backtest():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders.
    Built after the account owner pasted a third-party proposal (Spot
    Swing Trading / Automated Grid Bot / Hourly Momentum Trading) that
    contained no real backtest of its own, only illustrative arithmetic,
    and asked directly to see all three tested for real against real
    historical data, A/B/C/D style. See
    crypto_selection_backtest.run_strategy_lab_comparison() for the real
    replay logic - the existing live baseline plus all three new
    strategies, replayed on the identical real historical candles per
    coin so all four are directly, fairly comparable.

    Real, honest limit: none of the three new strategies are wired into
    live trading by this backtest, and grid_bot/swing_trading don't fit
    the live branch engine's current single-position-per-branch design
    at all - promoting any of them would be a real, separate decision
    once the account owner has seen these real numbers.

    Pulls real historical data from Coinbase's public candles endpoint -
    can take 30-90 seconds depending on that endpoint's response time."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_strategy_lab_comparison()


@router.post("/crypto-selection-backtest/grid-drawdown-breaker", dependencies=[Depends(require_admin_key)])
async def run_grid_drawdown_breaker_backtest():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders.
    Grid Bot went live with no account-level protection at all - a
    losing branch could keep buying new slices into a real, sustained
    decline indefinitely. Per the account owner's explicit "build both,
    backtest before going live," this replays several real candidate
    drawdown-breaker thresholds (plus a real no-breaker baseline) against
    the identical real historical Coinbase candles per coin, via
    crypto_selection_backtest.py's run_grid_drawdown_breaker_comparison -
    the exact same real equity/peak/drawdown math the live bot's own
    breaker uses, just replayed offline. Always includes today's real
    live default (crypto_grid_bot.GRID_DRAWDOWN_BREAKER_PCT, 25%) so it's
    directly comparable against every other candidate tested.

    Pulls real historical data from Coinbase's public candles endpoint -
    can take 30-90 seconds depending on that endpoint's response time."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_grid_drawdown_breaker_comparison()


@router.post("/crypto-selection-backtest/grid-fee-tier-spacing", dependencies=[Depends(require_admin_key)])
async def run_grid_fee_tier_spacing_backtest():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders.
    Real backtest for Grid Bot's opt-in fee-tier-aware dynamic spacing
    (crypto_grid_bot.compute_dynamic_grid_pct) - per the account owner's
    explicit "build both, backtest before going live." Replays the
    existing, already-validated grid-strategy replay at the real
    grid_pct each Coinbase Advanced Trade volume tier would produce,
    against the identical real historical candles per coin. See
    crypto_selection_backtest.py's run_grid_fee_tier_spacing_comparison
    for the full real methodology and its one honest, stated
    approximation (this sandbox has no live access to real historical
    fee-tier data, so tier spacing is modeled from Coinbase's publicly
    documented tier ratios against this codebase's own existing fee
    assumption - the LIVE feature itself reads the account's real
    current fee tier directly, no approximation needed there).

    Pulls real historical data from Coinbase's public candles endpoint -
    can take 30-90 seconds depending on that endpoint's response time."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_grid_fee_tier_spacing_comparison()


@router.post("/crypto-selection-backtest/grid-atr-spacing", dependencies=[Depends(require_admin_key)])
async def run_grid_atr_spacing_backtest():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders.
    Direct answer to the account owner's own question: "what is the
    average swing of coins... do you think it should stay at 1% or we
    should change it and see what the average of coins moving is and set
    it around that rate." Computes each real coin's own real average
    hourly price swing over the test window (from the same real
    historical candles the replay itself uses), then replays the
    existing, already-validated grid strategy at grid_pct set to several
    real multiples of that PER-COIN average (0.5x/1.0x/1.5x/2.0x),
    alongside today's real fixed 1% baseline - see
    crypto_selection_backtest.py's run_grid_atr_spacing_comparison for
    the full real methodology.

    Pulls real historical data from Coinbase's public candles endpoint -
    can take 30-90 seconds depending on that endpoint's response time."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_grid_atr_spacing_comparison()


@router.post("/crypto-selection-backtest/grid-higher-tf-trend", dependencies=[Depends(require_admin_key)])
async def run_grid_higher_tf_trend_backtest():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders.
    Direct follow-up to the account owner's own question after seeing
    narrow grid spacing lose money: does the same real higher-timeframe
    trend filter already validated for the family tree's own entries
    (SMA20 > SMA50 on hourly candles) reduce Grid Bot's real losses from
    buying into a decline and later FIFO-selling an older, higher-priced
    slice at a loss? Replays the existing, already-validated grid
    strategy at today's real live 1%/10-level default, with new buys
    gated on the real trend filter vs. an ungated real baseline, on
    identical real historical candles - see
    crypto_selection_backtest.py's run_grid_higher_tf_trend_comparison
    for the full real methodology.

    Pulls real historical data from Coinbase's public candles endpoint -
    can take 30-90 seconds depending on that endpoint's response time."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_grid_higher_tf_trend_comparison()


@router.post("/crypto-selection-backtest/grid-rotation-effectiveness", dependencies=[Depends(require_admin_key)])
async def run_grid_rotation_effectiveness_backtest_endpoint():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders.
    Direct answer to the account owner's own question after
    crypto_grid_9 disappeared (reallocated its own idle real cash into
    crypto_grid_5 and, per the existing "an emptied-out branch doesn't
    linger" design, was deleted once drained): does the real auto-
    rotation mechanism that moved that cash actually help real returns,
    or would it have done just as well leaving the cash parked? Replays
    a single real branch's own capital, starting on each real candidate
    coin in turn, two ways over the identical real historical data -
    parked the whole time vs. free to rotate to a better-ranked coin
    whenever flat - see crypto_selection_backtest.py's
    run_grid_rotation_effectiveness_backtest for the full real
    methodology and its one honest simplification (a BTC-relative-
    strength proxy standing in for the live blended ranking signal).

    Pulls real historical data from Coinbase's public candles endpoint -
    can take 30-90 seconds depending on that endpoint's response time."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_grid_rotation_effectiveness_backtest()


class SetExitModeRequest(BaseModel):
    mode: str


@router.post("/family-tree-status/set-exit-mode", dependencies=[Depends(require_admin_key)])
async def set_crypto_exit_mode(payload: SetExitModeRequest):
    """Promotes one of the 2 real, backtested exit philosophies (see
    crypto_selection_backtest.py's run_quick_profit_vs_trailing_stop_comparison,
    and crypto_family_tree_bot.py's run_branch_cycle which actually enforces
    whichever one is live) to production - the direct crypto-side
    counterpart to set_alpaca_entry_variant above, per the account owner's
    explicit request for "an option like that alpaca" after seeing the
    real QUICK_PROFIT-vs-trailing-stop comparison evidence.

    Deliberately restricted to exactly the 2 modes the backtest tool
    itself tested (quick_profit/trailing_stop) - there is no way to
    request an untested exit rule. Takes effect on the live bot's very
    next cycle for every branch - no restart needed, same as every other
    real-time flag in this codebase (STOP_TRADING, passive mode, the
    Alpaca entry variant)."""
    if crypto_family_tree_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_family_tree_bot module not available")
    mode = payload.mode.strip().lower()
    if mode not in crypto_family_tree_bot_module.EXIT_MODE_LEVELS:
        raise HTTPException(status_code=400, detail=f"mode must be one of {crypto_family_tree_bot_module.EXIT_MODE_LEVELS}, got {payload.mode!r}")
    await crypto_family_tree_bot_module.set_live_exit_mode(mode)
    log.info(f"[dashboard] 🎯 Live crypto exit mode promoted to '{mode}'")
    return {"status": "promoted", "exit_mode": mode}


class SetReversalTradeRequest(BaseModel):
    enabled: bool


@router.post("/family-tree-status/set-reversal-trade", dependencies=[Depends(require_admin_key)])
async def set_crypto_reversal_trade(payload: SetReversalTradeRequest):
    """Turns the real, opt-in STOP-HIT reversal buy on or off - the live
    wiring of what crypto_selection_backtest.py's
    run_stop_hit_reversal_backtest() already validated in shadow mode (94
    real STOP HIT events, 88.3% recovered to breakeven within 24h, 68.1%
    hypothetical win rate, +1.94% avg hypothetical P&L on a plain "buy
    back at the stop price" trade). Per the account owner's explicit
    "yes" after being shown that real evidence.

    Off by default - a true no-op until explicitly turned on here. Takes
    effect on the live bot's very next real STOP HIT for any branch, no
    restart needed, same as every other real-time flag in this codebase.
    See crypto_family_tree_bot._attempt_stop_hit_reversal_buy's own
    docstring for why this is deliberately scoped to a genuine STOP HIT
    only, never a BRANCH BREACH/EQUITY FLOOR BREACH forced exit."""
    if crypto_family_tree_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_family_tree_bot module not available")
    await crypto_family_tree_bot_module.set_reversal_trade_active(payload.enabled)
    log.info(f"[dashboard] 🔁 Live crypto STOP-HIT reversal buy {'ENABLED' if payload.enabled else 'disabled'}")
    return {"status": "updated", "reversal_trade_active": payload.enabled}


class SetTrailingStopPctRequest(BaseModel):
    pct: float


@router.post("/family-tree-status/set-trailing-stop-pct", dependencies=[Depends(require_admin_key)])
async def set_crypto_trailing_stop_pct(payload: SetTrailingStopPctRequest):
    """Promotes one of the real, backtested trailing-stop widths (see
    crypto_selection_backtest.py's run_trailing_stop_pct_sweep_comparison,
    and crypto_family_tree_bot.py's run_branch_cycle which actually
    enforces whichever one is live) to production - the direct trailing-
    stop-refinement counterpart to set_crypto_exit_mode above, per the
    account owner's explicit request to "refine and update" trailing
    stop rather than replace it outright.

    Deliberately restricted to exactly the candidate widths the sweep
    tool itself tested (TRAILING_STOP_PCT_CANDIDATES) - there is no way
    to request an untested trail width. Takes effect on the live bot's
    very next cycle for every branch - no restart needed, same as every
    other real-time flag in this codebase."""
    if crypto_family_tree_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_family_tree_bot module not available")
    try:
        await crypto_family_tree_bot_module.set_live_trailing_stop_pct(payload.pct)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    log.info(f"[dashboard] 🎯 Live crypto trailing-stop width promoted to {payload.pct * 100:.1f}%")
    return {"status": "promoted", "trailing_stop_pct": payload.pct}


@router.post("/crypto-selection-backtest/stop-hit-reversal", dependencies=[Depends(require_admin_key)])
async def run_crypto_stop_hit_reversal_backtest():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders.
    Built directly from the account owner's own real question, right
    after the exit-reason breakdown surfaced that most of a real losing
    window's damage wasn't from genuine price-based stop-losses at all
    (mostly legacy exit types that no longer exist, plus structural
    branch/floor-breach forced exits): "if we figure out a way to make
    money on it losing... we can make money off stops."

    Tests the honest, real version of that idea against the FULL real
    historical STOP HIT ledger (every coin, every real hard-stop exit
    ever recorded - not just one rolling 20-trade window): does price
    tend to recover after a real stop-loss, and would a simple
    hypothetical "buy back in right at the stop-exit price" trade have
    actually been profitable? See
    crypto_selection_backtest.py's run_stop_hit_reversal_backtest() for
    the full real methodology and its stated limitations (no fees
    modeled on the hypothetical trades, doesn't check real cash
    availability).

    Pulls real historical data from Coinbase's public candles endpoint -
    time depends on how many distinct coins have real STOP HIT history."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_stop_hit_reversal_backtest()


@router.post("/crypto-selection-backtest/forced-exit-reversal", dependencies=[Depends(require_admin_key)])
async def run_crypto_forced_exit_reversal_backtest():
    """SHADOW-MODE ONLY - does not touch live trading, places no orders.
    The direct follow-up to the Stop-Hit Reversal Backtest above, per the
    account owner's own real question after seeing that a real losing
    window was mostly driven by structural forced exits (a branch's own
    floor/drawdown-breach safety nets firing) rather than genuine STOP HIT
    price-stops: "how is there a way that we can make money off a system
    like that." Tests the identical real reversal hypothesis, scoped to
    the real, still-live BRANCH BREACH/EQUITY FLOOR BREACH exit types
    (never the legacy PEAK PROFIT GIVEBACK/QUICK PROFIT exit types from
    the removed QUICK_PROFIT era, which can never happen again live).

    See crypto_selection_backtest.py's run_forced_exit_reversal_backtest()
    for the full real methodology - identical to the stop-hit version,
    only the source exit_reason filter differs."""
    if crypto_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="crypto_selection_backtest module not available")
    return await crypto_selection_backtest_module.run_forced_exit_reversal_backtest()


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


@router.post("/alpaca-selection-backtest/exit-rule-comparison", dependencies=[Depends(require_admin_key)])
async def run_alpaca_exit_rule_comparison():
    """SHADOW-MODE ONLY - never touches live trading, places no order.
    Per the account owner's real question after ~4 months of live Alpaca
    trading (real deposit $980, real profit only ~$29-50): is the tight
    0.5% peak-giveback cap the reason winners never reach the real 2%
    target? Replays the SAME real historical Alpaca bars run_full_backtest()
    uses, under 3 exit-rule scenarios side by side (current 0.5%/2%,
    moderate 1.5%/3%, loose 2.5%/4%), using the bot's own real
    should_exit_position() for every scenario. Returns per-scenario totals
    (summed across every symbol) plus a per-symbol breakdown."""
    if alpaca_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="alpaca_selection_backtest module not available")
    return await alpaca_selection_backtest_module.run_exit_rule_sensitivity_comparison()


@router.post("/alpaca-selection-backtest/momentum-comparison", dependencies=[Depends(require_admin_key)])
async def run_alpaca_momentum_comparison():
    """SHADOW-MODE ONLY - never touches live trading, places no order.
    Per the account owner's real request: everything built so far is
    mean-reversion (buy weakness, small quick profit) - this replays the
    SAME real historical Alpaca bars under a genuinely different rule set:
    buy STRENGTH (RSI above 55 and price above its own 20-bar average)
    and exit via a trailing stop off the real peak price since entry
    (not a small fixed target), letting a real winner run further. Runs
    both the existing real mean-reversion replay and the new momentum
    replay against the identical real bars, so the two are directly,
    fairly comparable - real evidence before any real money is touched.
    Returns totals for both strategies (summed across every symbol) plus
    a per-symbol breakdown."""
    if alpaca_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="alpaca_selection_backtest module not available")
    return await alpaca_selection_backtest_module.run_momentum_vs_mean_reversion_comparison()


@router.post("/alpaca-selection-backtest/momentum-comparison-multi-window", dependencies=[Depends(require_admin_key)])
async def run_alpaca_momentum_comparison_multi_window(num_windows: int = 3):
    """SHADOW-MODE ONLY - never touches live trading, places no order.
    Built after the account owner ran the single-window momentum-vs-mean-
    reversion comparison above for real and got the OPPOSITE result from
    the run that originally justified switching the live bot to momentum
    months earlier (mean-reversion won this time, $54.58/353 trades vs
    momentum's $41.57/69 trades). A single 30-day window flipping isn't
    itself proof the live strategy is wrong - the same "require several
    consecutive results, not one" discipline the crypto side's
    auto-exclusion layer already uses applies here too. Runs the identical
    real comparison across `num_windows` consecutive, non-overlapping real
    historical 30-day windows (most recent first) and reports how many
    windows each strategy actually won, not just one sample's total."""
    if alpaca_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="alpaca_selection_backtest module not available")
    return await alpaca_selection_backtest_module.run_momentum_vs_mean_reversion_multi_window(num_windows=num_windows)


@router.post("/alpaca-selection-backtest/combined-strategy", dependencies=[Depends(require_admin_key)])
async def run_alpaca_combined_strategy_backtest():
    """SHADOW-MODE ONLY - never touches live trading, places no order.
    Real answer to the account owner's direct question after seeing the
    momentum-vs-mean-reversion comparison: "are we putting them together...
    together looks like it'll make a whole lot more money." The comparison
    above replays each ruleset independently, each with its own
    always-available $150/trade - correct for "which ruleset is better,"
    but not "would running both AT ONCE actually make more money," since a
    real account sharing one pool of cash can't spend the same dollar
    twice. This merges every symbol's real bars onto one real chronological
    timeline and runs both entry gates against a single shared pool -
    returns both a realistic "constrained" number (a real, modest shared
    pool) and a theoretical "unconstrained" ceiling (capital never binds),
    so the honest real effect of combining is directly visible either way."""
    if alpaca_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="alpaca_selection_backtest module not available")
    return await alpaca_selection_backtest_module.run_combined_dual_strategy_backtest()


@router.post("/alpaca-selection-backtest/entry-signal-ab-test", dependencies=[Depends(require_admin_key)])
async def run_alpaca_entry_signal_ab_test():
    """SHADOW-MODE ONLY - never touches live trading, places no order.
    Real, well-reasoned pushback on the live momentum entry (RSI > 55 AND
    price > SMA20 is binary - it can't tell a fresh breakout from a stock
    that's already run and is due to snap back). Replays the SAME real
    historical bars under 4 entry variants that progressively layer on
    real filters (RSI rising, SMA20 rising, an overextension cap), with
    the exit rule held completely fixed across all four so this isolates
    entry-signal quality specifically. Returns a real multi-metric summary
    per variant (win rate, profit factor, max drawdown, Sharpe/Sortino,
    avg holding time, longest losing streak - not just total P&L) plus a
    per-symbol P&L breakdown across all four."""
    if alpaca_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="alpaca_selection_backtest module not available")
    return await alpaca_selection_backtest_module.run_entry_signal_ab_test()


@router.post("/alpaca-selection-backtest/narrow-range-breakout", dependencies=[Depends(require_admin_key)])
async def run_alpaca_narrow_range_breakout_backtest():
    """SHADOW-MODE ONLY - never touches live trading, places no order.
    The Alpaca-side counterpart to the crypto narrow-range-breakout
    backtest - tests the account owner's own real trading claim about
    narrow-range breakout continuation, but MORE LITERALLY than crypto
    could (stocks have a real discrete daily open crypto's 24/7 market
    doesn't): groups real historical 15-min bars into real trading days,
    finds real days whose own range is genuinely NARROW relative to that
    symbol's own recent range history, and checks the very next real
    trading day's actual FIRST bar against that narrow day's own high/low
    - exactly "when your stock opens in the morning, the first bar opens
    above/below a narrow state." Reports the real hit rate against an
    honest 50% coin-flip baseline, split by breakout direction."""
    if alpaca_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="alpaca_selection_backtest module not available")
    return await alpaca_selection_backtest_module.run_narrow_range_breakout_backtest()


@router.post("/alpaca-selection-backtest/opening-bar-breakout", dependencies=[Depends(require_admin_key)])
async def run_alpaca_opening_bar_breakout_backtest():
    """SHADOW-MODE ONLY - never touches live trading, places no order.
    Tests the account owner's own fully-specified real trading system,
    running on the real thing (stocks have a genuine 2-minute bar and a
    real discrete session open, unlike crypto's invented UTC stand-in):
    real first 2-min bar of the day must be a real "Elephant Bar" or
    "bottoming Tail" bar; entry the instant bar 2's real price crosses
    bar 1's high + $0.01 (never waiting for bar 2 to close); real stop
    at bar 1's own low; real exit once a second "push" confirms."""
    if alpaca_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="alpaca_selection_backtest module not available")
    return await alpaca_selection_backtest_module.run_opening_bar_breakout_backtest()


@router.post("/alpaca-selection-backtest/opening-bar-multi-entry-comparison", dependencies=[Depends(require_admin_key)])
async def run_alpaca_opening_bar_multi_entry_comparison():
    """SHADOW-MODE ONLY - never touches live trading, places no order.
    Per the account owner's own real reference chart (a staircase of
    several pullback-and-continuation entries through one session, not
    just the first): compares today's real one-entry-per-day baseline
    against a new multi-entry version that keeps trading the same
    established real trend through every subsequent confirmed pullback/
    breakout leg, on the identical real historical bars."""
    if alpaca_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="alpaca_selection_backtest module not available")
    return await alpaca_selection_backtest_module.run_opening_bar_multi_entry_comparison()


@router.post("/alpaca-selection-backtest/opening-bar-narrow-state-comparison", dependencies=[Depends(require_admin_key)])
async def run_alpaca_opening_bar_narrow_state_comparison():
    """SHADOW-MODE ONLY - never touches live trading, places no order.
    The Alpaca-side counterpart to the crypto comparison above - compares
    three real narrow-state definitions against the IDENTICAL real
    Elephant/Tail opening-bar trades: no gate (baseline), the existing
    percentile-range method, and the account owner's own newly-described
    real 20/200 SMA-convergence method."""
    if alpaca_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="alpaca_selection_backtest module not available")
    return await alpaca_selection_backtest_module.run_opening_bar_narrow_state_comparison()


@router.post("/alpaca-selection-backtest/wide-state-contrarian", dependencies=[Depends(require_admin_key)])
async def run_alpaca_wide_state_contrarian_backtest():
    """SHADOW-MODE ONLY - never touches live trading, places no order.
    The Alpaca-side counterpart to the crypto wide-state contrarian
    backtest above: "you become a contrarian trader in the wide state...
    the drop brings you back to narrow." Both directions here are
    genuinely long-only executable in spirit (a wide_down real reversion
    LONG matches what prop_bot.py can already place) - the wide_up
    SHORT leg is still reported as pure diagnostic information, since
    prop_bot.py's real shorting is a documented, confirmed account-level
    restriction, not a bug in this backtest."""
    if alpaca_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="alpaca_selection_backtest module not available")
    return await alpaca_selection_backtest_module.run_wide_state_contrarian_backtest()


@router.post("/alpaca-selection-backtest/opening-bar-short-side", dependencies=[Depends(require_admin_key)])
async def run_alpaca_opening_bar_short_side_backtest():
    """SHADOW-MODE ONLY, DIAGNOSTIC ONLY - never touches live trading,
    places no order. The Alpaca-side counterpart to the crypto bearish-
    mirror backtest above: a real RED Elephant Bar or topping Tail bar
    breaking below a real narrow state. Never places a real order -
    prop_bot.py's real shorting is a documented, confirmed account-level
    restriction, not a bug in this backtest."""
    if alpaca_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="alpaca_selection_backtest module not available")
    return await alpaca_selection_backtest_module.run_opening_bar_short_side_backtest()


@router.post("/alpaca-selection-backtest/scaled-entry-comparison", dependencies=[Depends(require_admin_key)])
async def run_alpaca_scaled_entry_comparison_backtest():
    """SHADOW-MODE ONLY - never touches live trading, places no order.
    The Alpaca-side counterpart to the crypto scaled-entry comparison
    above - replays the IDENTICAL real qualifying Elephant/Tail setups
    two ways: the existing single-shot entry vs. the account owner's own
    real half-in-then-two-adds scaling mechanic."""
    if alpaca_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="alpaca_selection_backtest module not available")
    return await alpaca_selection_backtest_module.run_scaled_entry_comparison_backtest()


@router.post("/alpaca-selection-backtest/red-bar-takeout", dependencies=[Depends(require_admin_key)])
async def run_alpaca_red_bar_takeout_backtest():
    """SHADOW-MODE ONLY - never touches live trading, places no order.
    The Alpaca-side counterpart to the crypto red-bar-takeout backtest
    above - the account owner's own real third, lower-conviction setup:
    an ordinary red bar 1 (not a qualifying Elephant or Tail) whose high
    still gets taken out by a later real bar."""
    if alpaca_selection_backtest_module is None:
        raise HTTPException(status_code=500, detail="alpaca_selection_backtest module not available")
    return await alpaca_selection_backtest_module.run_red_bar_takeout_backtest()


@router.post("/macro-event-backtest", dependencies=[Depends(require_admin_key)])
async def run_macro_event_backtest_endpoint():
    """SHADOW-MODE ONLY - never touches live trading, places no order. Per
    the account owner's own direct request after sharing a real US Balance
    of Trade release: "if you specifically think broad macro releases...
    affect how BTC or the stocks move around release dates... Back-test
    them and let me look and make a decision."

    Real event-study backtest: measures BTC-USD's and SPY/QQQ's own real
    return and volatility over the real window following each real macro
    release date in macro_event_backtest.MACRO_EVENTS, against a real
    random-window baseline drawn from the same real fetched history. The
    event dates themselves are real, verified release dates the account
    owner pasted directly - not guessed. See macro_event_backtest.py's own
    module docstring for the full real methodology and its honest
    sample-size caveat (currently just 2 real events - far too few to
    conclude anything from yet)."""
    if macro_event_backtest_module is None:
        raise HTTPException(status_code=500, detail="macro_event_backtest module not available")
    return await macro_event_backtest_module.run_macro_event_backtest()


def _safe_float(v):
    """Alpaca's real REST API returns numeric position fields as JSON
    strings (e.g. "150.25", not 150.25) - this converts them to real
    floats, returning None on anything that genuinely can't be parsed
    (missing field, real None) rather than raising or silently
    defaulting to 0, which would fabricate a fake price/qty."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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
    alpaca_passive_mode = await prop_bot_module.is_alpaca_passive_mode() if prop_bot_module else False
    entry_variant = await prop_bot_module.get_live_entry_variant() if prop_bot_module else "A"
    strategy_family = await prop_bot_module.get_live_strategy_family() if prop_bot_module else "momentum"

    return {
        "equity": round(equity, 2),
        "alpaca_passive_mode": alpaca_passive_mode,
        "entry_variant": entry_variant,
        "strategy_family": strategy_family,
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
        "profit_skim_pct": ALPACA_PROFIT_SKIM_PCT,
        "bots": [{"name": b.bot_name, "capital": round(b.base_capital, 2), "profit": round(_bot_profit(b), 2), "pl": round(_bot_pl(b), 2)} for b in bots],
        "positions": [
            {
                "symbol": p.get("symbol"),
                "side": p.get("side"),
                # Alpaca's real REST API returns these numeric fields as
                # JSON strings, not numbers - a real bug found via
                # status_snapshot.py crashing on "current - entry" (str -
                # str). JS callers (alpaca_dashboard.html) never noticed
                # since JS's `-` operator silently coerces strings to
                # numbers; a Python consumer doing real arithmetic on this
                # payload does not have that luxury. Cast explicitly here
                # so every consumer of this endpoint gets real floats.
                "qty": _safe_float(p.get("qty")),
                "avg_entry_price": _safe_float(p.get("avg_entry_price")),
                "current_price": _safe_float(p.get("current_price")),
                "market_value": _safe_float(p.get("market_value")),
                "unrealized_pl": _safe_float(p.get("unrealized_pl")),
                "unrealized_plpc": _safe_float(p.get("unrealized_plpc")),
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


@router.post("/alpaca-overview/liquidate-and-buy-spy", dependencies=[Depends(require_admin_key)])
async def liquidate_alpaca_and_buy_spy(db: AsyncSession = Depends(get_db)):
    """Per the account owner's explicit, real decision: retire active
    Alpaca trading entirely (prop_bot.py's mean-reversion futures-proxy
    trading AND alpaca_swing_bot.py's separate swing/day strategy - both
    place real trades on this same account) and replace it with a single
    real buy-and-hold SPY position, after real evidence showed both a
    HYSA and, in this particular strong stretch, the S&P 500 itself
    outperformed this account's real active-trading return.

    A ONE-WAY real action, in order:
    1. Closes EVERY real open position on the account at market (the same
       real DELETE /v2/positions/{symbol}?cancel_orders=true Alpaca's own
       app uses, same pattern as the existing manual close-one endpoint) -
       reads the real position list directly from Alpaca, so this closes
       everything regardless of which of the two bots opened it.
    2. Records each real realized P&L as a Payment row, same bookkeeping
       the existing manual close already does, so nothing vanishes from
       earnings tracking.
    3. Sets is_alpaca_passive_mode() to True BEFORE buying, so nothing can
       race in and open a new position in the gap between closing
       everything and the SPY buy landing - both prop_bot.py's and
       alpaca_swing_bot.py's own main loops check this every cycle and
       fully stop (no entries, no exit-management - there's nothing left
       to manage) once it's set. This is a real, deliberate retirement,
       not a pause - it stays off only if explicitly turned back on.
    4. Buys real SPY with ~99.5% of the real cash freed up (a small
       buffer, same reasoning as the crypto side's real-balance clamp -
       leaves a sliver rather than requesting exactly 100% of a balance
       that could shift by the time the order executes), via a real
       Alpaca notional (dollar-amount) market order - Alpaca computes the
       real fractional share count itself, so this doesn't need a
       separately-fetched price to compute qty from.

    The resulting real SPY position needs no separate tracking - it's not
    added to open_prop_positions (passive mode means nothing ever reads
    that dict to manage it again anyway), so the dashboard's existing real
    Alpaca positions list already shows it accurately going forward,
    straight from Alpaca itself."""
    if prop_bot_module is None:
        raise HTTPException(status_code=500, detail="prop_bot module not available")
    pb = prop_bot_module
    if not (ALPACA_KEY and ALPACA_SECRET):
        raise HTTPException(status_code=500, detail="Alpaca credentials not configured")

    async with aiohttp.ClientSession() as session:
        positions = await _fetch_alpaca_positions(session)
        symbol_to_contract = {cfg["symbol"]: code for code, cfg in pb.FUTURES.items()}

        closed = []
        for p in positions:
            symbol = p.get("symbol")
            if not symbol:
                continue
            try:
                qty = float(p.get("qty", 0))
                entry_price = float(p.get("avg_entry_price", 0))
                current_price = float(p.get("current_price", entry_price))
                pnl = (current_price - entry_price) * qty
            except (ValueError, TypeError):
                qty, pnl = 0.0, 0.0

            async with session.delete(
                f"{ALPACA_BASE_URL}/v2/positions/{symbol}?cancel_orders=true", headers=ALPACA_HEADERS
            ) as r:
                if r.status not in (200, 207):
                    body = await r.text()
                    log.error(f"[liquidate-to-spy] failed to close {symbol} ({r.status}): {body}")
                    continue

            contract = symbol_to_contract.get(symbol)
            if contract:
                pb.open_prop_positions.pop(contract, None)
                await pb._db_delete_open(contract)

            try:
                db.add(Payment(
                    id=f"liquidate_{uuid.uuid4().hex[:8]}",
                    job_id=f"liquidate_{symbol}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
                    worker_id="bot@pgusa.local",
                    client_id="alpaca_liquidate_to_spy",
                    gross_amount=pnl,
                    worker_amount=pnl * 0.90,
                    platform_amount=pnl * 0.10,
                    payout_status="pending" if pnl > 0 else "completed",
                ))
                await db.commit()
            except Exception as e:
                log.warning(f"Failed to record liquidation earnings for {symbol}: {e}")

            closed.append({"symbol": symbol, "qty": qty, "realized_pnl": round(pnl, 2)})
            log.info(f"[liquidate-to-spy] closed {symbol}: qty={qty}, realized_pnl=${pnl:.2f}")

        await pb.set_alpaca_passive_mode(True)

        account = await _fetch_alpaca_account(session)
        try:
            cash = float(account.get("cash", 0))
        except (ValueError, TypeError):
            cash = 0.0

        if cash < 1.0:
            log.warning(f"[liquidate-to-spy] only ${cash:.2f} real cash free after closing - not enough to buy SPY, passive mode still enabled")
            return {
                "status": "closed_only",
                "closed_positions": closed,
                "cash_after_closing": round(cash, 2),
                "passive_mode": True,
                "note": "Real free cash after closing was too small to buy SPY. Active trading is still retired - nothing will open a new position on its own.",
            }

        spend = round(cash * 0.995, 2)
        order = {"symbol": "SPY", "notional": str(spend), "side": "buy", "type": "market", "time_in_force": "day"}
        async with session.post(f"{ALPACA_BASE_URL}/v2/orders", headers=ALPACA_HEADERS, json=order) as r:
            spy_order = await r.json()
            if r.status not in (200, 201):
                log.error(f"[liquidate-to-spy] real SPY buy failed ({r.status}): {spy_order}")
                raise HTTPException(status_code=502, detail=f"Closed {len(closed)} position(s) for real, but the real SPY buy order failed: {spy_order.get('message', spy_order)} - passive mode is still enabled, retry the buy manually or via this endpoint again")

    log.info(f"[dashboard] 🔒📈 Liquidated {len(closed)} real position(s), bought ~${spend:.2f} of real SPY - Alpaca active trading retired")
    return {
        "status": "liquidated_and_bought_spy",
        "closed_positions": closed,
        "cash_after_closing": round(cash, 2),
        "spy_order_notional": spend,
        "spy_order": spy_order,
        "passive_mode": True,
    }


@router.post("/alpaca-overview/resume-active-trading", dependencies=[Depends(require_admin_key)])
async def resume_alpaca_active_trading():
    """Reverses is_alpaca_passive_mode() - per the account owner's explicit
    request to let prop_bot.py/alpaca_swing_bot.py resume real automatic
    entries and exit-management on this account, after having retired to
    the buy-and-hold SPY position (see liquidate_alpaca_and_buy_spy above).

    Deliberately does NOT touch the real SPY position bought at retirement
    time - that was never tracked in open_prop_positions (passive mode
    means nothing ever read that dict to manage it), so resuming doesn't
    suddenly try to manage or sell it. It just sits in the account's real
    position list, sellable by hand via the existing manual close endpoint,
    exactly as it did while passive mode was on - the two bots' own next
    cycle will simply resume scanning for new momentum entries and start
    managing whatever they open going forward."""
    if prop_bot_module is None:
        raise HTTPException(status_code=500, detail="prop_bot module not available")
    was_passive = await prop_bot_module.is_alpaca_passive_mode()
    await prop_bot_module.set_alpaca_passive_mode(False)
    log.info("[dashboard] 🔓📉 Alpaca active trading resumed (passive/buy-and-hold-SPY mode turned off)")
    return {"status": "active_trading_resumed", "was_passive": was_passive, "passive_mode": False}


class SetEntryVariantRequest(BaseModel):
    variant: str


@router.post("/alpaca-overview/set-entry-variant", dependencies=[Depends(require_admin_key)])
async def set_alpaca_entry_variant(payload: SetEntryVariantRequest):
    """Promotes one of the 4 real, backtested entry-gate variants (see
    alpaca_selection_backtest.py's ENTRY_VARIANTS / run_entry_signal_ab_test,
    and prop_bot.py's check_momentum_entry_gate which actually enforces
    whichever one is live) to production - per the account owner's explicit
    request to see the real backtest results, then push whichever variant
    performs best straight to the live bot from the dashboard, without a
    manual code change each time.

    Deliberately restricted to exactly the 4 combinations the backtest tool
    itself tested (A/B/C/D, each cumulative on the previous) - there is no
    way to request an untested combination of filters, so the live bot can
    never end up running something that was never actually validated. Takes
    effect on prop_bot.py's very next cycle - no restart needed, same as
    every other real-time flag in this codebase (STOP_TRADING, passive
    mode)."""
    if prop_bot_module is None:
        raise HTTPException(status_code=500, detail="prop_bot module not available")
    variant = payload.variant.strip().upper()
    if variant not in prop_bot_module.ENTRY_VARIANT_LEVELS:
        raise HTTPException(status_code=400, detail=f"variant must be one of {prop_bot_module.ENTRY_VARIANT_LEVELS}, got {payload.variant!r}")
    await prop_bot_module.set_live_entry_variant(variant)
    log.info(f"[dashboard] 🎯 Live Alpaca entry variant promoted to '{variant}'")
    return {"status": "promoted", "entry_variant": variant}


class SetStrategyFamilyRequest(BaseModel):
    family: str


@router.post("/alpaca-overview/set-strategy-family", dependencies=[Depends(require_admin_key)])
async def set_alpaca_strategy_family(payload: SetStrategyFamilyRequest):
    """Switches the live Alpaca strategy between "momentum" (buy strength,
    trailing stop) and "mean_reversion" (buy oversold, fixed target/stop/
    breakeven/giveback) - a real, reversible toggle, not a one-way code
    change, per the account owner's explicit real decision after
    run_momentum_vs_mean_reversion_multi_window() showed mean-reversion
    winning 3 of 3 real 30-day windows ($77.51 vs momentum's $14.30
    total), directly contradicting the single-window comparison that
    originally justified switching TO momentum. See
    prop_bot.get_live_strategy_family()'s own docstring for why this is
    reversible: the same real comparison already flipped once between
    real windows tonight, so a future re-run favoring momentum again
    should be just as easy to act on.

    Mean-reversion's real entry threshold (RSI < 40) and exit parameters
    (1.5% stop, 3% target / 1.5% giveback - the "moderate" scenario,
    already the account's own prior real decision and reconfirmed by
    tonight's fresh exit-rule-sensitivity re-run) are fixed constants in
    prop_bot.py, not user-supplied - this can only ever switch between
    the two real, already-validated configurations, never an untested
    combination. Takes effect on prop_bot.py's very next cycle - no
    restart needed, same as every other real-time flag in this codebase."""
    if prop_bot_module is None:
        raise HTTPException(status_code=500, detail="prop_bot module not available")
    family = payload.family.strip().lower()
    if family not in prop_bot_module.STRATEGY_FAMILIES:
        raise HTTPException(status_code=400, detail=f"family must be one of {prop_bot_module.STRATEGY_FAMILIES}, got {payload.family!r}")
    await prop_bot_module.set_live_strategy_family(family)
    log.info(f"[dashboard] 🔀 Live Alpaca strategy family switched to '{family}'")
    return {"status": "switched", "strategy_family": family}


@router.get("/alpaca-overview/branches", dependencies=[Depends(require_admin_key)])
async def get_alpaca_branches_status():
    """Real status of the Alpaca branch system - a smaller, real first
    slice toward something like the crypto family tree's compounding
    branches, per the account owner's explicit request. See prop_bot.py's
    own ALPACA BRANCHES section docstring for the full real design (why
    it's scoped down from the full spawn-tree, how capital partitioning
    works, why it's off by default). Read-only - never places an order.

    Also reports real, current buying-power affordability
    (buying_power/already_allocated_usd/real_spendable_usd) using the
    EXACT SAME formula create_alpaca_branch_endpoint() enforces at submit
    time - per the account owner's explicit complaint that the "New Real
    Branch" modal's Allocated Capital field gave zero guidance on what
    they actually had free, forcing them to leave the page to check.
    Fails open on a real buying-power fetch hiccup (returns null for
    those three fields rather than erroring the whole status call) -
    this endpoint's job is to inform, not to gate; the real, blocking
    affordability check still lives in the create endpoint.

    Also reports each branch's real progress toward its own
    `next_unlock_tier` - per the account owner's explicit "let me know
    when it's about ready to [reinforce] some more money" request. This
    is the real, live number `prop_bot._alpaca_maybe_spawn_or_reinforce()`
    itself checks every cycle (`allocated_usd >= next_unlock_tier`), not a
    separately-estimated one - so the dashboard can never show "ready"
    when the bot itself isn't. `reinforcement_progress_pct` is `None` for
    a legacy branch with no `next_unlock_tier` on record yet (a row
    created before this column existed)."""
    if prop_bot_module is None:
        raise HTTPException(status_code=500, detail="prop_bot module not available")
    branches = await prop_bot_module.get_alpaca_branches()
    mode_active = await prop_bot_module.is_alpaca_branch_mode_active()
    rows = []
    for b in branches:
        position = prop_bot_module.open_alpaca_branch_positions.get(b.bot_name)
        config = prop_bot_module.FUTURES.get(b.contract, {})
        progress_pct = None
        if b.next_unlock_tier:
            progress_pct = round(min(100.0, (b.allocated_usd / b.next_unlock_tier) * 100), 1)
        rows.append({
            "bot_name": b.bot_name,
            "contract": b.contract,
            "symbol": config.get("symbol"),
            "allocated_usd": round(b.allocated_usd, 2),
            "active": b.active,
            "position": position,
            "next_unlock_tier": round(b.next_unlock_tier, 2) if b.next_unlock_tier else None,
            "reinforcement_progress_pct": progress_pct,
        })

    buying_power = None
    try:
        async with aiohttp.ClientSession() as session:
            buying_power = await prop_bot_module.get_account_buying_power(session)
    except Exception as e:
        log.warning(f"[dashboard] real buying-power fetch failed for branch status: {e}")

    already_allocated = sum(b.allocated_usd for b in branches if b.active)
    real_spendable = (buying_power - already_allocated) if buying_power is not None else None

    return {
        "mode_active": mode_active,
        "branches": rows,
        "total_allocated_usd": round(sum(b.allocated_usd for b in branches), 2),
        "buying_power": round(buying_power, 2) if buying_power is not None else None,
        "already_allocated_usd": round(already_allocated, 2),
        "real_spendable_usd": round(real_spendable, 2) if real_spendable is not None else None,
    }


@router.get("/alpaca-overview/branch-trade-history", dependencies=[Depends(require_admin_key)])
async def get_alpaca_branch_trade_history_endpoint():
    """Real, per-branch win rate and cumulative P&L for the Alpaca
    branches - per the account owner's explicit request to see the real
    money "adding up" for a branch, not just its current Allocated
    number with no history behind it. Reads AlpacaBranchTradeHistory
    (written the moment a real branch sell fills, in
    prop_bot.run_alpaca_branch_cycle()) via
    prop_bot.get_alpaca_branch_trade_history() - the exact same real
    aggregation, not a second, separately-computed number. Read-only -
    never places an order."""
    if prop_bot_module is None:
        raise HTTPException(status_code=500, detail="prop_bot module not available")
    return await prop_bot_module.get_alpaca_branch_trade_history()


@router.get("/alpaca-overview/branch-symbol-rankings", dependencies=[Depends(require_admin_key)])
async def get_alpaca_branch_symbol_rankings():
    """Real backtested ROI per contract, ranked best to worst - per the
    account owner's explicit request to see this directly inside the New
    Real Branch modal instead of having to leave the page and cross-
    reference the separate Stock/ETF Selection Backtest page by hand.
    Reuses the exact same real data prop_bot.py's own top-N concentration
    filter and auto-exclusion layer already read
    (AlpacaBacktestRun/_compute_top_ranked_symbols/describe_symbol_exclusion_reason)
    - this can never disagree with what the live bot itself would
    actually trade. Read-only - never places an order, never runs a new
    backtest (that's still the separate manual "Run Backtest" button)."""
    if prop_bot_module is None:
        raise HTTPException(status_code=500, detail="prop_bot module not available")

    top_ranked = await prop_bot_module._compute_top_ranked_symbols()
    excluded = await prop_bot_module.get_effective_excluded_symbols()

    rows = []
    for contract, config in prop_bot_module.FUTURES.items():
        symbol = config["symbol"]
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AlpacaBacktestRun)
                .where(AlpacaBacktestRun.product_id == symbol)
                .order_by(AlpacaBacktestRun.run_at.desc())
                .limit(1)
            )
            latest = result.scalar_one_or_none()

        is_excluded = symbol in excluded
        rows.append({
            "contract": contract,
            "name": config["name"],
            "symbol": symbol,
            "num_trades": latest.num_trades if latest else None,
            "win_rate": round(latest.win_rate, 1) if latest else None,
            "roi_pct": round(latest.roi_pct_of_spend, 2) if latest else None,
            "run_at": (latest.run_at.isoformat() + "Z") if latest and latest.run_at else None,
            "in_top_n": (top_ranked is None) or (symbol in top_ranked),
            "excluded": is_excluded,
            "excluded_reason": (await prop_bot_module.describe_symbol_exclusion_reason(symbol)) if is_excluded else None,
        })

    # Real backtested symbols (highest ROI first) come before symbols with
    # no real run on record yet - a symbol nobody has ever backtested
    # shouldn't outrank one with real, if mediocre, evidence behind it.
    rows.sort(key=lambda r: (r["roi_pct"] is None, -(r["roi_pct"] or 0)))
    return {"rankings": rows, "top_n": prop_bot_module.TOP_N_ELIGIBLE_SYMBOLS}


class CreateAlpacaBranchRequest(BaseModel):
    contract: str
    allocated_usd: float


@router.post("/alpaca-overview/branches", dependencies=[Depends(require_admin_key)])
async def create_alpaca_branch_endpoint(payload: CreateAlpacaBranchRequest):
    """Creates a real new Alpaca branch - a pure bookkeeping operation
    (see prop_bot.create_alpaca_branch's own docstring), never a trade by
    itself. Refuses if the requested amount exceeds real free buying
    power (real account buying power minus whatever's already allocated
    to other active branches - the same real-affordability reasoning the
    crypto side's spawn-branch endpoint already uses), if the contract
    isn't a real FUTURES key, or if it's already claimed by another
    active branch."""
    if prop_bot_module is None:
        raise HTTPException(status_code=500, detail="prop_bot module not available")
    if payload.contract not in prop_bot_module.FUTURES:
        raise HTTPException(status_code=400, detail=f"{payload.contract!r} is not a real FUTURES contract. Choose one of: {list(prop_bot_module.FUTURES.keys())}")
    if payload.allocated_usd <= 0:
        raise HTTPException(status_code=400, detail="allocated_usd must be positive")

    async with aiohttp.ClientSession() as session:
        buying_power = await prop_bot_module.get_account_buying_power(session)
    if buying_power is None:
        raise HTTPException(status_code=502, detail="could not fetch real Alpaca buying power right now - try again shortly")

    existing = await prop_bot_module.get_alpaca_branches()
    already_allocated = sum(b.allocated_usd for b in existing if b.active)
    real_spendable = buying_power - already_allocated
    if payload.allocated_usd > real_spendable:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Only ${real_spendable:.2f} in real free buying power right now "
                f"(${buying_power:.2f} total buying power - ${already_allocated:.2f} already allocated to "
                f"other active branches) - can't allocate ${payload.allocated_usd:.2f}"
            ),
        )

    try:
        branch = await prop_bot_module.create_alpaca_branch(payload.contract, payload.allocated_usd)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "created", "bot_name": branch.bot_name, "contract": branch.contract, "allocated_usd": round(branch.allocated_usd, 2)}


class SetAlpacaBranchModeRequest(BaseModel):
    enabled: bool


@router.post("/alpaca-overview/branches/mode", dependencies=[Depends(require_admin_key)])
async def set_alpaca_branch_mode_endpoint(payload: SetAlpacaBranchModeRequest):
    """The real master switch for the whole Alpaca branch system - off by
    default (is_alpaca_branch_mode_active). While off, every branch cycle
    is a true no-op regardless of how many branches exist. Real branches
    can be created while the mode is off (so they're ready before flipping
    it on), but nothing trades until this is explicitly enabled."""
    if prop_bot_module is None:
        raise HTTPException(status_code=500, detail="prop_bot module not available")
    await prop_bot_module.set_alpaca_branch_mode(payload.enabled)
    log.info(f"[dashboard] 🔀 Alpaca branch mode {'ENABLED - real branch trading is now live' if payload.enabled else 'disabled'}")
    return {"status": "updated", "mode_active": payload.enabled}


@router.get("/alpaca-overview/opening-bar-status", dependencies=[Depends(require_admin_key)])
async def get_opening_bar_status():
    """Real status of the opening-bar live trading system (the validated
    multi-entry elephant/tail breakout - see prop_bot.py's own OPENING-BAR
    LIVE TRADING section docstring for the full real design). Read-only -
    never places an order. Off by default; a true no-op until explicitly
    enabled here."""
    if prop_bot_module is None:
        raise HTTPException(status_code=500, detail="prop_bot module not available")
    mode_active = await prop_bot_module.is_opening_bar_live_active()
    positions = []
    for contract, pos in prop_bot_module.open_opening_bar_positions.items():
        config = prop_bot_module.FUTURES.get(contract, {})
        positions.append({
            "contract": contract,
            "symbol": config.get("symbol"),
            "entry_price": round(pos.get("entry_price", 0.0), 2),
            "qty": pos.get("qty"),
            "stop_price": round(pos.get("stop_price", 0.0), 2),
            "leg_number": pos.get("leg_number"),
            "qualifies_as": pos.get("qualifies_as"),
        })
    return {
        "mode_active": mode_active,
        "positions": positions,
        "total_notional_usd": round(prop_bot_module._total_opening_bar_notional(), 2),
        "watchlist": list(prop_bot_module.FUTURES.keys()),
    }


class SetOpeningBarLiveModeRequest(BaseModel):
    enabled: bool


@router.post("/alpaca-overview/opening-bar-mode", dependencies=[Depends(require_admin_key)])
async def set_opening_bar_live_mode_endpoint(payload: SetOpeningBarLiveModeRequest):
    """The real master switch for the opening-bar live trading system -
    off by default (is_opening_bar_live_active). While off, its per-cycle
    driver is a true no-op. Places real orders, sized via the same real
    size_position()/check_margin_safety() every other real entry on this
    account already goes through, once enabled."""
    if prop_bot_module is None:
        raise HTTPException(status_code=500, detail="prop_bot module not available")
    await prop_bot_module.set_opening_bar_live_active(payload.enabled)
    log.info(f"[dashboard] 🐘 Opening-bar live trading {'ENABLED - real orders will be placed' if payload.enabled else 'disabled'}")
    return {"status": "updated", "mode_active": payload.enabled}


class SetAlpacaBranchActiveRequest(BaseModel):
    active: bool


@router.post("/alpaca-overview/branches/{bot_name}/active", dependencies=[Depends(require_admin_key)])
async def set_alpaca_branch_active_endpoint(bot_name: str, payload: SetAlpacaBranchActiveRequest):
    """Pauses or resumes ONE specific branch without touching the master
    switch or any other branch. A paused branch's own contract is also
    released back to the whole-account scan (get_alpaca_branch_claimed_contracts
    only ever returns ACTIVE branches) - it does NOT force-close a
    currently-open position on that branch, matching the "never force a
    real position closed by a settings change" principle used elsewhere
    in this codebase; the position keeps running under its own real
    exit protection until it closes normally, it just won't open a new
    one while paused."""
    if prop_bot_module is None:
        raise HTTPException(status_code=500, detail="prop_bot module not available")
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(AlpacaBranch).where(AlpacaBranch.bot_name == bot_name))
        branch = result.scalar_one_or_none()
        if branch is None:
            raise HTTPException(status_code=404, detail=f"no branch named {bot_name!r}")
        branch.active = payload.active
        await db.commit()
    log.info(f"[dashboard] {'▶️ Resumed' if payload.active else '⏸️ Paused'} Alpaca branch {bot_name}")
    return {"status": "updated", "bot_name": bot_name, "active": payload.active}


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
    entry path calls (get_price_momentum, validate_entry/APEX_MANDATE's
    universe check, check_kill_conditions, check_margin_safety,
    size_position, execute_futures_trade) rather than reimplementing any
    of them, so a manual entry gets the same real protection an
    automatic one does - it's just triggered on demand instead of by a
    live momentum signal. Long-only, matching everything else prop_bot.py can
    actually execute today (shorting is disabled on the real account -
    see get_account_shorting_enabled)."""
    if prop_bot_module is None:
        raise HTTPException(status_code=500, detail="prop_bot module not available")
    pb = prop_bot_module
    ticker = ticker.upper()

    if os.getenv("STOP_TRADING", "false").lower() == "true":
        raise HTTPException(status_code=400, detail="STOP_TRADING is set - all entries (manual or automatic) are paused")
    if await pb.is_alpaca_passive_mode():
        raise HTTPException(status_code=400, detail="Active Alpaca trading has been retired in favor of a real buy-and-hold SPY position - no new entries")

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
        reason = await pb.describe_symbol_exclusion_reason(ticker)
        raise HTTPException(status_code=400, detail=f"{ticker} is currently excluded - {reason}")

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

        # Which real strategy family is live right now - also re-syncs
        # bot_mandates.APEX_MANDATE["entry"] as a side effect (see
        # get_live_strategy_family()'s own docstring), so the mandate
        # check right below always matches whichever family is actually
        # live, not a stale in-process default after a restart.
        strategy_family = await pb.get_live_strategy_family()
        price_data = await (pb.get_price_rsi(session, ticker) if strategy_family == "mean_reversion" else pb.get_price_momentum(session, ticker))
        if price_data is None:
            reason = pb._price_rsi_last_failure.get(ticker, "unknown reason")
            raise HTTPException(status_code=503, detail=f"Could not fetch a live price/RSI for {ticker}: {reason} - try again")
        price, rsi, trend = price_data["price"], price_data["rsi"], price_data["trend"]
        sma20 = price_data.get("sma20") or price

        total_notional = sum(p.get("qty", 0) * p.get("entry", 0) for p in pb.open_prop_positions.values())
        is_valid, mandate_reason = pb.validate_entry(
            bot_name="prop_bot", symbol=contract, rsi=rsi, volume_ratio=1.0,
            buying_power=buying_power, open_positions=len(pb.open_prop_positions),
            total_notional=total_notional, equity=equity,
        )
        if not is_valid:
            raise HTTPException(status_code=400, detail=f"Mandate check failed: {mandate_reason}")

        # Reuses the exact same real gate function
        # (check_momentum_entry_gate / check_mean_reversion_entry_gate)
        # the automatic Pass 2 scan and the "Right now" eligibility
        # dry-run both call - covers momentum's price>SMA20 condition
        # (which validate_entry's mandate check above doesn't) plus
        # whichever variant (A/B/C/D - see get_live_entry_variant) is
        # currently promoted to live, so a manual click can never enter
        # something the live logic itself wouldn't.
        if strategy_family == "mean_reversion":
            gate_ok, gate_reason = pb.check_mean_reversion_entry_gate(rsi)
        else:
            live_variant = await pb.get_live_entry_variant()
            gate_ok, gate_reason = pb.check_momentum_entry_gate(price_data, live_variant)
        if not gate_ok:
            raise HTTPException(status_code=400, detail=f"Mandate check failed: {gate_reason}")

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
    did NOT want built. Updated alongside the live momentum-strategy swap:
    now goes through get_price_momentum (RSI + real SMA20) and the same
    price-above-SMA20 check manual_open_prop_position enforces, not the
    old RSI-oversold framing - nothing here is cached or estimated.

    Kill-condition and margin-safety are account-wide, not per-symbol, so
    they're checked once: if either fails, every symbol is reported
    ineligible with that one shared reason, matching how "Trade this"
    itself would fail identically on every symbol in that state."""
    if prop_bot_module is None:
        raise HTTPException(status_code=500, detail="prop_bot module not available")
    pb = prop_bot_module

    if os.getenv("STOP_TRADING", "false").lower() == "true":
        return {
            "tickers": {c["symbol"]: {"eligible": False, "reason": "STOP_TRADING is set - all entries paused", "rsi": None} for c in pb.FUTURES.values()},
            "strategy_family": await pb.get_live_strategy_family(),
        }

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

        # Which real strategy family is live right now - also re-syncs
        # bot_mandates.APEX_MANDATE["entry"] as a side effect, so this
        # preview's own mandate check always matches whichever family is
        # actually live.
        strategy_family = await pb.get_live_strategy_family()
        live_variant = await pb.get_live_entry_variant()
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
                reason = await pb.describe_symbol_exclusion_reason(ticker)
                results[ticker] = {"eligible": False, "reason": f"Excluded - {reason}", "rsi": None}
                continue

            price_data = await (pb.get_price_rsi(session, ticker) if strategy_family == "mean_reversion" else pb.get_price_momentum(session, ticker))
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
            if is_valid:
                # Reuses the exact same real gate function
                # (check_momentum_entry_gate / check_mean_reversion_entry_gate)
                # the automatic Pass 2 scan and manual_open_prop_position
                # both call - covers price>SMA20 plus whichever variant is
                # currently live (momentum only), so this preview can
                # never show a symbol as eligible that a real click would
                # actually refuse.
                if strategy_family == "mean_reversion":
                    is_valid, mandate_reason = pb.check_mean_reversion_entry_gate(rsi)
                else:
                    is_valid, mandate_reason = pb.check_momentum_entry_gate(price_data, live_variant)
            results[ticker] = {"eligible": is_valid, "reason": None if is_valid else mandate_reason, "rsi": rsi}

    return {"tickers": results, "strategy_family": strategy_family}


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
CHART_STOCK_SYMBOLS = {"SPY", "QQQ", "DIA", "IWM", "GLD", "USO", "SLV", "SH", "PSQ", "DOG", "RWM", "MSFT", "META", "AAPL", "GOOGL", "AMZN", "NVDA"}
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


# ============================================================================
# CRYPTO GRID BOT - real, live grid-trading branches (see crypto_grid_bot.py's
# own module docstring for the full real evidence/scoping). Per the account
# owner's direct "you have to do it C" after Strategy Lab's real A/B/C/D
# comparison showed Grid Bot as the clear best real performer.
# ============================================================================

@router.get("/grid-status", dependencies=[Depends(require_admin_key)])
async def get_grid_status_endpoint():
    if crypto_grid_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_grid_bot module not available")
    return await crypto_grid_bot_module.get_grid_status()


@router.get("/grid-status/trade-history", dependencies=[Depends(require_admin_key)])
async def get_grid_trade_history_endpoint():
    if crypto_grid_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_grid_bot module not available")
    return await crypto_grid_bot_module.get_grid_trade_history()


class SetGridBotModeRequest(BaseModel):
    enabled: bool


@router.post("/grid-status/mode", dependencies=[Depends(require_admin_key)])
async def set_grid_bot_mode_endpoint(payload: SetGridBotModeRequest):
    """The real master switch for the whole grid-branch system. Real
    branches can be created while the mode is off (so they're ready
    before flipping it on), but nothing trades until this is explicitly
    enabled - see crypto_grid_bot.is_grid_bot_active's own docstring for
    why it currently defaults to True."""
    if crypto_grid_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_grid_bot module not available")
    await crypto_grid_bot_module.set_grid_bot_active(payload.enabled)
    log.info(f"[dashboard] 🔲 Crypto grid bot mode {'ENABLED - real grid branches are now live' if payload.enabled else 'disabled'}")
    return {"status": "updated", "mode_active": payload.enabled}


class SetGridDynamicSpacingRequest(BaseModel):
    enabled: bool


@router.post("/grid-status/dynamic-spacing", dependencies=[Depends(require_admin_key)])
async def set_grid_dynamic_spacing_endpoint(payload: SetGridDynamicSpacingRequest):
    """Turns real fee-tier-aware dynamic grid spacing on or off - the
    live wiring for crypto_grid_bot.compute_dynamic_grid_pct, per the
    account owner's explicit "build both, backtest before going live."
    Off by default - see crypto_selection_backtest.py's
    run_grid_fee_tier_spacing_comparison for the real backtest evidence
    that should inform whether to turn this on. Takes effect on the live
    bot's very next cycle for every branch, no restart needed."""
    if crypto_grid_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_grid_bot module not available")
    await crypto_grid_bot_module.set_dynamic_spacing_active(payload.enabled)
    log.info(f"[dashboard] 🎯 Grid Bot fee-tier-aware dynamic spacing {'ENABLED' if payload.enabled else 'disabled'}")
    return {"status": "updated", "dynamic_spacing_active": payload.enabled}


class SetGridAvgSwingSpacingRequest(BaseModel):
    enabled: bool


@router.post("/grid-status/avg-swing-spacing", dependencies=[Depends(require_admin_key)])
async def set_grid_avg_swing_spacing_endpoint(payload: SetGridAvgSwingSpacingRequest):
    """Turns real average-swing-based dynamic grid spacing on or off - the
    live wiring for crypto_grid_bot.compute_avg_swing_grid_pct, per the
    account owner's own direct "make Grid Bot better" follow-up right
    after seeing crypto_selection_backtest.py's run_grid_atr_spacing_comparison
    show 1.5x avg swing beating today's fixed 1% by a wide margin on real
    30-day data. ON by default (see is_avg_swing_spacing_active's own
    docstring) - takes precedence over fee-tier spacing when both happen
    to be active. Takes effect on the live bot's very next cycle for
    every branch, no restart needed."""
    if crypto_grid_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_grid_bot module not available")
    await crypto_grid_bot_module.set_avg_swing_spacing_active(payload.enabled)
    log.info(f"[dashboard] 📏 Grid Bot average-swing-based dynamic spacing {'ENABLED' if payload.enabled else 'disabled'}")
    return {"status": "updated", "avg_swing_spacing_active": payload.enabled}


class SetGridAutoRotateRequest(BaseModel):
    enabled: bool


@router.post("/grid-status/auto-rotate", dependencies=[Depends(require_admin_key)])
async def set_grid_auto_rotate_endpoint(payload: SetGridAutoRotateRequest):
    """Turns real automatic idle-cash rotation on or off - per the
    account owner's explicit request that real idle cash should never
    just sit there, it should keep moving toward whichever real coin is
    currently doing well (see crypto_grid_bot.run_grid_auto_rotate_sweep).
    On by default - reuses the exact same real coin-ranking signal
    already live via the $20 Quick Buy button. Takes effect on the live
    bot's very next scheduled sweep, no restart needed."""
    if crypto_grid_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_grid_bot module not available")
    await crypto_grid_bot_module.set_grid_auto_rotate_active(payload.enabled)
    log.info(f"[dashboard] 🔁 Grid Bot automatic idle-cash rotation {'ENABLED' if payload.enabled else 'disabled'}")
    return {"status": "updated", "auto_rotate_active": payload.enabled}


class CreateGridBranchRequest(BaseModel):
    product_id: str
    allocated_usd: float


@router.post("/grid-status/create-branch", dependencies=[Depends(require_admin_key)])
async def create_grid_branch_endpoint(payload: CreateGridBranchRequest):
    """Creates a real new grid branch on the given real Coinbase product
    id with the given real dollar allocation - a pure bookkeeping
    operation plus one real live price fetch (to anchor its starting
    reference_price), never a trade by itself. Refuses a non-positive
    amount or a coin already claimed by another active grid branch."""
    if crypto_grid_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_grid_bot module not available")
    try:
        branch = await crypto_grid_bot_module.create_grid_branch(payload.product_id, payload.allocated_usd)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "status": "created", "bot_name": branch.bot_name, "product_id": branch.product_id,
        "allocated_usd": round(branch.allocated_usd, 2), "reference_price": branch.reference_price,
    }


class GridQuickBuyRequest(BaseModel):
    amount_usd: float = 20.0


@router.post("/grid-status/quick-buy", dependencies=[Depends(require_admin_key)])
async def grid_quick_buy_endpoint(payload: GridQuickBuyRequest):
    """The real $20 Quick Buy button - per the account owner's explicit
    request for a real 'put money in, it trades for me' button, after
    being shown why the BTC price-prediction panel couldn't back one (no
    proven directional edge, no real instrument to bet on) and offered
    Grid Bot instead (56.2% real backtested win rate).

    Creates a real new grid branch with the given amount on whichever
    coin currently ranks best by real backtested ROI (see
    crypto_grid_bot.pick_best_ranked_coin_for_grid) - never an instant
    market buy; the branch's own first real fill happens on its own next
    cycle, on a genuine 1% dip, same as every other grid branch."""
    if crypto_grid_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_grid_bot module not available")
    try:
        result = await crypto_grid_bot_module.quick_buy_best_coin(payload.amount_usd)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


class CreateMultipleGridBranchesRequest(BaseModel):
    count: int = 3
    amount_per_branch: float


@router.post("/grid-status/create-multiple-branches", dependencies=[Depends(require_admin_key)])
async def create_multiple_grid_branches_endpoint(payload: CreateMultipleGridBranchesRequest):
    """The real one-click "add several branches at once" shortcut - per
    the account owner's explicit "yes build the one-click add 3 branches
    shortcut," after real backtest evidence showed narrower grid spacing
    loses money and running more coins is the real lever for more trade
    frequency instead. Creates up to `count` real branches, each on a
    different real coin (same auto-pick the $20 Quick Buy button already
    uses) - see crypto_grid_bot.create_multiple_grid_branches for its own
    real partial-success behavior (stops and returns whatever it
    genuinely managed the moment one real attempt fails, never rolls
    back what already succeeded)."""
    if crypto_grid_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_grid_bot module not available")
    try:
        result = await crypto_grid_bot_module.create_multiple_grid_branches(payload.count, payload.amount_per_branch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    log.info(f"[dashboard] 🌱 Created {len(result['created'])}/{payload.count} real grid branches via the one-click shortcut")
    return result


class FundGridFromTreeRequest(BaseModel):
    from_bot_name: str
    amount: float
    product_id: str | None = None
    to_grid_bot_name: str | None = None


@router.post("/grid-status/fund-from-tree", dependencies=[Depends(require_admin_key)])
async def fund_grid_from_tree_endpoint(payload: FundGridFromTreeRequest):
    """Moves real, already-reserved cash from a FLAT family-tree branch
    directly into Grid Bot - built after the account owner's own direct
    request to put more real capital into Grid Bot right after a fresh
    Strategy Lab run confirmed it's the one strategy actually winning
    (+$81.23, 58.8% win rate on a real 34-coin sample), while the real
    family tree (Baseline "A") lost -$363.63 on the identical data.
    get_real_free_cash_usd() had genuinely gone negative - the family
    tree's own flat, idle allocation was itself the thing blocking Grid
    Bot from getting more real money, since nothing previously let that
    reserved-but-doing-nothing cash move across systems.

    `to_grid_bot_name`, if given, adds to that existing real grid branch;
    otherwise `product_id` (or an auto-pick by real backtested ROI if
    neither is given) creates a new one. Refuses if the source branch is
    currently holding a real position (must be flat), if the amount
    exceeds its real allocated_usd, or if STOP_TRADING is set. The
    destination is funded first; the source is only debited after that
    real fill/bookkeeping succeeds."""
    if crypto_grid_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_grid_bot module not available")
    try:
        result = await crypto_grid_bot_module.fund_grid_from_tree_branch(
            payload.from_bot_name, payload.amount, product_id=payload.product_id, to_grid_bot_name=payload.to_grid_bot_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


class WithdrawGridBranchRequest(BaseModel):
    amount: float


@router.post("/grid-status/{bot_name}/withdraw", dependencies=[Depends(require_admin_key)])
async def withdraw_grid_branch_endpoint(bot_name: str, payload: WithdrawGridBranchRequest):
    """Pulls real cash OUT of an existing grid branch's own allocation -
    the reverse of add_cash_to_grid_branch, and the direct sibling of
    fund_grid_from_tree_branch above. Built after the account owner's
    own direct request, looking at a real $994.65 STX-USD branch sitting
    completely flat: "can you make it to where I can pull some money out
    of this Branch... so I can make more."

    Requires the branch to be FLAT (no real open slices) - refuses
    otherwise, same real safety discipline as every other cash-moving
    function here. The withdrawn amount isn't sent anywhere - shrinking
    this branch's allocated_usd is itself what makes that real cash
    spendable again via get_real_free_cash_usd(), so the very next "New
    grid branch" click (or another fund-from-tree/add-cash call) can use
    it immediately. A withdrawal that drains the branch to essentially
    $0.00 deletes the row outright and releases its coin claim, rather
    than leaving a real empty stub behind."""
    if crypto_grid_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_grid_bot module not available")
    try:
        result = await crypto_grid_bot_module.withdraw_from_grid_branch(bot_name, payload.amount)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


class MoveCashBetweenGridBranchesRequest(BaseModel):
    from_bot_name: str
    amount: float
    to_bot_name: str | None = None
    product_id: str | None = None


@router.post("/grid-status/move-cash", dependencies=[Depends(require_admin_key)])
async def move_cash_between_grid_branches_endpoint(payload: MoveCashBetweenGridBranchesRequest):
    """One-step real grid-to-grid cash move - per the account owner's
    direct follow-up wanting the withdraw + redeploy flow combined into
    one modal with two sections: pick the source branch and amount, then
    pick either a different existing grid branch or a new one.

    `to_bot_name`, if given, adds to that existing real grid branch;
    otherwise `product_id` (or an auto-pick by real backtested
    ROI/BTC-relative-strength if neither is given) creates a new one.
    Refuses if the source is holding a real position (must be flat), if
    the amount exceeds its own real allocated_usd, if source and
    destination are the same branch, or if STOP_TRADING is set. The
    destination is funded first; the source is only debited after that
    real fill/bookkeeping succeeds."""
    if crypto_grid_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_grid_bot module not available")
    try:
        result = await crypto_grid_bot_module.move_cash_between_grid_branches(
            payload.from_bot_name, payload.amount, to_bot_name=payload.to_bot_name, product_id=payload.product_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


class SetGridBranchLockedRequest(BaseModel):
    locked: bool


@router.post("/grid-status/{bot_name}/lock", dependencies=[Depends(require_admin_key)])
async def set_grid_branch_locked_endpoint(bot_name: str, payload: SetGridBranchLockedRequest):
    """Real, manual per-branch lock - per the account owner's direct
    request after recalling losing real money moving cash off a branch
    that was "about to make profit" a few times in the past. A locked
    branch's real cash can never be pulled out by Withdraw, Move Cash (as
    a source), or the automatic auto-rotate sweep - its own normal grid
    trading (buying real dips, selling real rises) is completely
    unaffected either way."""
    if crypto_grid_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_grid_bot module not available")
    try:
        result = await crypto_grid_bot_module.set_grid_branch_locked(bot_name, payload.locked)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@router.get("/grid-status/{bot_name}/move-candidates", dependencies=[Depends(require_admin_key)])
async def get_grid_cash_move_candidates_endpoint(bot_name: str):
    """Real, read-only "would moving cash here actually help" preview for
    the Move Cash Between Grid Branches modal - per the account owner's
    direct request: "show me if I do move something to another Branch...
    will help it out and potentially push it to make money faster."
    Reports every other real active branch's own real backtested ROI
    (plus a real auto-picked new-branch option) so the account owner can
    compare against the source branch's own current coin before
    confirming a move. Never moves anything itself."""
    if crypto_grid_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_grid_bot module not available")
    try:
        result = await crypto_grid_bot_module.get_grid_cash_move_candidates(bot_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


class SetGridBranchActiveRequest(BaseModel):
    active: bool


@router.post("/grid-status/{bot_name}/active", dependencies=[Depends(require_admin_key)])
async def set_grid_branch_active_endpoint(bot_name: str, payload: SetGridBranchActiveRequest):
    """Pauses or resumes ONE specific real grid branch without touching
    the master switch or any other branch. A paused branch's own coin is
    also released back to real availability for a new branch (see
    get_grid_branch_claimed_coins, active-only) - it does NOT force-close
    any real open slices, matching the "never force a real position
    closed by a settings change" principle used everywhere else in this
    codebase; existing slices just sit until price naturally reaches
    them, or a real manual close is added later."""
    if crypto_grid_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_grid_bot module not available")
    from models import CryptoGridBranch
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoGridBranch).where(CryptoGridBranch.bot_name == bot_name))
        branch = result.scalar_one_or_none()
        if branch is None:
            raise HTTPException(status_code=404, detail=f"no grid branch named {bot_name!r}")
        branch.active = payload.active
        await db.commit()
    log.info(f"[dashboard] {'▶️ Resumed' if payload.active else '⏸️ Paused'} grid branch {bot_name}")
    return {"status": "updated", "bot_name": bot_name, "active": payload.active}


@router.post("/grid-status/close-all", dependencies=[Depends(require_admin_key)])
async def close_all_grid_slices_endpoint():
    """Real, one-way "close everything & take profit" - per the account
    owner's direct request for one button at the bottom of the Grid Bot
    section that sells every real open slice across every branch right
    now, instead of closing branches one at a time.

    Real, server-side re-check so this can't be triggered when the total
    isn't actually profitable just by calling the API directly: refuses
    (400) unless the real grand total across every branch's own "if sold
    right now" figure is genuinely positive at this exact moment -
    matching the same "only enabled when genuinely in profit right now,
    re-checked server-side too" principle the family tree's own
    root-take-profit button already established. A real live-price fetch
    failure for any branch makes the real total honestly unknown rather
    than a guess, and is refused the same way."""
    if crypto_grid_bot_module is None:
        raise HTTPException(status_code=500, detail="crypto_grid_bot module not available")
    status = await crypto_grid_bot_module.get_grid_status()
    total = status.get("total_unrealized_net_usd")
    if total is None:
        raise HTTPException(status_code=400, detail="Could not confirm the real total right now (a live price fetch failed) - refusing to close everything blind")
    if total <= 0:
        raise HTTPException(status_code=400, detail=f"Total unrealized P&L across all Grid Bot branches is ${total:.2f} right now - not currently profitable, refusing to close everything")
    result = await crypto_grid_bot_module.close_all_grid_slices()
    log.info(f"[dashboard] 🔒 Close-all triggered: {result['branches_closed']} branches, {result['slices_closed']} real slices, ${result['total_realized_pnl']:.2f} total realized")
    return result
