"""The patient stock bot - LIVE. Alpaca only; nothing here touches Coinbase.

The owner, 2026-10-27: "Yes build it" - the live switch, built switched off.
ALPACA_PATIENT=live turns it on; paper, blank or anything else leaves it off.

THIS IS THE ONLY FILE THAT SENDS A PATIENT ORDER. alpaca_patient.py holds the
rule and the practice; this file runs the same rule with real orders.

HOW IT TRADES
  * BUY: on the hour that just closed, a symbol whose 14-hour RSI is under 40
    (alpaca_patient.pick_buys - the rule that won the stock game), with a free
    slot (4 pieces at most, one per symbol). A DAY LIMIT buy of $120 at that
    hour's close plus BUY_LIMIT_SLACK. Never a market order. A buy not filled
    in BUY_WAIT_SECONDS is cancelled; whatever did fill becomes the piece, at
    Alpaca's own fill price.
  * SELL: a DAY LIMIT sell of the whole piece at its target ROUNDED UP to the
    cent - the price that nets 5% after the round trip. A limit sell cannot
    fill under its price, so nothing sells under the target, ever. No stop,
    no time limit, no market sell, no position close. The order rests all
    day; if it has not filled by the close it expires and is placed again
    the next trading day.
  * NEVER ON THE BUY DAY: the first sell goes out the trading day after the
    buy, so this bot never makes a day trade (the pattern-day-trader rule).
  * CASH ONLY: a buy needs Alpaca `cash` - not buying power - to cover it and
    every buy still waiting. No margin, no leverage.
  * ONLY WHILE THE MARKET IS OPEN, by Alpaca's own clock.

WHY IT SENDS NOTHING UNLESS THE OLD STOCK BOTS ARE STOPPED
  prop_bot.py adopts any Alpaca position on its symbols it is not tracking
  (reconcile_positions_with_broker) and puts its own exits on it, and every
  patient symbol is one of its symbols. The account-wide auto-close sells any
  position at +8% or after 10 days. Those two, the swing bot, the wheel and
  market_brain all stand still while alpaca_stop.alpaca_stopped() is true -
  so this file sends an order only while it is true. If the old bots are
  ever let run, it sends nothing and says why on /alpaca-patient.
  It also never buys a symbol Alpaca already holds outside this book (today
  MSFT, NVDA and SLV from the old bot) or one with an open order it did not
  send: Alpaca keeps one position per symbol, and two owners of one position
  is the bug this avoids.

ONE SENDER. Only the process holding the live lease (TradingBotState row
LEASE_KEY, renewed every pass) sends orders, so two copies of this app can
never double-buy.

TO STOP IT: set ALPACA_PATIENT to paper (or remove it). STOP_TRADING does not
stop it - STOP_TRADING is what keeps the old bots off its pieces. Anything it
has open stays open, with its resting sell at the target.
"""
import asyncio
import logging
import os
import random
import time
import uuid
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal

import alpaca_patient as ap
import alpaca_stop

log = logging.getLogger("alpaca_patient_live")

BUY_LIMIT_SLACK = 0.002          # a buy may fill up to 0.2% above the decided close, never more
BUY_WAIT_SECONDS = 900           # an unfilled buy is cancelled after 15 minutes
SENDING_GRACE_SECONDS = 120      # an order Alpaca has never heard of after this never arrived
QTY_EPS = 1e-9
ENTRY_TAG = "patient_entry"      # client_order_id prefixes, read by the dashboard's _order_source
EXIT_TAG = "patient_exit"
TERMINAL = ("filled", "canceled", "expired", "rejected")
PASSES_KEY = "alpaca_patient_passes_live"
LAST_BUCKET_KEY = "alpaca_patient_last_bucket_live"
LEASE_KEY = "alpaca_patient_live_lease"
LEASE_SECONDS = 900
PRICE_READ_PAUSE = 0.2           # between the 13 symbols' price reads

_ME = float(random.SystemRandom().randrange(1, 2 ** 52))   # this process, for the lease
_last_pass = {}
_last_prices = {}                # symbol -> (bucket start epoch, close)


# ------------------------------------------------------------------ the switch

def blocker():
    """Why live may not send an order right now, or None."""
    if ap.mode() != "live":
        return "off"
    if not alpaca_stop.alpaca_stopped():
        return ("the old stock bots are running (Alpaca's off switch is not set) - they would "
                "take over these pieces, so live sends nothing")
    if not (os.getenv("ALPACA_API_KEY") and os.getenv("ALPACA_SECRET_KEY")):
        return "Alpaca keys are not set"
    return None


