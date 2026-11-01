"""The coin keeper - PRACTICE. Coinbase only; nothing here touches Alpaca.

The owner, 2026-10-31: "Do the practice mode for the leftover coins."

THE LEFTOVERS. Counted coin by coin, the grid (its open slices) and the coin
picker (its open pieces) already hold a profit-only sell price on nearly every
unit they own. What has none is the units ABOVE what they hold - the extra
NEAR, HBAR and ALGO, and small coins no bot ever bought (RNDR, ETC, PUMP,
HOPR, APT, CRO, ARB ...): about $400 of coin with nothing that would ever sell
it at a profit.

WHAT THIS DOES, every PASS_SECONDS, with COIN_KEEPER=paper:
  * reads every coin Coinbase holds WITH Coinbase's own cost for it
    (GET /api/v3/brokerage/portfolios/{uuid}: average_entry_price and
    cost_basis - the higher of the two per unit, so a fee inside the cost is
    never left out);
  * subtracts the units the grid's open slices and the coin picker's open
    pieces already hold - the leftover is what neither of them will sell;
  * keeps a leftover only if it is worth at least MIN_VIABLE_USD (the owner's
    $15 viability floor - never lower), Coinbase knows its cost, it is not on
    the no-sell list (BTC is never sold), and no tree branch claims the coin;
  * gives each one a practice piece with ONE profit-only target: the price
    that nets NET (5%) of its cost after the WORST-CASE fee (SELL_FEE, the
    taker rate), so the target is a profit even if a live sale paid taker;
  * when the public price reaches the target, records a practice sale AT the
    target. Nothing is ever recorded under it.

PRACTICE ONLY. This file has no order path: it cannot sell, buy, cancel or
move anything. Anything but exactly "paper" is off. A practice sale is not
money - the coin is still in the account - so an asset sold in practice is
never taken in again, or practice would "sell" the same coin forever.
If the real leftover shrinks under a practice piece (the grid's adoption took
the units, or they were sold by hand), the piece is closed as "gone" with no
profit or loss.
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timezone

log = logging.getLogger("coin_keeper")

SWITCH_ENV = "COIN_KEEPER"
NET = 0.05                       # the target nets 5% of the piece's cost
SELL_FEE = 0.012                 # worst case: Coinbase's taker rate on the sale
MIN_VIABLE_USD = 15.0            # the owner's viability floor - never lower
GONE_TOLERANCE = 0.98            # real leftover under 98% of a piece's units: the piece is gone
PASS_SECONDS = 900.0
HOST = "api.coinbase.com"
PUBLIC_API = "https://api.exchange.coinbase.com"
PUBLIC_HEADERS = {"User-Agent": "empire-v2-keeper"}
CASH = {"USD", "USDC", "USDT", "DAI", "PYUSD", "EURC", "GUSD", "PAX", "USDS", "FDUSD", "TUSD"}

_last_pass = {}
_last_prices = {}


def mode() -> str:
    """"paper" for exactly "paper" (any case, spaces ignored), otherwise "off"."""
    return "paper" if (os.getenv(SWITCH_ENV) or "").strip().lower() == "paper" else "off"


# ------------------------------------------------------------------ pure rules

def target_price(cost_per_unit, net=NET, sell_fee=SELL_FEE):
    """The price at which selling nets `net` of the cost after the sale fee:
    q*p*(1 - f) - q*c = net*q*c  ->  p = c(1 + net)/(1 - f)."""
    return cost_per_unit * (1 + net) / (1 - sell_fee)


def practice_pnl(qty, cost_per_unit, price, sell_fee=SELL_FEE):
    return qty * price * (1 - sell_fee) - qty * cost_per_unit


def leftovers(positions, claimed_units, tree_claimed, no_sell):
    """(keep, skipped). positions: [{asset, units, available, cost_per_unit,
    price}]. keep: [{asset, qty, cost_per_unit, price, worth}] - the units no
    bot holds a sell price on, worth at least the floor. skipped: [(asset,
    reason)] for every held coin that is not kept, so nothing is hidden."""
    keep, skipped = [], []
    for p in positions:
        a = p["asset"]
        if a in CASH:
            continue
        if a in no_sell:
            skipped.append((a, "on the no-sell list - never sold"))
            continue
        if a in tree_claimed:
            skipped.append((a, "a tree branch claims it"))
            continue
        free = min(p.get("available") if p.get("available") is not None else p["units"], p["units"])
        extra = free - claimed_units.get(a, 0.0)
        price = p.get("price") or 0.0
        if extra <= 0 or price <= 0:
            if claimed_units.get(a):
                skipped.append((a, "the grid or the coin picker holds every unit"))
            continue
        worth = extra * price
        if worth < MIN_VIABLE_USD:
            skipped.append((a, f"leftover worth ${worth:,.2f} - under the ${MIN_VIABLE_USD:.0f} floor"))
            continue
        c = p.get("cost_per_unit")
        if not c or c <= 0:
            skipped.append((a, "Coinbase reports no cost for it - no profit price can be set"))
            continue
        keep.append({"asset": a, "qty": extra, "cost_per_unit": c, "price": price, "worth": worth})
    return keep, skipped


# ------------------------------------------------------------------ Coinbase reads (GET only)

def _headers(path):
    import crypto_btc_compound_bot as engine
    return engine._auth_headers("GET", path)


async def _get_signed(session, path):
    import aiohttp
    try:
        async with session.get(f"https://{HOST}{path}", headers=_headers(path),
                               timeout=aiohttp.ClientTimeout(total=25)) as r:
            try:
                return r.status, await r.json(content_type=None)
            except Exception:
                return r.status, None
    except Exception as e:
        log.warning(f"[KEEPER] GET {path.split('?')[0]} got no answer ({type(e).__name__})")
        return None, None


def _num(x):
    try:
        if isinstance(x, dict):
            x = x.get("value")
        return float(x)
    except (TypeError, ValueError):
        return None


async def positions(session):
    """Every spot coin with Coinbase's cost per unit, or an error string."""
    status, data = await _get_signed(session, "/api/v3/brokerage/portfolios")
    if status != 200 or not isinstance(data, dict):
        return f"portfolios HTTP {status}"
    ports = data.get("portfolios") or []
    port = next((p for p in ports if p.get("type") == "DEFAULT"), ports[0] if ports else None)
    if not port or not port.get("uuid"):
        return "no portfolio"
    status, data = await _get_signed(session, f"/api/v3/brokerage/portfolios/{port['uuid']}")
    if status != 200 or not isinstance(data, dict):
        return f"portfolio breakdown HTTP {status}"
    out = []
    for s in (data.get("breakdown") or {}).get("spot_positions") or []:
        if s.get("is_cash"):
            continue
        units = _num(s.get("total_balance_crypto")) or 0.0
        if units <= 0:
            continue
        avg = _num(s.get("average_entry_price")) or 0.0
        basis = _num(s.get("cost_basis")) or 0.0
        per_unit = max(avg, basis / units if units else 0.0)
        out.append({"asset": str(s.get("asset") or "").upper(), "units": units,
                    "available": _num(s.get("available_to_trade_crypto")),
                    "cost_per_unit": per_unit,
                    "price": (_num(s.get("total_balance_fiat")) or 0.0) / units})
    return out


