"""The patient stock bot - practice first. Alpaca only; nothing here touches Coinbase.

The owner, 2026-10-26, after the stock bot lost money from Sep 21: "build the
patient version".

WHY THE OLD STOCK BOT LOST (Alpaca's own orders, /trades/closed, 256 round trips)
  * to Sep 20:        12 trades, +$8.28 - about one a day, held for days.
  * Sep 21 - Oct 14: 244 trades, -$52.45 - after the Sep 24-25 retune to "take
    profit faster": 5-minute checks, four positions, a 50% budget, $1 targets,
    plus stop-loss and 2-hour time-limit sells. Average win $0.37, average loss
    $0.54, 40% winners: a small loss every round.

WHAT THIS DOES INSTEAD - the rule that won the stock game (scratchpad
stockgame/game_targets.py, Nov 2023 - Oct 2026 hourly prices, 32 overlapping
60-day seasons, $480 in play, never a sale at a loss):
  * one look per HOUR, on the regular session (09:30-16:00 Eastern) only;
  * buy $120 of a symbol when its 14-hour RSI is under 40 (a dip), at most 4
    pieces open and one per symbol;
  * sell a piece only when it nets 5% after the 0.12% round trip. No stop-loss,
    no time limit, no giveback rule - nothing ever sells under the target.
  Measured: +$370.54 over 2.75 years against +$266.40 for simply holding SPY,
  24 of 32 seasons up, worst season -$40.43 (pieces still waiting, not sold).
  Every bigger target beat a smaller one; the small-target style lost. It mostly
  rides the market: in a long fall its four pieces just wait.

PRACTICE ONLY, FOR NOW. ALPACA_PATIENT=paper runs every step on Alpaca's real
hourly prices and records what it would have bought and sold - and places no
order: this file has no order path at all. Anything else is off. Going live is
a separate change, made only after practice has shown its numbers, because the
old bots must first be kept from trading or selling these pieces.

Practice runs whatever STOP_TRADING and ALPACA_STOP_TRADING say: it can
neither buy nor sell, and watching the market while trading is stopped is the
whole point of it.
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("alpaca_patient")

SWITCH_ENV = "ALPACA_PATIENT"
SYMBOLS = ["SPY", "QQQ", "DIA", "IWM", "GLD", "USO", "SLV",
           "MSFT", "META", "AAPL", "GOOGL", "AMZN", "NVDA"]   # the order slots fill in
PIECE_USD = 120.0
MAX_OPEN = 4
RSI_BUY_BELOW = 40.0
RSI_PERIOD = 14
NET = 0.05                      # sell where the piece nets 5% after the round trip
ROUND_TRIP_COST = 0.0012        # spread and slippage; Alpaca charges no commission
LOOKBACK_DAYS = 21              # calendar days of 30-minute bars read per symbol
PASS_SECONDS = 300.0
SETTLE_SECONDS = 180             # an hour is decided 3 minutes after it closes, once its bars are in
ET = ZoneInfo("America/New_York")
SESSION_OPEN = (9, 30)
SESSION_CLOSE = (16, 0)
DATA_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"

PASSES_KEY = "alpaca_patient_passes_paper"
LAST_BUCKET_KEY = "alpaca_patient_last_bucket"

_last_prices = {}                # symbol -> (bucket start epoch, close), for the status page
_last_pass = {}                  # what the newest pass saw and did


def mode() -> str:
    """"paper" for exactly "paper" (any case, spaces ignored), otherwise "off".
    There is no live mode in this file."""
    return "paper" if (os.getenv(SWITCH_ENV) or "").strip().lower() == "paper" else "off"


# ------------------------------------------------------------------ pure rules

def target_price(entry, cost=ROUND_TRIP_COST, net=NET):
    """The price at which selling nets `net` of the piece's cost after the round
    trip: q*p*(1 - c/2) - q*e*(1 + c/2) = net*q*e  ->  p = e(1 + c/2 + net)/(1 - c/2)."""
    half = cost / 2
    return entry * (1 + half + net) / (1 - half)


def net_after_costs(qty, entry, exit_price, cost=ROUND_TRIP_COST):
    half = cost / 2
    return qty * exit_price * (1 - half) - qty * entry * (1 + half)


def rsi(closes, n=RSI_PERIOD):
    """Wilder's RSI of the last close, or None with fewer than n+1 closes."""
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
    return 100.0 - 100.0 / (1.0 + ag / al) if al else 100.0


def _bucket_bounds(day):
    """The regular session's hourly buckets for one Eastern date, as
    (start, end) Eastern datetimes: 09:30, 10:30 ... 15:30 (a half hour)."""
    o = datetime(day.year, day.month, day.day, *SESSION_OPEN, tzinfo=ET)
    c = datetime(day.year, day.month, day.day, *SESSION_CLOSE, tzinfo=ET)
    out, s = [], o
    while s < c:
        out.append((s, min(s + timedelta(hours=1), c)))
        s += timedelta(hours=1)
    return out


