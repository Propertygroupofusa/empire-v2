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

# WHY THE ACCOUNTS READ COMES BACK UNREADABLE, AND WHAT TO DO ABOUT IT.
#
# Measured 2026-09-28 13:53Z: four of ten consecutive /account-census
# calls returned `available: false, error: "accounts HTTP 429"`. A 40%
# refusal rate is not an exotic failure, it is the normal weather on this
# endpoint, and every caller that treats a refusal as "unknown" is
# therefore unknown roughly half the time. On 2026-10-08 12:44:58Z that
# cost real money: the grid's backing gate reads this map, a single
# refusal made it pass, and a branch bought coin it was short of.
#
# A 429 is the venue asking us to wait, not telling us the account is
# unreadable. So wait, briefly and a bounded number of times, honouring
# Retry-After when the venue sends one. Only a refusal that survives
# every attempt is reported as a refusal.
#
# Bounded on purpose: this runs inside a trading cycle. Three attempts
# with these gaps cannot add more than ACCOUNTS_RETRY_BUDGET_SECONDS to
# a page fetch, and the function keeps its never-raises contract.
ACCOUNTS_RETRY_STATUSES = (429, 500, 502, 503, 504)
ACCOUNTS_RETRY_ATTEMPTS = 3
ACCOUNTS_RETRY_BACKOFF_SECONDS = (0.5, 1.5)
ACCOUNTS_RETRY_BUDGET_SECONDS = 6.0


def _retry_after_seconds(resp, fallback):
    """The venue's own Retry-After, clamped to the retry budget."""
    raw = None
    try:
        raw = resp.headers.get("Retry-After")
    except Exception:
        raw = None
    if raw:
        try:
            return max(0.0, min(float(str(raw).strip()), ACCOUNTS_RETRY_BUDGET_SECONDS))
        except (TypeError, ValueError):
            pass
    return fallback
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
            body = None
            refusal = None
            for attempt in range(ACCOUNTS_RETRY_ATTEMPTS):
                async with session.get(f"https://{COINBASE_HOST}{ACCOUNTS_PATH}{q}",
                                       headers=_auth_headers("GET", ACCOUNTS_PATH),
                                       timeout=25) as r:
                    if r.status == 200:
                        body = await r.json()
                        refusal = None
                        break
                    refusal = {"available": False,
                               "error": f"accounts HTTP {r.status}",
                               "detail": (await r.text())[:300],
                               "attempts": attempt + 1}
                    # A status the venue will not reconsider is final on
                    # the first look - retrying a 401 only delays the bad
                    # news and burns cycle time.
                    if r.status not in ACCOUNTS_RETRY_STATUSES:
                        break
                    if attempt >= ACCOUNTS_RETRY_ATTEMPTS - 1:
                        break
                    wait = _retry_after_seconds(
                        r, ACCOUNTS_RETRY_BACKOFF_SECONDS[
                            min(attempt, len(ACCOUNTS_RETRY_BACKOFF_SECONDS) - 1)])
                log.info(f"[CENSUS] accounts HTTP {refusal['error'][-3:]} - "
                         f"attempt {attempt + 1}/{ACCOUNTS_RETRY_ATTEMPTS}, "
                         f"waiting {wait:.1f}s")
                await asyncio.sleep(wait)
            if refusal is not None:
                return refusal
            accounts.extend(body.get("accounts") or [])
            pages += 1
            cursor = body.get("cursor") or None
            if not body.get("has_next") or not cursor:
                break
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}: {e}"}

    # AVAILABLE AND HELD ARE KEPT APART, NOT SUMMED AWAY.
    #
    # This used to add available_balance and hold into one figure and
    # return only the sum. Everything downstream then had a number that
    # counts coin the venue will not release - staked units, units behind
    # a resting stop, units in an open order. coin_adoption already asks
    # for `available_units` and falls back to `units` when it is absent,
    # and because this function never supplied it, that fallback was the
    # only path ever taken: the module that exists to size against what
    # the venue will release has been sizing against everything owned.
    #
    # The venue is the authority on this and it already says so per
    # account. Both figures are reported; `held` keeps its old meaning so
    # no existing caller changes behaviour.
    held, available, held_all = {}, {}, {}
    for a in accounts:
        cur = a.get("currency")
        if not cur:
            continue

        def _f(field):
            try:
                return float((a.get(field) or {}).get("value") or 0)
            except (TypeError, ValueError):
                return 0.0

        avail = _f("available_balance")
        total = avail + _f("hold")
        # EVERY account the venue listed, INCLUDING the ones holding zero.
        #
        # `held` below keeps its exact meaning and its `total > 0` filter,
        # because every existing caller depends on it. This one does not
        # filter, and that is the whole difference: a currency the venue
        # lists with a balance of exactly 0.0 is a CONFIRMED ZERO, which is
        # the largest shortfall a branch can have, and the filtered map
        # cannot express it. TIA-USD and PRIME-USD were invisible to the
        # shortfall check for precisely this reason.
        #
        # It costs NOTHING: these rows are already in hand and were being
        # discarded. The first attempt at this fix re-read the account list
        # once per missing asset instead - and since get_asset_balance
        # paginates the WHOLE list for one currency, that turned one read
        # into about five per call, got rate-limited, and left the check
        # reporting UNKNOWN with no positions at all. Blind is worse than
        # under-reported.
        held_all[cur] = held_all.get(cur, 0.0) + total
        if total > 0:
            held[cur] = held.get(cur, 0.0) + total
            available[cur] = available.get(cur, 0.0) + avail
    return {"available": True, "held": held, "available_units": available,
            "held_including_zero": held_all,
            "pages": pages, "accounts_seen": len(accounts)}