async def price_now(session, asset):
    """The public last price for ASSET-USD, or None."""
    import aiohttp
    try:
        async with session.get(f"{PUBLIC_API}/products/{asset}-USD/ticker", headers=PUBLIC_HEADERS,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None
            px = float((await r.json(content_type=None)).get("price"))
            return px if px > 0 else None
    except Exception:
        return None


# ------------------------------------------------------------------ the record

async def claimed_units():
    """{asset: units} the grid's open slices and the coin picker's open real
    pieces hold. Raises if unreadable - a pass that cannot see the claims
    decides nothing."""
    from sqlalchemy import func, select
    from database import get_session_factory
    from models import CoinPickerPiece, CryptoGridSlice
    out = {}
    async with get_session_factory()() as db:
        for pid, q in (await db.execute(select(CryptoGridSlice.product_id, func.sum(CryptoGridSlice.qty))
                                        .group_by(CryptoGridSlice.product_id))).all():
            a = str(pid or "").upper().split("-")[0]
            out[a] = out.get(a, 0.0) + float(q or 0.0)
        for pid, q in (await db.execute(select(CoinPickerPiece.product_id, func.sum(CoinPickerPiece.qty))
                                        .where(CoinPickerPiece.status == "open")
                                        .where(CoinPickerPiece.paper == False)  # noqa: E712 - SQL
                                        .group_by(CoinPickerPiece.product_id))).all():
            a = str(pid or "").upper().split("-")[0]
            out[a] = out.get(a, 0.0) + float(q or 0.0)
    return out


async def tree_claimed():
    try:
        import crypto_coin_claims
        return {str(p).upper().split("-")[0] for p in await crypto_coin_claims.tree_claimed_products()}
    except Exception:
        return set()


async def pieces():
    from sqlalchemy import select
    from database import get_session_factory
    from models import CoinKeeperPiece
    async with get_session_factory()() as db:
        return list((await db.execute(select(CoinKeeperPiece).where(CoinKeeperPiece.paper == True)))  # noqa: E712
                    .scalars().all())


async def open_piece(asset, qty, cost_per_unit, now):
    from database import get_session_factory
    from models import CoinKeeperPiece
    async with get_session_factory()() as db:
        db.add(CoinKeeperPiece(asset=asset, product_id=f"{asset}-USD", status="open", paper=True, qty=qty,
                               entry_price=cost_per_unit, cost_usd=qty * cost_per_unit,
                               target_price=target_price(cost_per_unit), sell_fee=SELL_FEE,
                               opened_at=datetime.fromtimestamp(now, timezone.utc).replace(tzinfo=None)))
        await db.commit()


async def close_piece(piece_id, status, now, price=None, pnl=None, note=None):
    from sqlalchemy import select
    from database import get_session_factory
    from models import CoinKeeperPiece
    async with get_session_factory()() as db:
        row = (await db.execute(select(CoinKeeperPiece).where(CoinKeeperPiece.id == piece_id))).scalar_one()
        row.status, row.exit_price, row.pnl, row.note = status, price, pnl, note
        row.closed_at = datetime.fromtimestamp(now, timezone.utc).replace(tzinfo=None)
        await db.commit()


# ------------------------------------------------------------------ the pass

async def run_pass(session, now=None):
    """One practice pass. Returns a summary. Never raises."""
    import no_sell
    now = time.time() if now is None else now
    out = {"mode": mode(), "taken_in": [], "sold": [], "gone": [], "skipped": [], "problems": []}
    if out["mode"] != "paper":
        out["problems"].append("off")
        return out
    try:
        pos = await positions(session)
        if isinstance(pos, str):
            out["problems"].append(pos)
            return _done(out, now)
        claims = await claimed_units()
        keep, out["skipped"] = leftovers(pos, claims, await tree_claimed(), set(no_sell.coins()))
        left = {k["asset"]: k for k in keep}
        held = await pieces()
        ever = {p.asset for p in held}
        for p in [p for p in held if p.status == "open"]:
            real = left.get(p.asset)
            if real is None or real["qty"] < p.qty * GONE_TOLERANCE:
                await close_piece(p.id, "gone", now, note="the leftover units are no longer there")
                out["gone"].append(p.asset)
                continue
            px = await price_now(session, p.asset)
            if px is not None:
                _last_prices[p.asset] = px
            if px is not None and px >= p.target_price:
                pnl = practice_pnl(p.qty, p.entry_price, p.target_price, p.sell_fee or SELL_FEE)
                await close_piece(p.id, "sold", now, price=p.target_price, pnl=pnl)
                log.warning(f"[KEEPER] practice sold {p.qty:.6g} {p.asset} at ${p.target_price:,.6g}: "
                            f"{pnl:+.2f} practice profit")
                out["sold"].append((p.asset, round(pnl, 2)))
        for a, k in left.items():
            if a in ever:
                continue                         # taken in once, ever - see the docstring
            await open_piece(a, k["qty"], k["cost_per_unit"], now)
            _last_prices[a] = k["price"]
            out["taken_in"].append((a, round(k["worth"], 2), round(target_price(k["cost_per_unit"]), 8)))
    except Exception as e:
        log.error(f"[KEEPER] pass failed ({type(e).__name__}: {e})")
        out["problems"].append(f"failed: {type(e).__name__}")
    return _done(out, now)


def _done(out, now):
    _last_pass.clear()
    _last_pass.update(out, at=now)
    return out


async def run_forever():
    """The practice loop. Started by main.py only when COIN_KEEPER=paper."""
    import aiohttp
    log.warning("[KEEPER] practice loop started - no orders are placed by this module")
    async with aiohttp.ClientSession() as session:
        while mode() == "paper":
            await run_pass(session)
            await asyncio.sleep(PASS_SECONDS)
    log.warning("[KEEPER] COIN_KEEPER is no longer paper - practice loop stopped")


def last_pass():
    return dict(_last_pass)


def last_prices():
    return dict(_last_prices)