# ------------------------------------------------------------------ prices and sizes

def qty_str(q):
    """Alpaca's fractional quantity: 9 decimals at most, rounded DOWN so it
    never asks for more than there is; fixed-point, trailing zeros trimmed.
    None when nothing is left."""
    try:
        d = Decimal(repr(float(q))).quantize(Decimal("1e-9"), rounding=ROUND_DOWN)
    except (TypeError, ValueError, ArithmeticError):
        return None
    if d <= 0:
        return None
    return format(d, "f").rstrip("0").rstrip(".")


def buy_limit(close):
    """The decided close plus BUY_LIMIT_SLACK, to the cent."""
    return float(Decimal(repr(close * (1 + BUY_LIMIT_SLACK))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def sell_limit(target):
    """The target rounded UP to the cent: a limit sell there cannot fill under the target."""
    return float(Decimal(repr(float(target))).quantize(Decimal("0.01"), rounding=ROUND_CEILING))


def _when(iso, now):
    """Alpaca's timestamp as naive UTC, or `now` when it has none."""
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return datetime.fromtimestamp(now, timezone.utc).replace(tzinfo=None)


def _epoch(naive_utc):
    return naive_utc.replace(tzinfo=timezone.utc).timestamp()


# ------------------------------------------------------------------ Alpaca

def _base():
    return os.getenv("ALPACA_BASE_URL", "https://api.alpaca.markets").rstrip("/")


async def _api(session, method, path, body=None):
    """(HTTP status, parsed JSON). (None, None) when no answer came back - for a
    POST that means the order may or may not have arrived."""
    import aiohttp
    headers = dict(ap._headers(), **{"Content-Type": "application/json"})
    try:
        async with session.request(method, _base() + path, headers=headers, json=body,
                                   timeout=aiohttp.ClientTimeout(total=20)) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = None
            return r.status, data
    except Exception as e:
        log.warning(f"[PATIENT-LIVE] {method} {path.split('?')[0]} got no answer ({type(e).__name__})")
        return None, None


async def market_open(session):
    """True or False by Alpaca's clock, None when unreadable."""
    status, data = await _api(session, "GET", "/v2/clock")
    if status != 200 or not isinstance(data, dict) or "is_open" not in data:
        return None
    return bool(data["is_open"])


async def account_cash(session):
    status, data = await _api(session, "GET", "/v2/account")
    try:
        return float(data["cash"]) if status == 200 else None
    except (TypeError, KeyError, ValueError):
        return None


async def positions_by_symbol(session):
    """{symbol: position} for every non-zero Alpaca position, or None."""
    status, data = await _api(session, "GET", "/v2/positions")
    if status != 200 or not isinstance(data, list):
        return None
    out = {}
    for p in data:
        try:
            if p.get("symbol") and float(p.get("qty") or 0) != 0:
                out[p["symbol"]] = p
        except (TypeError, ValueError):
            return None
    return out


async def open_orders(session):
    """Every open Alpaca order on the account, or None."""
    status, data = await _api(session, "GET", "/v2/orders?status=open&limit=500")
    return data if status == 200 and isinstance(data, list) else None


# ------------------------------------------------------------------ the record

async def live_pieces():
    return await ap.open_pieces(paper=False)


async def active_orders():
    from sqlalchemy import select
    from database import get_session_factory
    from models import AlpacaPatientOrder
    async with get_session_factory()() as db:
        return list((await db.execute(select(AlpacaPatientOrder)
                                      .where(AlpacaPatientOrder.status.in_(("sending", "sent"))))).scalars().all())


async def new_order(**fields):
    from database import get_session_factory
    from models import AlpacaPatientOrder
    async with get_session_factory()() as db:
        row = AlpacaPatientOrder(**fields)
        db.add(row)
        await db.commit()
        return row.id


async def update_order(order_id, **fields):
    from sqlalchemy import select
    from database import get_session_factory
    from models import AlpacaPatientOrder
    async with get_session_factory()() as db:
        row = (await db.execute(select(AlpacaPatientOrder).where(AlpacaPatientOrder.id == order_id))).scalar_one()
        for k, v in fields.items():
            setattr(row, k, v)
        row.updated_at = datetime.utcnow()
        await db.commit()


async def open_live_piece(symbol, qty, entry, rsi_value, opened_at):
    """A real piece from a real buy fill. Returns its id."""
    from database import get_session_factory
    from models import AlpacaPatientPiece
    async with get_session_factory()() as db:
        row = AlpacaPatientPiece(symbol=symbol, status="open", paper=False, qty=qty, entry_price=entry,
                                 cost_usd=qty * entry, target_price=ap.target_price(entry),
                                 round_trip_cost=ap.ROUND_TRIP_COST, entry_rsi=rsi_value, opened_at=opened_at)
        db.add(row)
        await db.commit()
        return row.id


async def close_live_piece(piece_id, sold_qty, exit_price, closed_at):
    """Book a real sell fill against its piece. Returns the profit in dollars:
    what the shares sold for less what they cost, at Alpaca's own fills.
    A part-fill is split off as its own closed row; the open row keeps the rest."""
    from sqlalchemy import select
    from database import get_session_factory
    from models import AlpacaPatientPiece
    async with get_session_factory()() as db:
        row = (await db.execute(select(AlpacaPatientPiece).where(AlpacaPatientPiece.id == piece_id))).scalar_one()
        sold = min(float(sold_qty), row.qty)
        pnl = sold * exit_price - sold * row.entry_price
        if sold >= row.qty - QTY_EPS:
            row.status, row.exit_price, row.pnl, row.closed_at = "closed", exit_price, pnl, closed_at
        else:
            db.add(AlpacaPatientPiece(symbol=row.symbol, status="closed", paper=False, qty=sold,
                                      entry_price=row.entry_price, cost_usd=sold * row.entry_price,
                                      target_price=row.target_price, round_trip_cost=row.round_trip_cost,
                                      entry_rsi=row.entry_rsi, opened_at=row.opened_at, closed_at=closed_at,
                                      exit_price=exit_price, pnl=pnl))
            rest = float(Decimal(repr(row.qty)) - Decimal(repr(sold)))
            row.qty, row.cost_usd = rest, rest * row.entry_price
        await db.commit()
        return pnl


async def hold_lease(now):
    """True when this process holds the live lease - taken if free or expired,
    renewed if already ours. Only the holder sends orders."""
    from sqlalchemy import or_, select, update
    from database import get_session_factory
    from models import TradingBotState
    async with get_session_factory()() as db:
        res = await db.execute(update(TradingBotState)
                               .where(TradingBotState.bot_name == LEASE_KEY)
                               .where(or_(TradingBotState.base_capital < now,
                                          TradingBotState.starting_capital == _ME))
                               .values(base_capital=now + LEASE_SECONDS, starting_capital=_ME))
        await db.commit()
        if res.rowcount == 1:
            return True
        if (await db.execute(select(TradingBotState.id).where(TradingBotState.bot_name == LEASE_KEY))).first():
            return False
        db.add(TradingBotState(bot_name=LEASE_KEY, base_capital=now + LEASE_SECONDS, starting_capital=_ME))
        try:
            await db.commit()
            return True
        except Exception:
            await db.rollback()
            return False


# ------------------------------------------------------------------ orders

async def send(session, out, side, symbol, qty, limit, now, piece_id=None, rsi_value=None, bucket=None):
    """One DAY LIMIT order, recorded before it goes out. True when Alpaca took
    it, False when it refused, None when no answer came back (the record stays
    "sending" and the next pass looks it up by client_order_id)."""
    coid = f"{ENTRY_TAG if side == 'buy' else EXIT_TAG}-{symbol}-{uuid.uuid4().hex[:16]}"
    oid = await new_order(client_order_id=coid, symbol=symbol, side=side, status="sending", qty=float(qty),
                          limit_price=limit, piece_id=piece_id, entry_rsi=rsi_value,
                          bucket_start=(datetime.fromtimestamp(bucket, timezone.utc).replace(tzinfo=None)
                                        if bucket is not None else None),
                          created_at=datetime.fromtimestamp(now, timezone.utc).replace(tzinfo=None))
    body = {"symbol": symbol, "qty": qty, "side": side, "type": "limit", "time_in_force": "day",
            "limit_price": f"{limit:.2f}", "client_order_id": coid}
    status, data = await _api(session, "POST", "/v2/orders", body)
    if status is None:
        out["problems"].append(f"{side} {symbol}: no answer from Alpaca - looked up next pass")
        return None
    if status in (200, 201) and isinstance(data, dict):
        await update_order(oid, status="sent", alpaca_order_id=data.get("id"), alpaca_status=data.get("status"))
        log.warning(f"[PATIENT-LIVE] {side} {qty} {symbol} limit ${limit:,.2f} sent")
        out["sent"].append((side, symbol, qty, limit))
        return True
    why = (data or {}).get("message") if isinstance(data, dict) else None
    await update_order(oid, status="done", alpaca_status="rejected", note=f"HTTP {status}: {why}"[:250])
    log.error(f"[PATIENT-LIVE] {side} {qty} {symbol} limit ${limit:,.2f} REFUSED (HTTP {status}): {why}")
    out["problems"].append(f"{side} {symbol} refused by Alpaca: {why}")
    return False


async def follow_up(session, out, now):
    """Read back every order still open in the record, and book what filled."""
    for o in await active_orders():
        status, data = await _api(session, "GET", f"/v2/orders:by_client_order_id?client_order_id={o.client_order_id}")
        age = now - _epoch(o.created_at) if o.created_at else 0.0
        if status == 404:
            if o.status == "sending" and age >= SENDING_GRACE_SECONDS:
                await update_order(o.id, status="done", alpaca_status="never_arrived",
                                   note="Alpaca has no such order - the send never arrived")
            elif o.status == "sent":
                out["problems"].append(f"{o.side} {o.symbol}: Alpaca no longer finds order {o.client_order_id}")
            continue
        if status != 200 or not isinstance(data, dict):
            out["problems"].append(f"{o.side} {o.symbol}: order unreadable - looked at again next pass")
            continue
        st = str(data.get("status") or "")
        try:
            filled = float(data.get("filled_qty") or 0)
            avg = float(data.get("filled_avg_price") or 0)
        except (TypeError, ValueError):
            out["problems"].append(f"{o.side} {o.symbol}: fill unreadable")
            continue
        fields = {"status": "sent", "alpaca_order_id": data.get("id"), "alpaca_status": st,
                  "filled_qty": filled, "filled_avg_price": avg or None}
        if st in TERMINAL:
            fields["status"] = "done"
            when = _when(data.get("filled_at") or data.get("updated_at"), now)
            if filled > 0 and avg > 0 and o.side == "buy":
                fields["piece_id"] = await open_live_piece(o.symbol, filled, avg, o.entry_rsi, when)
                log.warning(f"[PATIENT-LIVE] bought {filled:.9g} {o.symbol} @ ${avg:,.2f}; "
                            f"sells at ${sell_limit(ap.target_price(avg)):,.2f} or better, from the next trading day")
                out["filled"].append((o.symbol, filled, avg))
            elif filled > 0 and avg > 0 and o.side == "sell" and o.piece_id is not None:
                pnl = await close_live_piece(o.piece_id, filled, avg, when)
                log.warning(f"[PATIENT-LIVE] sold {filled:.9g} {o.symbol} @ ${avg:,.2f}: {pnl:+.2f} profit")
                out["sold"].append((o.symbol, round(pnl, 2)))
            await update_order(o.id, **fields)
            continue
        await update_order(o.id, **fields)
        if o.side == "buy" and age >= BUY_WAIT_SECONDS and st != "pending_cancel" and data.get("id"):
            # Our own unfilled buy, past its wait: withdrawn. What filled is
            # booked when Alpaca reports it canceled.
            cstatus, _ = await _api(session, "DELETE", f"/v2/orders/{data['id']}")
            if cstatus in (200, 204):
                out["cancelled"].append(o.symbol)
            else:
                out["problems"].append(f"buy {o.symbol}: cancel not taken (HTTP {cstatus})")


async def place_sells(session, out, now):
    """A resting limit sell at the target for every piece bought before today
    that has none."""
    pieces = await live_pieces()
    selling = {o.piece_id for o in await active_orders() if o.side == "sell"}
    today = ap.et_date(now)
    due = [p for p in pieces if p.id not in selling and ap.et_date(_epoch(p.opened_at)) < today]
    if not due:
        return
    held = await positions_by_symbol(session)
    if held is None:
        out["problems"].append("positions unreadable - no sell placed this pass")
        return
    for p in due:
        pos = held.get(p.symbol) or {}
        try:
            avail = float(pos.get("qty_available", pos.get("qty")) or 0)
        except (TypeError, ValueError):
            avail = 0.0
        q = qty_str(p.qty)
        if q is None or avail + QTY_EPS < float(q):
            out["problems"].append(f"{p.symbol}: Alpaca shows {avail:.9g} free, the piece is {p.qty:.9g} - not sold")
            continue
        await send(session, out, "sell", p.symbol, q, sell_limit(p.target_price), now, piece_id=p.id)


async def decide_buys(session, out, now):
    """Once per closed hour: the rule's buys, as limit orders."""
    due = ap.newest_closed_bucket(now - ap.SETTLE_SECONDS)
    last = await ap._state(LAST_BUCKET_KEY)
    if due is None or (last is not None and (last.base_capital or 0) >= due):
        return "no new closed hour"
    buckets = {}
    for sym in ap.SYMBOLS:
        b = await ap.bars_30m(session, sym, now)
        if b is None:
            await ap._set_state(PASSES_KEY, add_start=1.0)
            return f"{sym} prices unreadable - nothing decided this hour"
        buckets[sym] = ap.hourly_buckets(b, now - ap.SETTLE_SECONDS)
        if buckets[sym]:
            _last_prices[sym] = (buckets[sym][-1][0], buckets[sym][-1][4])
        await asyncio.sleep(PRICE_READ_PAUSE)
    pieces = await live_pieces()
    buying = [o for o in await active_orders() if o.side == "buy"]
    mine = {p.symbol for p in pieces} | {o.symbol for o in buying}
    n_open = len(pieces) + len(buying)
    if n_open < ap.MAX_OPEN and ap.pick_buys(mine, n_open, buckets, bucket=due):
        held, orders, cash = (await positions_by_symbol(session), await open_orders(session),
                              await account_cash(session))
        if held is None or orders is None or cash is None:
            await ap._set_state(PASSES_KEY, add_start=1.0)
            return "account unreadable - nothing decided this hour"
        mine_coids = {o.client_order_id for o in buying}
        others = {s for s in held if s not in mine} | {o.get("symbol") for o in orders
                                                       if o.get("client_order_id") not in mine_coids
                                                       and not str(o.get("client_order_id") or "").startswith(EXIT_TAG)}
        out["not_mine"] = sorted(s for s in others if s in ap.SYMBOLS)
        waiting = sum((o.qty or 0) * (o.limit_price or 0) for o in buying)
        for sym, t, close, r in ap.pick_buys(mine | others, n_open, buckets, bucket=due):
            limit = buy_limit(close)
            q = qty_str(ap.PIECE_USD / limit)
            if q is None:
                continue
            cost = float(q) * limit
            if cash - waiting < cost:
                out["problems"].append(f"buy {sym} skipped: ${cash - waiting:,.2f} cash free, ${cost:,.2f} needed")
                break
            if await send(session, out, "buy", sym, q, limit, now, rsi_value=r, bucket=t) is not False:
                waiting += cost
    await ap._set_state(LAST_BUCKET_KEY, base=float(due))
    await ap._set_state(PASSES_KEY, add_base=1.0)
    out["bucket"] = due
    return None


# ------------------------------------------------------------------ the pass

async def run_pass(session, now=None):
    """One live pass. Returns a summary. Never raises."""
    now = time.time() if now is None else now
    out = {"mode": ap.mode(), "sent": [], "filled": [], "sold": [], "cancelled": [], "problems": [],
           "skipped": None}
    why = blocker()
    if why:
        out["skipped"] = why
        if why != "off":
            _last_pass.clear()
            _last_pass.update(out, at=now)
        return out
    try:
        if not await hold_lease(now):
            out["skipped"] = "another copy of the app holds the live lease - it sends the orders"
        else:
            await follow_up(session, out, now)
            is_open = await market_open(session)
            if is_open is not True:
                out["skipped"] = "market closed" if is_open is False else "market clock unreadable"
            else:
                await place_sells(session, out, now)
                out["skipped"] = await decide_buys(session, out, now)
    except Exception as e:
        log.error(f"[PATIENT-LIVE] pass failed ({type(e).__name__}: {e})")
        try:
            await ap._set_state(PASSES_KEY, add_start=1.0)
        except Exception:
            pass
        out["skipped"] = f"failed: {type(e).__name__}"
    _last_pass.clear()
    _last_pass.update(out, at=now)
    return out


async def run_forever():
    """The live loop. Started by main.py only when ALPACA_PATIENT=live."""
    import aiohttp
    log.warning("[PATIENT-LIVE] live loop started - limit orders only, never a sale under the target")
    async with aiohttp.ClientSession() as session:
        while ap.mode() == "live":
            await run_pass(session)
            await asyncio.sleep(ap.PASS_SECONDS)
    log.warning("[PATIENT-LIVE] ALPACA_PATIENT is no longer live - live loop stopped")


def last_pass():
    return dict(_last_pass)


def last_prices():
    return dict(_last_prices)