def owned_units_map(balances):
    """{ASSET: units the account OWNS}, or None when unreadable.

    OWNED, NOT AVAILABLE - and the difference is not cosmetic.
    `available_units` excludes coin the venue is holding against a resting
    order or a stake. That is the right figure for "can this branch place
    a sell right now", which is what slice_backing and the dashboard's
    `backing` block ask. It is the WRONG figure for "does this coin exist
    at all".

    Measured 2026-10-04 05:13Z, the four branches the backing gate was
    refusing:
        SOL   claims   1.034600  owns   1.034600  available   0.258650
        LINK  claims   9.340000  owns  10.090000  available   3.460000
        ALGO  claims 492.700000  owns 1347.646389 available 213.346389
        ACH   claims 5345.2000   owns 5345.204595 available   0.004595
    Every one owns at least what it claims. All $245.23 the gate called
    "not in the wallet" was the fleet's own resting sell orders. A gate
    reading `available` refuses a buy on a branch whose coin is entirely
    present - which is a false refusal on a healthy branch, and the
    reconcile endpoint's own comment already said so, naming SOL and LINK.

    Same source and same choice as reconcile-slices, deliberately: these
    two must never disagree about what the account owns.
    """
    if not balances or not balances.get("available"):
        return None
    owned = balances.get("held_including_zero")
    if isinstance(owned, dict) and owned:
        return {str(k).upper(): v for k, v in owned.items()}
    # Older payload without the zero-inclusive map. `held` is also
    # available+hold, so it answers the same question; it merely drops
    # confirmed zeros, which read as UNKNOWN rather than as a shortfall.
    held = balances.get("held")
    if isinstance(held, dict) and held:
        return {str(k).upper(): v for k, v in held.items()}
    return None


def available_units_map(balances):
    """{ASSET: units the venue will RELEASE}, or None when unreadable.

    THE ONE PLACE THIS NORMALIZATION LIVES. Three callers need the same
    map - the dashboard's `backing` block, exchange_truth_worker, and the
    grid's own buy gate - and each had its own copy of these three lines.
    A backing gate that normalizes even slightly differently from the
    page the owner reads is worse than no gate: it refuses buys the page
    says are fine, or allows ones it says are not. Same failure as a
    module re-implementing tradeable_slices instead of importing it.

    TWO MAPS, BECAUSE NEITHER IS SUFFICIENT ALONE.
      `available_units` is what an order is actually sized against, but
      fetch_balances filters it on `total > 0`, so an asset the venue
      lists at exactly 0.0 is MISSING from it - and a confirmed zero is
      the largest shortfall a branch can have.
      `held_including_zero` lists every account including the zeros, but
      it is available+hold, which counts coin the venue will not release.

    So: start from available_units, and add a 0.0 only for an asset the
    zero-inclusive map confirms is empty. setdefault never lowers a
    figure already present. An asset in NEITHER map stays ABSENT, so
    slice_backing reports it UNKNOWN rather than inventing a shortfall
    out of a rate limit.
    """
    if not balances or not balances.get("available"):
        return None
    out = dict(balances.get("available_units") or {})
    for cur, total in (balances.get("held_including_zero") or {}).items():
        out.setdefault(cur, 0.0 if not total else out.get(cur, 0.0))
    return out


