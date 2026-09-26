"""Every asset the venue reports, priced, with the gap to what we track.

WHY THIS EXISTS

On 2026-09-26 every dashboard in this repository reported the Coinbase
account at $572.47. It held $11,219.28.

Nothing was stolen and nothing was hidden. The figure those pages serve,
real_crypto_net_worth, is the sum of exactly three things: USD cash, coin
held by TREE branches, and coin held by GRID branches. The account also held
56 other assets - ZEC $2,803, XRP $2,414, BTC $1,468, ETH $1,246, SHIB
$1,071 - belonging to no branch, and a number built from branches cannot see
them. Its companion field, real_crypto_net_worth_missing, returned [],
because it only checks assets it already knows about.

The consequence was not cosmetic. An entire morning was spent tracing a
"$473 loss" that was a decline in branch-tracked value while the account
itself was twenty times larger than the number being traced.

capital_census.py already asks the venue directly, and its logic is right:
it prices each coin and reports the unpriceable separately rather than
calling them zero. But its live output read

    coins held    $0.00
      ZEC   1.82953300  (no price - not counted)

for all 56 assets, because it prices from api.exchange.coinbase.com while
its balances come from api.coinbase.com. The balance host works from
production; the pricing host evidently does not. Every price silently
returned None, every holding was excluded "rather than guessed", and the
honest-by-design fallback printed $0.00 with a straight face.

So this module takes three positions:

  1. PRICE FROM THE HOST THAT ANSWERS. Advanced Trade first - the same host
     and the same credentials that just returned the balances - and the
     public feed only as a fallback. A pricing path that can fail while the
     balance path succeeds is a silent zero waiting to happen.
  2. AN UNPRICED ASSET IS A HOLE IN THE ANSWER, NOT A ZERO. The total is
     never reported alone; it is reported with how many assets are missing
     from it and what they are. A census that cannot price half the account
     must say so louder than it says the total.
  3. THE GAP IS THE PRODUCT. tracked_usd - what the branch-based figure
     believes - is compared against the venue's own total, and the
     difference is a first-class number. That difference is what nothing on
     any page could show, and it was $10,646.81.

Read-only. Places no order and writes nothing.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

log = logging.getLogger(__name__)

COINBASE_HOST = os.getenv("COINBASE_HOST", "api.coinbase.com")
PUBLIC_HOST = "api.exchange.coinbase.com"
ACCOUNTS_PATH = "/api/v3/brokerage/accounts"
MAX_ACCOUNT_PAGES = 20
PRICE_CONCURRENCY = 8

# Assets that ARE dollars. Priced at 1.0 rather than looked up, because a
# stablecoin whose ticker happens to be unreachable would otherwise land in
# the unpriced bucket and quietly leave real cash out of the total.
STABLE = {"USD", "USDC", "USDT", "DAI", "PYUSD", "USDS"}

# Below this a holding is dust: real, but not worth a line of its own. It is
# still COUNTED - only the presentation collapses it.
DUST_USD = 0.50


def _auth_headers(method: str, path: str):
    """Reuse the signing that is already proven in production.

    crypto_btc_compound_bot signs the BARE path and lets the query string be
    appended at request time. capital_census signed the path WITH its query
    and got HTTP 401 on every call for most of a day, which was read as a
    revoked key. It was not the key.
    """
    import crypto_btc_compound_bot as engine
    return engine._auth_headers(method, path)


async def fetch_balances(session) -> dict:
    """Every non-zero balance the venue reports. Paginated. Never raises."""
    accounts, cursor, pages = [], None, 0
    try:
        while pages < MAX_ACCOUNT_PAGES:
            q = "?limit=250" + (f"&cursor={cursor}" if cursor else "")
            async with session.get(f"https://{COINBASE_HOST}{ACCOUNTS_PATH}{q}",
                                   headers=_auth_headers("GET", ACCOUNTS_PATH),
                                   timeout=25) as r:
                if r.status != 200:
                    return {"available": False,
                            "error": f"accounts HTTP {r.status}",
                            "detail": (await r.text())[:300]}
                body = await r.json()
            accounts.extend(body.get("accounts") or [])
            pages += 1
            cursor = body.get("cursor") or None
            if not body.get("has_next") or not cursor:
                break
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}: {e}"}

    held = {}
    for a in accounts:
        cur = a.get("currency")
        if not cur:
            continue
        total = 0.0
        for field in ("available_balance", "hold"):
            try:
                total += float((a.get(field) or {}).get("value") or 0)
            except (TypeError, ValueError):
                pass
        if total > 0:
            held[cur] = held.get(cur, 0.0) + total
    return {"available": True, "held": held, "pages": pages,
            "accounts_seen": len(accounts)}


async def _price_one(session, asset: str):
    """Advanced Trade first, public feed second. Returns (price, source)."""
    if asset in STABLE:
        return 1.0, "stablecoin par"
    for quote in ("USD", "USDC"):
        path = f"/api/v3/brokerage/products/{asset}-{quote}"
        try:
            async with session.get(f"https://{COINBASE_HOST}{path}",
                                   headers=_auth_headers("GET", path),
                                   timeout=15) as r:
                if r.status == 200:
                    b = await r.json()
                    p = float(b.get("price") or 0)
                    if p > 0:
                        return p, f"advanced_trade {asset}-{quote}"
        except Exception:
            pass
    for quote in ("USD", "USDC"):
        try:
            async with session.get(
                    f"https://{PUBLIC_HOST}/products/{asset}-{quote}/ticker",
                    timeout=15) as r:
                if r.status == 200:
                    p = float((await r.json()).get("price") or 0)
                    if p > 0:
                        return p, f"public_feed {asset}-{quote}"
        except Exception:
            pass
    return None, None


async def price_all(session, assets) -> dict:
    """Price every asset, bounded concurrency. Never raises."""
    sem = asyncio.Semaphore(PRICE_CONCURRENCY)

    async def one(a):
        async with sem:
            return a, await _price_one(session, a)
    out = {}
    try:
        for coro in asyncio.as_completed([one(a) for a in assets]):
            a, (p, src) = await coro
            out[a] = {"price": p, "source": src}
    except Exception as e:
        log.warning(f"[CENSUS] pricing pass failed: {type(e).__name__}: {e}")
    return out


async def census(session, tracked_usd: float = None) -> dict:
    """The whole account, priced, against what the branch view believes."""
    bal = await fetch_balances(session)
    if not bal.get("available"):
        return {"available": False, "error": bal.get("error"),
                "detail": bal.get("detail")}
    held = bal["held"]
    prices = await price_all(session, list(held))

    cash, coin_usd = 0.0, 0.0
    rows, unpriced = [], []
    for asset, units in held.items():
        p = (prices.get(asset) or {}).get("price")
        if p is None:
            unpriced.append({"asset": asset, "units": units})
            continue
        usd = units * p
        if asset in STABLE:
            cash += usd
        else:
            coin_usd += usd
        rows.append({"asset": asset, "units": units, "price": p,
                     "usd": round(usd, 2),
                     "source": (prices.get(asset) or {}).get("source")})
    rows.sort(key=lambda r: -r["usd"])
    total = cash + coin_usd

    out = {
        "available": True,
        "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "venue": "Coinbase",
        "assets_held": len(held),
        "assets_priced": len(rows),
        "assets_unpriced": len(unpriced),
        "unpriced": unpriced,
        "cash_usd": round(cash, 2),
        "coin_usd": round(coin_usd, 2),
        "total_usd": round(total, 2),
        "holdings": [r for r in rows if r["usd"] >= DUST_USD],
        "dust_usd": round(sum(r["usd"] for r in rows if r["usd"] < DUST_USD), 2),
        "dust_assets": sum(1 for r in rows if r["usd"] < DUST_USD),
        "accounts_pages": bal["pages"],
    }
    # THE WARNING GOES ABOVE THE TOTAL, NOT BESIDE IT. A census that priced
    # half the account and printed a confident number is what produced
    # "coins held $0.00" on an $11,219 balance.
    if unpriced:
        out["warning"] = (
            f"{len(unpriced)} of {len(held)} assets could not be priced and are "
            f"NOT in total_usd: {', '.join(u['asset'] for u in unpriced)}. "
            f"The real total is higher than the figure shown by whatever they "
            f"are worth.")
    if tracked_usd is not None:
        gap = total - tracked_usd
        out["tracked_usd"] = round(tracked_usd, 2)
        out["untracked_usd"] = round(gap, 2)
        out["tracked_share_pct"] = round(100.0 * tracked_usd / total, 2) if total else None
        out["reconciliation"] = (
            f"The bot-facing figure counts ${tracked_usd:,.2f}. The venue reports "
            f"${total:,.2f}. ${gap:,.2f} - {100.0 * gap / total:.1f}% of the account - "
            f"belongs to no branch, so nothing monitors, prices, or stops it."
            if total else "no priced total to reconcile against")
    return out