def hourly_buckets(bars, now):
    """30-minute bars [(epoch, o, h, l, c)] -> CLOSED regular-session hourly
    buckets [(start epoch, o, h, l, c)], oldest first.

    Buckets start at :30 like the public hourly series the rule was tested on.
    Bars outside 09:30-16:00 Eastern are dropped, and a bucket still forming at
    `now` (epoch) is left out - a price this minute is not a closed hour."""
    groups = {}
    for t, o, h, l, c in sorted(bars):
        et = datetime.fromtimestamp(t, timezone.utc).astimezone(ET)
        for s, e in _bucket_bounds(et.date()):
            if s <= et < e:
                groups.setdefault((s, e), []).append((o, h, l, c))
                break
    closed = [(s, e) for s, e in sorted(groups) if e.timestamp() <= now]
    # The newest closed hour must be whole: a 30-minute bar not yet published
    # would make its first half look like the hour's close.
    if closed:
        s, e = closed[-1]
        if len(groups[(s, e)]) < (2 if e - s == timedelta(hours=1) else 1):
            closed = closed[:-1]
    out = []
    for s, e in closed:
        rows = groups[(s, e)]
        out.append((int(s.timestamp()), rows[0][0], max(r[1] for r in rows),
                    min(r[2] for r in rows), rows[-1][3]))
    return out


def plan(pieces, buckets_by_symbol, bucket=None):
    """One pass, as decisions. Changes nothing.

    pieces: open pieces as dicts {id, symbol, qty, entry_price, target_price,
    opened_at (bucket start epoch)}. buckets_by_symbol: closed hourly buckets.
    Returns (sales, buys): a sale is (piece, bucket start, price) at the target,
    from the first LATER bucket whose high reaches it; a buy is (symbol, bucket
    start, close, rsi) on the symbol's newest bucket - and, when `bucket` is
    given, only if that newest bucket IS `bucket`: a stale hour is never bought on.
    """
    sales, held = [], set()
    for p in pieces:
        hit = next(((t, p["target_price"]) for t, o, h, l, c in buckets_by_symbol.get(p["symbol"], [])
                    if t > p["opened_at"] and h >= p["target_price"]), None)
        if hit:
            sales.append((p, hit[0], hit[1]))
        else:
            held.add(p["symbol"])
    buys = []
    for sym in SYMBOLS:
        if len(held) + len(buys) >= MAX_OPEN:
            break
        if sym in held:
            continue
        bk = buckets_by_symbol.get(sym) or []
        if len(bk) <= RSI_PERIOD or (bucket is not None and bk[-1][0] != bucket):
            continue
        r = rsi([b[4] for b in bk])
        if r is not None and r < RSI_BUY_BELOW:
            buys.append((sym, bk[-1][0], bk[-1][4], r))
    return sales, buys


# ------------------------------------------------------------------ the record

async def open_pieces(paper=True):
    from sqlalchemy import select
    from database import get_session_factory
    from models import AlpacaPatientPiece
    async with get_session_factory()() as db:
        return list((await db.execute(select(AlpacaPatientPiece)
                                      .where(AlpacaPatientPiece.status == "open")
                                      .where(AlpacaPatientPiece.paper == bool(paper)))).scalars().all())


async def record_buy(symbol, bucket_start, price, rsi_value, paper=True):
    from database import get_session_factory
    from models import AlpacaPatientPiece
    qty = PIECE_USD / (price * (1 + ROUND_TRIP_COST / 2))
    async with get_session_factory()() as db:
        db.add(AlpacaPatientPiece(
            symbol=symbol, status="open", paper=bool(paper), qty=qty, entry_price=price,
            cost_usd=PIECE_USD, target_price=target_price(price), round_trip_cost=ROUND_TRIP_COST,
            entry_rsi=rsi_value,
            opened_at=datetime.fromtimestamp(bucket_start, timezone.utc).replace(tzinfo=None)))
        await db.commit()
    return qty


async def record_sale(piece_id, bucket_start, price):
    from sqlalchemy import select
    from database import get_session_factory
    from models import AlpacaPatientPiece
    async with get_session_factory()() as db:
        row = (await db.execute(select(AlpacaPatientPiece)
                                .where(AlpacaPatientPiece.id == piece_id))).scalar_one()
        row.status = "closed"
        row.exit_price = price
        row.pnl = net_after_costs(row.qty, row.entry_price, price, row.round_trip_cost or ROUND_TRIP_COST)
        row.closed_at = datetime.fromtimestamp(bucket_start, timezone.utc).replace(tzinfo=None)
        await db.commit()
        return row.pnl


async def _state(key):
    from sqlalchemy import select
    from database import get_session_factory
    from models import TradingBotState
    async with get_session_factory()() as db:
        return (await db.execute(select(TradingBotState)
                                 .where(TradingBotState.bot_name == key))).scalars().first()