def wallet_units_for(balances, assets, direct=None):
    """A units map for exactly the assets asked about, with ZERO and UNKNOWN
    kept apart. Pure - the caller does the I/O.

    WHY THIS EXISTS. coin_tracked_is_held was being handed census()'s
    `holdings` list, and this module's own asset-balance endpoint already
    documents why that is the wrong input: census drops assets it cannot
    price and rolls anything under the dust threshold into an unnamed count.
    Its job is "what is this account worth"; it cannot answer "does this
    account hold X at all".

    The consequence was not theoretical. A coin absent from that list is
    classified UNREADABLE rather than short - correct defence given the
    input - so the three positions with the LARGEST shortfalls in the fleet
    were reported as unknown in a footnote while the headline named only the
    six smaller ones. QNT-USD alone was $149.54 short, more than any coin in
    the headline.

    Two filters were doing it, and neither is a fault of the filter:
      - the dust threshold, which hid QNT (0.00097323 units, worth $0.22)
      - `total > 0` in fetch_balances, which cannot represent a real zero
        and so hid TIA and PRIME after they went to exactly nothing

    `balances` is fetch_balances()'s result. Its `held_including_zero` map
    is preferred when present: it lists every account the venue reported,
    including those holding exactly zero, at no extra API cost.

    `direct` is {asset: (units, reason)} from a per-currency read, and is now
    only a fallback for an older payload without that map. A direct read of
    0.0 is a CONFIRMED ZERO and lands as 0.0; a failed one stays absent,
    because an unreadable balance is still not an empty one and that rule is
    the whole point.
    """
    if not balances or not balances.get("available"):
        return None
    # Prefer the unfiltered map. `held` drops any account at exactly zero,
    # which is the single case this function exists to represent.
    held = balances.get("held_including_zero") or balances.get("held") or {}
    direct = direct or {}
    out = {}
    for asset in assets or ():
        key = str(asset).upper()
        if key in held:
            out[key] = held[key]
            continue
        units, _reason = direct.get(key, (None, None))
        if units is not None:
            # The venue was asked about this one currency and answered. 0.0
            # here means the account exists and holds nothing - the maximum
            # possible shortfall, and precisely the case the filtered map
            # could not express.
            out[key] = units
        # else: left ABSENT, so the check reports it unreadable rather than
        # inventing a zero for a balance nobody could read.
    return out


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
    avail_units = bal.get("available_units") or {}
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
        au = avail_units.get(asset)
        rows.append({"asset": asset, "units": units, "price": p,
                     "usd": round(usd, 2),
                     # What the venue will actually release, beside what is
                     # owned. None only when the balance read did not carry
                     # it - never silently equal to `units`, because a
                     # caller that cannot tell them apart is exactly the
                     # caller that tries to sell staked coin.
                     "available_units": au,
                     "available_usd": (round(au * p, 2) if au is not None else None),
                     "locked_units": (round(units - au, 12) if au is not None else None),
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
        # THE UNFILTERED MAP, CARRIED THROUGH.
        #
        # Everything above this line is the census's own job - what the
        # account is WORTH - and to do it it drops unpriced assets and rolls
        # dust into an unnamed count. That makes `holdings` the wrong input
        # for "does this account hold X at all", which is a different
        # question and the one the shortfall check asks.
        #
        # Passing this through costs nothing: fetch_balances already
        # produced it. A caller that needs the honest units map no longer
        # has to make a SECOND account read to get it - which is what broke
        # coin_tracked_is_held once, by turning one read into several and
        # being rate-limited into reporting UNKNOWN.
        "held_including_zero": bal.get("held_including_zero"),
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
    return apply_tracked(out, tracked_usd)


# ── THE COMPARISON IS PER-CALLER; THE VENUE READING IS SHARED ──────────
#
# tracked_usd is the BOT's figure, not the venue's, and different callers
# pass different ones: /account-census passes the real bot-facing number,
# while trading_dashboard.py's internal caller passes 0.0. Everything
# else in a census dict describes the account and is identical for every
# caller - which is why it can be cached and shared.
#
# These four fields cannot. Baking one caller's comparison into a shared
# cache hands the next caller a reconciliation sentence that was never
# true of its own figure - and this one reads "$X belongs to no branch,
# so nothing monitors, prices, or stops it", which is precisely the
# sentence nobody should ever read a stale or borrowed version of.
#
# So they are computed HERE, fresh, on every return path in
# census_cached, against whatever total the shared reading carries.
def apply_tracked(out, tracked_usd):
    """(Re)compute the tracked-vs-venue comparison on a census dict.

    Clears the four fields first, so a dict that arrives carrying
    ANOTHER caller's comparison cannot keep it when this caller passes
    tracked_usd=None.
    """
    for k in ("tracked_usd", "untracked_usd", "tracked_share_pct",
              "reconciliation"):
        out.pop(k, None)
    if tracked_usd is None:
        return out
    total = out.get("total_usd") or 0.0
    gap = total - tracked_usd
    out["tracked_usd"] = round(tracked_usd, 2)
    out["untracked_usd"] = round(gap, 2)
    out["tracked_share_pct"] = (round(100.0 * tracked_usd / total, 2)
                                if total else None)
    out["reconciliation"] = (
        f"The bot-facing figure counts ${tracked_usd:,.2f}. The venue reports "
        f"${total:,.2f}. ${gap:,.2f} - {100.0 * gap / total:.1f}% of the account - "
        f"belongs to no branch, so nothing monitors, prices, or stops it."
        if total else "no priced total to reconcile against")
    return out


# ── a reading with an age, which is not the same as a fallback ──────────
#
# The venue rate-limits. Measured 2026-09-28 13:53Z: FOUR of ten
# consecutive /account-census calls came back `available: false,
# error: "accounts HTTP 429"`. The census is right to refuse rather
# than guess - but a caller that hits a 40% refusal rate and has
# nothing else to say renders "unavailable" almost half the time,
# which is a page nobody trusts.
#
# The honest middle is a real measurement carrying its own age. A
# reading from 40 seconds ago IS the account, near enough, and it says
# so out loud: stale=True, age_seconds=40. That is the opposite of the
# 483.00 literal this module was brought in to replace - that number
# was never measured at all and never admitted it.
#
# Nothing here invents a figure. With no cached reading, the refusal
# passes straight through and the caller still shows "unavailable".
CENSUS_TTL_SECONDS = 45.0
_CENSUS_CACHE = {"census": None, "at": 0.0}


async def census_cached(session, tracked_usd: float = None,
                        max_age_seconds: float = CENSUS_TTL_SECONDS,
                        max_stale_seconds: float = 900.0) -> dict:
    """census(), shared across callers, with the last good reading as
    the fallback when the venue refuses - never a fabricated one.

    Returns the census dict with two extra keys on every success path:
      stale        - False when freshly read, True when served from cache
      age_seconds  - how old the reading is, 0.0 when fresh

    A cached reading older than max_stale_seconds is not served; the
    refusal is returned instead, so "unavailable" still means
    unavailable rather than "here is something from an hour ago".
    """
    import time as _t
    now = _t.time()
    cached = _CENSUS_CACHE.get("census")
    age = now - (_CENSUS_CACHE.get("at") or 0.0)

    # Every served path recomputes the tracked comparison for THIS
    # caller - see apply_tracked. The cached reading describes the
    # account; the comparison describes the caller's own books.
    if cached is not None and age < max_age_seconds:
        out = apply_tracked(dict(cached), tracked_usd)
        out["stale"] = True
        out["age_seconds"] = round(age, 1)
        return out

    try:
        fresh = await census(session, tracked_usd=tracked_usd)
    except Exception as exc:
        fresh = {"available": False, "error": f"{type(exc).__name__}: {exc}",
                 "detail": ""}

    if fresh.get("available"):
        # Stored WITHOUT this caller's comparison, so the next caller
        # cannot inherit it. apply_tracked strips the four fields on the
        # way in and puts the right ones back on the way out.
        _CENSUS_CACHE.update(census=apply_tracked(dict(fresh), None), at=now)
        out = apply_tracked(dict(fresh), tracked_usd)
        out["stale"] = False
        out["age_seconds"] = 0.0
        return out

    # The read failed. Serve the last good one WITH ITS AGE, if it is
    # recent enough to still describe the same account.
    if cached is not None and age <= max_stale_seconds:
        out = apply_tracked(dict(cached), tracked_usd)
        out["stale"] = True
        out["age_seconds"] = round(age, 1)
        out["stale_reason"] = fresh.get("error") or "read failed"
        return out

    return fresh
