"""What the fleet holds, what is failing, and what could replace it.

A proposal, never an action. Nothing here places an order, creates a
branch, or moves a dollar - it composes universe_scan's measurement with
a correlation reading and ranks the result. The trading universe stays
locked to coins a human named; this is the evidence a human would use to
name a different one.

WHY CORRELATION AND NOT JUST MOVEMENT. The account owner's own recorded
experience: "last time I tried to go and pull all these different coins,
they all dragged down and I lost a lot of money on that." Measured, that
is right - average pairwise correlation across this asset class runs
about +0.574, and the four branches that currently pass their checks
average 0.48 with each other. Swapping in the two biggest movers would
raise that. A replacement is only worth making if it moves enough, fills
as maker, AND is not the same bet wearing a different ticker.

Measured 2026-09-26, two of six branches could not fill as maker at all:
TIA at $679,607/day and FLOKI at $95,738/day against a $750,000 floor. A
grid on a book that thin crosses the spread and pays taker - 1.50% round
trip against a 2.50% step - so those branches were structurally unable to
clear costs, and nothing on the dashboard said so.
"""

import asyncio
import datetime
import math
import statistics

import universe_scan

# Below this a candidate is genuinely a different bet. The fleet's own
# internal average is the honest yardstick and it is reported alongside,
# so "low" is relative to what is already held rather than to a number
# picked here.
DIVERSIFIES_BELOW = 0.45
SAME_BET_ABOVE = 0.60

CORR_WINDOW_DAYS = 180
MIN_CORR_BARS = 30


def pearson(a, b):
    """Correlation of two return series. None when there is too little
    overlap to mean anything - never 0.0, which reads as 'independent'."""
    n = min(len(a), len(b))
    if n < MIN_CORR_BARS:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da == 0 or db == 0:
        return None
    return round(num / (da * db), 4)


def returns(closes):
    if not closes or len(closes) < 2:
        return []
    return [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes)) if closes[i - 1]]


def correlation_verdict(avg_corr):
    if avg_corr is None:
        return "unknown"
    if avg_corr < DIVERSIFIES_BELOW:
        return "diversifies"
    if avg_corr < SAME_BET_ABOVE:
        return "ok"
    return "same bet"


async def _closes(session, pid, days=CORR_WINDOW_DAYS):
    end = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(days=days)
    params = {"granularity": 86400,
              "start": start.isoformat(), "end": end.isoformat()}
    url = universe_scan.CANDLES_URL.format(pid=pid)
    for attempt in range(universe_scan.RETRY_ATTEMPTS):
        try:
            async with session.get(url, params=params,
                                   timeout=universe_scan._timeout(45)) as r:
                if r.status == 200:
                    rows = await r.json()
                    if isinstance(rows, list) and len(rows) >= MIN_CORR_BARS:
                        return [float(x[4]) for x in reversed(rows)
                                if isinstance(x, (list, tuple)) and len(x) >= 6]
                    return None
                if r.status not in universe_scan.RETRYABLE_STATUS:
                    return None
        except Exception:
            pass
        await asyncio.sleep(universe_scan.RETRY_BACKOFF_SECONDS * (2 ** attempt))
    return None


def pair_swaps(failing, candidates):
    """Best available candidate to the worst failing branch, each used once.

    Ranked by correlation first and movement second: a replacement that
    moves less but genuinely diversifies beats a bigger mover that falls
    on the same days, which is the mistake this whole module exists to
    stop repeating.
    """
    usable = [c for c in candidates if c.get("avg_corr") is not None]
    usable.sort(key=lambda c: (c["avg_corr"], -(c.get("daily_vol_pct") or 0)))
    out, used = [], set()
    for branch in failing:
        for c in usable:
            if c["product_id"] in used:
                continue
            used.add(c["product_id"])
            out.append({
                "out": branch["product_id"],
                "out_reason": branch.get("reason"),
                "in": c["product_id"],
                "in_daily_vol_pct": c.get("daily_vol_pct"),
                "in_notional_24h_usd": c.get("notional_24h_usd"),
                "in_avg_corr": c["avg_corr"],
                "in_corr_verdict": correlation_verdict(c["avg_corr"]),
                "why": (f"{branch['product_id'].replace('-USD', '')} cannot fill as "
                        f"maker on its book; {c['product_id'].replace('-USD', '')} "
                        f"moves {c.get('daily_vol_pct', 0):.2f}% a day on "
                        f"${c.get('notional_24h_usd', 0):,.0f} of depth at "
                        f"{c['avg_corr']:.2f} correlation to what you keep."),
            })
            break
    return out