async def _set_state(key, base=None, start=None, add_base=0.0, add_start=0.0):
    from sqlalchemy import select
    from database import get_session_factory
    from models import TradingBotState
    async with get_session_factory()() as db:
        row = (await db.execute(select(TradingBotState)
                                .where(TradingBotState.bot_name == key))).scalars().first()
        if row is None:
            row = TradingBotState(bot_name=key, base_capital=0.0, starting_capital=0.0)
            db.add(row)
        if base is not None:
            row.base_capital = base
        if start is not None:
            row.starting_capital = start
        row.base_capital = (row.base_capital or 0.0) + add_base
        row.starting_capital = (row.starting_capital or 0.0) + add_start
        await db.commit()


# ------------------------------------------------------------- Alpaca prices

def _headers():
    return {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", "")}


async def bars_30m(session, symbol, now):
    """Alpaca's 30-minute bars for the last LOOKBACK_DAYS, or None. Read-only GET."""
    import aiohttp
    start = datetime.fromtimestamp(now, timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    params = {"timeframe": "30Min", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "limit": "10000", "feed": "iex", "adjustment": "raw"}
    try:
        async with session.get(DATA_URL.format(symbol=symbol), headers=_headers(), params=params,
                               timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
    except Exception:
        return None
    rows = []
    for b in (data or {}).get("bars") or []:
        try:
            t = datetime.fromisoformat(str(b["t"]).replace("Z", "+00:00")).timestamp()
            rows.append((int(t), float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])))
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def newest_closed_bucket(now):
    """Start epoch of the newest regular-session bucket closed by `now`, or None."""
    day = datetime.fromtimestamp(now, timezone.utc).astimezone(ET).date()
    for back in range(0, 7):
        d = day - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        closed = [s for s, e in _bucket_bounds(d) if e.timestamp() <= now]
        if closed:
            return int(closed[-1].timestamp())
    return None


# ------------------------------------------------------------------ the pass

async def run_pass(session, now=None):
    """One practice pass. Returns a summary. Never raises."""
    now = time.time() if now is None else now
    out = {"mode": mode(), "sold": [], "bought": [], "skipped": None}
    if out["mode"] != "paper":
        out["skipped"] = "off"
        return out
    try:
        due = newest_closed_bucket(now - SETTLE_SECONDS)
        last = await _state(LAST_BUCKET_KEY)
        if due is None or (last is not None and (last.base_capital or 0) >= due):
            out["skipped"] = "no new closed hour"
            return out
        buckets = {}
        for sym in SYMBOLS:
            b = await bars_30m(session, sym, now)
            if b is None:
                out["skipped"] = f"{sym} prices unreadable - nothing decided this hour"
                await _set_state(PASSES_KEY, add_start=1.0)
                _last_pass.update(out, at=now)
                return out
            buckets[sym] = hourly_buckets(b, now - SETTLE_SECONDS)
            if buckets[sym]:
                _last_prices[sym] = (buckets[sym][-1][0], buckets[sym][-1][4])
            await asyncio.sleep(0.2)
        pieces = await open_pieces(paper=True)
        as_dicts = [{"id": p.id, "symbol": p.symbol, "qty": p.qty, "entry_price": p.entry_price,
                     "target_price": p.target_price,
                     "opened_at": int(p.opened_at.replace(tzinfo=timezone.utc).timestamp())}
                    for p in pieces]
        sales, buys = plan(as_dicts, buckets, bucket=due)
        for p, t, px in sales:
            pnl = await record_sale(p["id"], t, px)
            log.warning(f"[PATIENT] practice sold {p['qty']:.6g} {p['symbol']} @ ${px:,.2f} - "
                        f"bought at ${p['entry_price']:,.2f}, {pnl:+.2f} profit after costs")
            out["sold"].append((p["symbol"], round(pnl, 2)))
        for sym, t, px, r in buys:
            qty = await record_buy(sym, t, px, r)
            log.warning(f"[PATIENT] practice bought {qty:.6g} {sym} @ ${px:,.2f} ($120) - RSI {r:.1f}; "
                        f"sells at ${target_price(px):,.2f} or better, never at a loss")
            out["bought"].append((sym, round(px, 2), round(r, 1)))
        await _set_state(LAST_BUCKET_KEY, base=float(due))
        await _set_state(PASSES_KEY, add_base=1.0)
        _last_pass.update(out, at=now, bucket=due)
        return out
    except Exception as e:
        log.error(f"[PATIENT] pass failed ({type(e).__name__}: {e})")
        try:
            await _set_state(PASSES_KEY, add_start=1.0)
        except Exception:
            pass
        out["skipped"] = f"failed: {type(e).__name__}"
        _last_pass.update(out, at=now)
        return out


async def run_forever():
    """The practice loop. Started by main.py only when ALPACA_PATIENT=paper."""
    import aiohttp
    log.warning("[PATIENT] practice loop started - no orders are placed by this module")
    async with aiohttp.ClientSession() as session:
        while mode() == "paper":
            await run_pass(session)
            await asyncio.sleep(PASS_SECONDS)
    log.warning("[PATIENT] ALPACA_PATIENT is no longer paper - practice loop stopped")


def last_pass():
    return dict(_last_pass)


def last_prices():
    return dict(_last_prices)
