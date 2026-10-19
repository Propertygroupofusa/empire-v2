"""GRID_COIN_PICKER: ten coins, $15 pieces, each sold only at a profit after fees.

The owner, 2026-10-19: "Only buy and sell at a profit ... with every coin there is
... if you don't see that happening with any of the coins, do not touch it ... just
go to the next one and keep searching until you find the one that you can buy and
sell at a profit ... bounce around ... where don't mess up no other coins flow."
Asked whether to build this and how big a piece: "Build it", "$15".

WHAT IT DOES
  * Every 14 days it ranks every liquid Coinbase USD coin (at least $500k a day
    traded) on its own last 60 days: how much the rule below would have banked,
    among coins whose round trips mostly (70%+) closed inside 24 hours, with at
    least 6 of them. The top 10 are the picks. Coins the grid or the family tree
    claims, and coins on the no-sell list, are never picked.
  * A pick with no open piece buys $15 when its price is 5% or more under its
    24-hour high.
  * A piece sells only at or above its target: the price that nets 1% of its
    cost after both fee legs. No stop. A coin that drops out of the picks keeps
    its open piece until that piece sells.

WHAT IT WAS MEASURED ON (scratchpad allcoins/quick_profit2.py, 2026-10-19)
  91 coins, 2026-06-22 to 2026-10-19, picks made from the past only: $66 of profit
  per $100 kept in coins, against $43 for all 91 coins and $38 for the grid's own
  21. 86% of sales came inside 24 hours. At the end 15 pieces were still waiting,
  $70 under cost. Test dollars have run about twice what real trading made.

WHAT STILL APPLIES
  * The cash floor (GRID_CASH_RESERVE_USD) through the grid's spendable_for_slice,
    the $15 minimum, the 20% single-coin ceiling, maker-only with no market
    fallback, USD only (USDC is never converted for it), at most 20 open pieces.
  * profit_only.py and no_sell.py, as for every Coinbase seller.

HOW IT STAYS OUT OF THE WAY
  It is not a branch - 21 stays 21, and the picker is what grows. Its open pieces
  are registered in crypto_coin_claims, so the grid, the tree and coin adoption
  never take its coins, and it never buys a coin they claim. Unreadable claims
  mean no buy. It runs under STOP_TRADING, as GRID_SELL_ONLY does.

PRACTICE FIRST
  The owner, the same day: "avoid doing anything outside of the sandbox until your
  bot has proven it can run flawlessly without manual trade conflicts or code
  crashes." GRID_COIN_PICKER=paper runs every step above on live prices inside the
  real bot - picks, gates, claims - but places no order: a buy is recorded at the
  price it would have paid, a sale at its target, as paper pieces that never touch
  the wallet, the claims or the BTC buy's profit. Every pass and every failure is
  counted (coin_picker_passes_<mode>) so "ran clean" is a number, not a feeling.
  GRID_COIN_PICKER=true is real money. Anything else is off.
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from env_config import env_float

import no_sell
import profit_only

log = logging.getLogger("coin_picker")

SWITCH_ENV = "GRID_COIN_PICKER"
PIECE_USD = env_float("GRID_COIN_PICKER_USD", 15.0)
MIN_VIABLE_USD = 15.0           # the owner's viability floor; never lower
DIP = 0.05                      # buy 5% or more under the 24h high
NET = 0.01                      # sell where the piece nets 1% of its cost after fees
TOP_K = 10
RANK_DAYS = 60
REPICK_SECONDS = 14 * 86400
MIN_ROUND_TRIPS = 6
MIN_WITHIN_24H = 0.70
MIN_VOLUME_USD = 500_000
MAX_OPEN = 20
SIM_FEE = 0.007                 # the ranking's round trip: the account's maker rate, both legs
PASS_SECONDS = 60.0
STABLE = {"USDC", "USDT", "DAI", "PYUSD", "EURC", "GUSD", "PAX", "USDS", "FDUSD", "TUSD",
          "RLUSD", "USD1", "GYEN", "WBTC", "CBBTC", "CBETH"}
API = "https://api.exchange.coinbase.com"
HEADERS = {"User-Agent": "empire-v2-grid"}

_picks = []
_picked_at = 0.0
_rank_task = None
_next_pass_at = 0.0
_said = {}


def mode() -> str:
    """"live" for exactly "true", "paper" for exactly "paper", otherwise "off"
    (any case, surrounding spaces ignored)."""
    return {"true": "live", "paper": "paper"}.get((os.getenv(SWITCH_ENV) or "").strip().lower(), "off")


def enabled() -> bool:
    return mode() != "off"


# ------------------------------------------------------------------ pure rules

def target_price(entry, round_trip_fee_rate, net=NET):
    """The price at which selling nets `net` of cost after both fee legs:
    q(p - e) - q(e + p)r/2 = net*q*e  ->  p = e(1 + r/2 + net) / (1 - r/2)."""
    half = round_trip_fee_rate / 2
    return entry * (1 + half + net) / (1 - half)


def buy_line(high_24h, dip=DIP):
    return high_24h * (1 - dip)


def simulate(rows, dip=DIP, net=NET, fee=SIM_FEE):
    """Replay the rule on hourly candles [(t, open, high, low, close)], one piece at a
    time, no peeking: decided on closed candles, filled on later ones only.
    Returns (round trips, profit per $15 piece, share closed inside 24h)."""
    closed, pos, highs = [], None, []
    for t, o, h, l, c in rows:
        if pos is not None and t > pos[1] and h >= pos[2]:
            q = 15.0 / pos[0]
            closed.append(((t - pos[1]) / 3600,
                           q * (pos[2] - pos[0]) - q * (pos[0] + pos[2]) * fee / 2))
            pos = None
        if pos is None and len(highs) >= 24:
            line = buy_line(max(highs[-24:]), dip)
            if l <= line:
                ent = min(line, o)
                pos = (ent, t, target_price(ent, fee, net))
        highs.append(h)
        if len(highs) > 48:
            highs.pop(0)
    if not closed:
        return 0, 0.0, 0.0
    return (len(closed), sum(p for _, p in closed),
            sum(1 for hrs, _ in closed if hrs <= 24) / len(closed))


def choose(scores, k=TOP_K, min_n=MIN_ROUND_TRIPS, min_w24=MIN_WITHIN_24H):
    """scores: {product: (round trips, banked, share inside 24h)} -> the top k by
    banked among coins that turned over often and mostly inside a day."""
    ok = [(p, s) for p, s in scores.items() if s[0] >= min_n and s[2] >= min_w24]
    ok.sort(key=lambda x: -x[1][1])
    return [p for p, _ in ok[:k]]


# ------------------------------------------------------------ public market data

async def _get(session, url, timeout=15):
    import aiohttp
    try:
        async with session.get(url, headers=HEADERS,
                               timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status != 200:
                return None
            return await r.json(content_type=None)
    except Exception:
        return None


async def universe(session):
    """Liquid, tradeable USD coins. None when Coinbase could not be read."""
    prods, stats = await _get(session, f"{API}/products"), await _get(session, f"{API}/products/stats")
    if not isinstance(prods, list) or not isinstance(stats, dict):
        return None
    out = []
    for p in prods:
        if (p.get("quote_currency") != "USD" or p.get("status") != "online"
                or p.get("trading_disabled") or p.get("base_currency") in STABLE):
            continue
        s = (stats.get(p["id"]) or {}).get("stats_24hour") or {}
        try:
            vol = float(s.get("volume") or 0) * float(s.get("last") or 0)
        except (TypeError, ValueError):
            vol = 0.0
        if vol >= MIN_VOLUME_USD:
            out.append(p["id"])
    return out


async def hourly(session, pid, hours):
    """[(t, open, high, low, close)] oldest first, or None."""
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    end = int(time.time()) // 3600 * 3600
    rows = {}
    t = end - hours * 3600
    while t < end:
        t2 = min(t + 300 * 3600, end)
        d = await _get(session, f"{API}/products/{pid}/candles?granularity=3600"
                       f"&start={datetime.fromtimestamp(t, timezone.utc).strftime(stamp)}"
                       f"&end={datetime.fromtimestamp(t2, timezone.utc).strftime(stamp)}")
        if not isinstance(d, list):
            return None
        for r in d:
            if isinstance(r, list) and len(r) >= 5:
                rows[int(r[0])] = (int(r[0]), float(r[3]), float(r[2]), float(r[1]), float(r[4]))
        t = t2
        await asyncio.sleep(0.35)
    return [rows[k] for k in sorted(rows)]


async def price_now(session, pid):
    d = await _get(session, f"{API}/products/{pid}/ticker", timeout=10)
    try:
        px = float((d or {}).get("price"))
        return px if px > 0 else None
    except (TypeError, ValueError):
        return None


async def high_24h(session, pid):
    rows = await hourly(session, pid, 25)
    if not rows or len(rows) < 20:
        return None
    return max(r[2] for r in rows[-24:])


# ------------------------------------------------------------------- the record

async def open_pieces(paper=False):
    from sqlalchemy import select
    from database import get_session_factory
    from models import CoinPickerPiece
    async with get_session_factory()() as db:
        return list((await db.execute(select(CoinPickerPiece)
                                      .where(CoinPickerPiece.status == "open")
                                      .where(CoinPickerPiece.paper == bool(paper)))).scalars().all())


async def record_buy(pid, qty, px, rate, paper=False):
    from database import get_session_factory
    from models import CoinPickerPiece
    async with get_session_factory()() as db:
        db.add(CoinPickerPiece(product_id=pid, status="open", qty=qty, entry_price=px,
                               cost_usd=qty * px, target_price=target_price(px, rate),
                               round_trip_fee_rate=rate, paper=bool(paper)))
        await db.commit()


async def record_sale(piece_id, filled_qty, px, pnl):
    """Close the piece for what filled; a partial fill leaves the rest open."""
    from sqlalchemy import select
    from database import get_session_factory
    from models import CoinPickerPiece
    async with get_session_factory()() as db:
        row = (await db.execute(select(CoinPickerPiece)
                                .where(CoinPickerPiece.id == piece_id))).scalar_one()
        rest = (row.qty or 0.0) - filled_qty
        if rest > (row.qty or 0.0) * 1e-6:
            db.add(CoinPickerPiece(product_id=row.product_id, status="open", qty=rest,
                                   entry_price=row.entry_price, cost_usd=rest * row.entry_price,
                                   target_price=row.target_price,
                                   round_trip_fee_rate=row.round_trip_fee_rate,
                                   opened_at=row.opened_at, paper=row.paper))
        row.qty, row.cost_usd = filled_qty, filled_qty * (row.entry_price or 0.0)
        row.status, row.exit_price, row.pnl = "closed", px, pnl
        row.closed_at = datetime.utcnow()
        await db.commit()


async def banked_since(start_naive_utc):
    """Profit the picker's REAL sales booked after a moment - never a paper one.
    Raises when unreadable."""
    from sqlalchemy import func, select
    from database import get_session_factory
    from models import CoinPickerPiece
    async with get_session_factory()() as db:
        total = (await db.execute(
            select(func.coalesce(func.sum(CoinPickerPiece.pnl), 0.0))
            .where(CoinPickerPiece.status == "closed")
            .where(CoinPickerPiece.paper == False)  # noqa: E712 - SQL, not Python
            .where(CoinPickerPiece.closed_at > start_naive_utc))).scalar()
    return float(total or 0.0)


# ---------------------------------------------------------------------- passes

async def _claimed():
    """Coins the grid or the tree claims. None when either cannot be read - the
    picker does not buy blind into a coin another system may hold."""
    try:
        import crypto_coin_claims as claims
        return await claims.grid_claimed_products() | await claims.tree_claimed_products()
    except Exception as e:
        log.info(f"[PICKER] claims unreadable ({type(e).__name__}) - no buys this pass")
        return None


async def rank(session):
    """(picks, scores) from the past RANK_DAYS only. None when the inputs could not be read."""
    coins = await universe(session)
    claimed = await _claimed()
    if coins is None or claimed is None:
        return None
    scores = {}
    for pid in coins:
        if pid in claimed or no_sell.blocks(pid):
            continue
        rows = await hourly(session, pid, RANK_DAYS * 24)
        if rows and len(rows) >= 24 * 30:
            scores[pid] = simulate(rows)
    return choose(scores), scores


async def save_picks(picks, scores, at):
    from database import get_session_factory
    from models import CoinPickerPick
    when = datetime.fromtimestamp(at, timezone.utc).replace(tzinfo=None)
    async with get_session_factory()() as db:
        for i, pid in enumerate(picks, 1):
            n, banked, w24 = scores.get(pid, (0, 0.0, 0.0))
            db.add(CoinPickerPick(picked_at=when, rank=i, product_id=pid, round_trips=n,
                                  banked_per_piece=round(banked, 2), within_24h=round(w24, 3)))
        await db.commit()


async def load_latest_picks():
    """(picks, epoch picked) of the newest saved set, or ([], 0.0)."""
    from sqlalchemy import func, select
    from database import get_session_factory
    from models import CoinPickerPick
    async with get_session_factory()() as db:
        at = (await db.execute(select(func.max(CoinPickerPick.picked_at)))).scalar()
        if at is None:
            return [], 0.0
        rows = (await db.execute(select(CoinPickerPick).where(CoinPickerPick.picked_at == at)
                                 .order_by(CoinPickerPick.rank))).scalars().all()
    return [r.product_id for r in rows], at.replace(tzinfo=timezone.utc).timestamp()


async def _rank_and_store():
    global _picks, _picked_at
    import aiohttp
    try:
        if not _picks:
            # A restart reads the saved set back rather than re-ranking early.
            try:
                saved, at = await load_latest_picks()
            except Exception:
                saved, at = [], 0.0
            if saved and time.time() - at < REPICK_SECONDS:
                _picks, _picked_at = saved, at
                log.info(f"[PICKER] picks read back from {datetime.fromtimestamp(at, timezone.utc):%Y-%m-%d}: "
                         f"{', '.join(saved)}")
                return
        async with aiohttp.ClientSession() as session:
            ranked = await rank(session)
        if ranked is None:
            log.info("[PICKER] ranking inputs unreadable - keeping the previous picks")
            return
        picks, scores = ranked
        at = time.time()
        _picks, _picked_at = picks, at
        try:
            await save_picks(picks, scores, at)
        except Exception as e:
            log.warning(f"[PICKER] picks not saved ({type(e).__name__}) - kept in memory")
        msg = f"coin picker picks for the next 14 days: {', '.join(picks) or 'none qualified'}"
        log.warning(f"[PICKER] {msg}")
        await _feed("PICKS", "fleet", msg, throttle=False)
    except Exception as e:
        log.error(f"[PICKER] ranking failed ({type(e).__name__}: {e}) - keeping the previous picks")


async def _count(ok, which):
    """One pass (and one failure if not ok) on coin_picker_passes_<mode>:
    base_capital counts passes, starting_capital failures. Never raises."""
    try:
        from sqlalchemy import select
        from database import get_session_factory
        from models import TradingBotState
        key = f"coin_picker_passes_{which}"
        async with get_session_factory()() as db:
            row = (await db.execute(select(TradingBotState)
                                    .where(TradingBotState.bot_name == key))).scalar_one_or_none()
            if row is None:
                row = TradingBotState(bot_name=key, base_capital=0.0, starting_capital=0.0)
                db.add(row)
            row.base_capital = float(row.base_capital or 0.0) + 1.0
            if not ok:
                row.starting_capital = float(row.starting_capital or 0.0) + 1.0
            await db.commit()
    except Exception as e:
        log.warning(f"[PICKER] pass counter not written ({type(e).__name__})")


def _say(key, text):
    if _said.get(key) != text:
        _said[key] = text
        log.info(f"[PICKER] {text}")


async def maybe_trade(session, now=None):
    """One pass: refresh the picks in the background when due, sell what has reached
    its target, then at most one buy. Returns a short summary. Never raises."""
    global _rank_task, _next_pass_at
    which = mode()
    if which == "off":
        return {"on": False}
    now = time.time() if now is None else now
    if now < _next_pass_at:
        return {"on": True, "skipped": "read this minute already"}
    _next_pass_at = now + PASS_SECONDS
    paper = which == "paper"
    out = {"on": True, "mode": which, "sold": [], "bought": None}
    ok = await _pass(session, now, paper, out)
    await _count(ok, which)
    return out


async def _pass(session, now, paper, out):
    """The pass itself. True when it ran to the end, False when it failed."""
    global _rank_task
    tag = "PAPER_" if paper else ""
    word = "practice " if paper else ""
    try:
        import concentration_gate
        import crypto_btc_compound_bot as engine
        import crypto_grid_bot as grid

        if (not _picks or now - _picked_at > REPICK_SECONDS) and (
                _rank_task is None or _rank_task.done()):
            _rank_task = asyncio.create_task(_rank_and_store())

        try:
            pieces = await open_pieces(paper)
        except Exception as e:
            _say("record", f"piece record unreadable ({type(e).__name__}) - nothing this pass")
            return False
        rate = await grid.get_effective_round_trip_fee_rate()
        wait = await grid.maker_wait_seconds()

        # ---- sells: only at or above the target, and only at a profit
        held = set()
        for p in pieces:
            px = await price_now(session, p.product_id)
            if px is None or px < (p.target_price or float("inf")):
                held.add(p.product_id)
                continue
            ok, why = profit_only.verdict(p.qty, p.entry_price, px, rate)
            if not ok:
                held.add(p.product_id)
                _say(f"hold:{p.id}", f"{p.product_id} {why}")
                continue
            # Practice: a resting sell at the target fills AT the target.
            fill = ((p.qty, p.target_price) if paper else
                    await engine.place_maker_sell(session, p.qty, p.product_id, wait))
            if not fill:
                held.add(p.product_id)
                continue
            fq, fpx = fill
            pnl = profit_only.net_after_fees(fq, p.entry_price, fpx, rate)
            await record_sale(p.id, fq, fpx, pnl)
            if fq < p.qty * (1 - 1e-6):
                held.add(p.product_id)
            msg = (f"coin picker {word}sold {fq:.8g} {p.product_id} @ ${fpx:,.6g} - bought at "
                   f"${p.entry_price:,.6g}, {pnl:+.2f} profit after fees")
            log.warning(f"[PICKER] {msg}")
            await _feed(f"{tag}SELL", p.product_id, msg, throttle=False)
            out["sold"].append((p.product_id, round(pnl, 2)))

        # ---- buys: one per pass, a pick with no open piece, on a 5% dip
        if not _picks or len(held) >= MAX_OPEN:
            return True
        if PIECE_USD < MIN_VIABLE_USD:
            _say("size", f"GRID_COIN_PICKER_USD ${PIECE_USD:,.2f} is under the "
                         f"${MIN_VIABLE_USD:.0f} minimum - no buys")
            return True
        claimed = await _claimed()
        if claimed is None:
            return True
        for pid in _picks:
            if pid in held or pid in claimed or no_sell.blocks(pid):
                continue
            hi, px = await high_24h(session, pid), await price_now(session, pid)
            if hi is None or px is None or px > buy_line(hi):
                continue
            usd, usdc, err = await grid.wallet_cash(session)
            if usd is None:
                _say("cash", f"cash unreadable ({err}) - no buy")
                return True
            reserve, _ = await grid.unfunded_deployment_reserve()
            spend, why = grid.spendable_for_slice(PIECE_USD, usd + (usdc or 0.0),
                                                  deployment_reserve=reserve)
            if spend < PIECE_USD:
                await _feed("CASH_RESERVE", pid, f"coin picker: cash floor - {why}")
                return True
            if usd < spend:
                await _feed("CASH_RESERVE", pid, f"coin picker: USD ${usd:,.2f} is short of "
                                                 f"${spend:,.2f} - USDC is not converted for it")
                return True
            conc_ok, conc_why = concentration_gate.concentration_verdict(
                pid, await grid.account_market_book(), spend)
            if not conc_ok:
                await _feed("CONCENTRATION", pid, f"coin picker: {conc_why}")
                continue
            # Practice: a buy at the bid is taken at the price seen now.
            fill = ((spend / px, px) if paper else
                    await engine.place_maker_buy(session, spend, pid, wait))
            if not fill:
                _say(f"nofill:{pid}", f"maker buy of {pid} did not fill - tries again next pass")
                return True
            q, bpx = fill
            await record_buy(pid, q, bpx, rate, paper=paper)
            msg = (f"coin picker {word}bought {q:.8g} {pid} @ ${bpx:,.6g} (${q * bpx:,.2f}) - "
                   f"{(1 - px / hi) * 100:.1f}% under its 24h high; sells at "
                   f"${target_price(bpx, rate):,.6g} or better, never at a loss")
            log.warning(f"[PICKER] {msg}")
            await _feed(f"{tag}BUY", pid, msg, throttle=False)
            out["bought"] = (pid, round(q * bpx, 2))
            return True
        return True
    except Exception as e:
        log.error(f"[PICKER] pass failed ({type(e).__name__}: {e}) - nothing more this pass")
        await _feed("ERROR", "fleet", f"coin picker pass failed: {type(e).__name__}: {e}")
        return False


async def _feed(verdict_name, product_id, reason, throttle=True):
    """One activity-feed row, through the grid's own never-raising writer."""
    try:
        import crypto_grid_bot as grid
        if throttle and not grid._feed_should_write(f"PICKER_{verdict_name}", product_id):
            return
        await grid._record_gate_decision("coin_picker", product_id, verdict_name, reason)
    except Exception:
        pass