async def review(fleet_products, *, session=None, top_n=12,
                 fee_pct=universe_scan.ROUND_TRIP_FEE_PCT,
                 min_notional=universe_scan.MIN_24H_NOTIONAL_USD):
    """The whole picture. Read-only."""
    own = session is None
    if own:
        import aiohttp
        session = aiohttp.ClientSession()
    try:
        scan = await universe_scan.scan(session=session, fee_pct=fee_pct,
                                        min_notional=min_notional)
        by_id = {r["product_id"]: r for r in scan["rows"]}
        tradeable = [r for r in scan["rows"] if r["verdict"] == "tradeable"]
        rank_of = {r["product_id"]: i + 1 for i, r in enumerate(tradeable)}

        fleet, keeping, failing = [], [], []
        for pid in fleet_products:
            r = dict(by_id.get(pid) or {"product_id": pid, "verdict": "no data",
                                        "reason": "not on the venue scan"})
            r["rank"] = rank_of.get(pid)
            r["of"] = len(tradeable)
            fleet.append(r)
            (keeping if r["verdict"] == "tradeable" else failing).append(r)

        # Correlation is measured against the branches being KEPT. A
        # candidate's relationship to a branch about to be dropped is not
        # the question.
        pool = [r["product_id"] for r in keeping]
        shortlist = [r for r in tradeable
                     if r["product_id"] not in set(fleet_products)]
        shortlist.sort(key=lambda r: -(r["daily_vol_pct"] or 0))
        shortlist = shortlist[:max(top_n * 3, 30)]

        wanted = pool + [r["product_id"] for r in shortlist]
        sem = asyncio.Semaphore(4)

        async def grab(pid):
            async with sem:
                c = await _closes(session, pid)
                await asyncio.sleep(0.05)
                return pid, (returns(c) if c else None)

        series = dict(await asyncio.gather(*[grab(p) for p in wanted]))

        for c in shortlist:
            cs = [pearson(series.get(c["product_id"]) or [], series.get(k) or [])
                  for k in pool]
            cs = [x for x in cs if x is not None]
            c["avg_corr"] = round(sum(cs) / len(cs), 4) if cs else None
            c["max_corr"] = round(max(cs), 4) if cs else None
            c["corr_verdict"] = correlation_verdict(c["avg_corr"])

        internal = [pearson(series.get(a) or [], series.get(b) or [])
                    for i, a in enumerate(pool) for b in pool[i + 1:]]
        internal = [x for x in internal if x is not None]
        fleet_corr = round(sum(internal) / len(internal), 4) if internal else None

        ranked = sorted([c for c in shortlist if c.get("avg_corr") is not None],
                        key=lambda c: (c["avg_corr"], -(c["daily_vol_pct"] or 0)))
    finally:
        if own:
            await session.close()

    return {
        "fleet": fleet,
        "keeping": [r["product_id"] for r in keeping],
        "failing": failing,
        "fleet_internal_corr": fleet_corr,
        "candidates": ranked[:top_n],
        "suggested_swaps": pair_swaps(failing, ranked),
        "scanned": scan["scanned"],
        "tradeable": scan["tradeable"],
        "fee_pct": fee_pct,
        "min_notional_usd": min_notional,
        "diversifies_below": DIVERSIFIES_BELOW,
        "note": ("A proposal, not an action. Nothing here places an order or moves "
                 "capital - the trading universe stays locked to coins a human named. "
                 "Correlation is measured over 180 days of daily returns and rises "
                 "toward 1.0 in a crash, which is exactly when diversification stops "
                 "helping, so a low figure here is a normal-weather reading."),
    }
