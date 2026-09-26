"""Every coin we can get data on, measured - not every coin we trade.

402 USD pairs are live on the venue. The fleet trades six. Those are two
different questions and conflating them is expensive:

  MEASURING a coin costs a candle request. There is no reason not to know
  about all of them, and knowing is how a better six gets chosen.

  TRADING a coin costs money and carries the account owner's own recorded
  experience: "last time I tried to do this and go and try to pull all
  these different coins, they all dragged down and I lost a lot of money
  on that." Measured, that instinct is right - average pairwise
  correlation +0.574 across 16 coins, 37 of 120 days with 80%+ falling
  together. Sixteen coins is not sixteen bets.

So this widens the MEASUREMENT set to everything and changes the trading
set not at all. coin_rotation.universe() still gates what may receive
money, and still requires a human to name it.

Per coin it reports the three things that decide whether a grid could
ever work on it:

  daily volatility   can it move far enough to clear the fee at all
  24h notional       is the book deep enough to fill as maker, or will
                     every trade cross the spread and pay taker
  fee-clearing move  the move it needs, and whether its own volatility
                     plausibly delivers one

A coin failing any of the three is reported with the reason, never
silently dropped.
"""

import asyncio
import datetime

PRODUCTS_URL = "https://api.exchange.coinbase.com/products"
CANDLES_URL = "https://api.exchange.coinbase.com/products/{pid}/candles"

# Maker both legs at this account's real rate. A coin whose typical daily
# move cannot clear this cannot pay for a round trip on it.
ROUND_TRIP_FEE_PCT = 0.70

# Below this, a grid order does not get the maker fill it is priced for -
# it crosses the spread and pays taker. Same floor coin_rotation uses.
MIN_24H_NOTIONAL_USD = 750_000.0

# Daily bars over half a year: one request per coin, and long enough that
# the volatility reading is stable (see adaptive_stop's window note).
WINDOW_DAYS = 180
MIN_BARS = 30

# 429 is a rate limit and 5xx is the venue having a moment. Both clear on
# their own; neither is evidence about a coin.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 1.5


async def list_usd_products(session):
    """Every online, tradeable USD pair. Never a guessed list."""
    async with session.get(PRODUCTS_URL, timeout=_timeout(60)) as r:
        if r.status != 200:
            raise RuntimeError(f"products HTTP {r.status}")
        rows = await r.json()
    if not isinstance(rows, list):
        raise RuntimeError("products response was not a list")
    out = []
    for p in rows:
        pid = p.get("id") or ""
        if (p.get("quote_currency") == "USD"
                and p.get("status") == "online"
                and not p.get("trading_disabled")
                and not p.get("auction_mode")
                and pid.count("-") == 1):
            out.append(pid)
    return sorted(out)


def _timeout(total):
    import aiohttp
    return aiohttp.ClientTimeout(total=total)


async def measure_one(session, pid, *, days=WINDOW_DAYS):
    """{product_id, daily_vol_pct, notional_24h_usd, bars} or a reason."""
    import adaptive_stop
    end = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(days=days)
    url = CANDLES_URL.format(pid=pid)
    params = {"granularity": 86400,
              "start": start.isoformat(), "end": end.isoformat()}

    # A 429 is the venue saying "slow down", not "this coin has no
    # history". Recorded as no-data it becomes a fact about the coin, and
    # on the first full run that is exactly what happened to ZEC and XRP -
    # the account's two largest holdings, 46% of its value, reported as
    # unmeasurable because the scan asked too fast. coin_rotation's own
    # fetcher has retried on this since it was written; this did not.
    rows = None
    last = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            async with session.get(url, params=params, timeout=_timeout(45)) as r:
                if r.status == 200:
                    rows = await r.json()
                    break
                last = f"candles HTTP {r.status}"
                if r.status not in RETRYABLE_STATUS:
                    break
        except Exception as exc:
            last = f"{type(exc).__name__}"
        if attempt < RETRY_ATTEMPTS - 1:
            await asyncio.sleep(RETRY_BACKOFF_SECONDS * (2 ** attempt))
    if rows is None:
        return {"product_id": pid, "skipped": last or "no response",
                "retried": RETRY_ATTEMPTS}

    if not isinstance(rows, list) or len(rows) < MIN_BARS:
        return {"product_id": pid,
                "skipped": f"only {len(rows) if isinstance(rows, list) else 0} daily bars"}

    # Coinbase candles: [time, low, high, open, close, volume], newest first.
    rows = [r for r in rows if isinstance(r, (list, tuple)) and len(r) >= 6]
    closes = [float(r[4]) for r in reversed(rows)]
    vol = adaptive_stop.daily_vol_pct_from_closes(closes, bars_per_day=1)
    if vol is None:
        return {"product_id": pid, "skipped": "volatility unreadable"}

    recent = rows[:1]
    notional = float(sum(r[5] * r[4] for r in recent)) if recent else 0.0
    return {"product_id": pid, "daily_vol_pct": round(vol, 4),
            "notional_24h_usd": round(notional, 2), "bars": len(rows),
            "last_price": closes[-1]}


def assess(row, *, fee_pct=ROUND_TRIP_FEE_PCT, min_notional=MIN_24H_NOTIONAL_USD):
    """Adds a verdict. A coin fails for a stated reason or not at all."""
    if row.get("skipped"):
        skipped = str(row["skipped"])
        rate_limited = any(str(c) in skipped for c in RETRYABLE_STATUS)
        row["verdict"] = "unmeasured" if rate_limited else "no data"
        row["reason"] = (
            f"{skipped} after {row.get('retried', 1)} attempts - the venue would not "
            f"serve it, which says nothing about the coin. Re-run to measure it."
            if rate_limited else skipped)
        return row

    vol = row.get("daily_vol_pct") or 0.0
    notional = row.get("notional_24h_usd") or 0.0
    reasons = []

    # The move a round trip has to clear before anything else matters.
    row["fee_clearing_move_pct"] = fee_pct
    # A day's typical move against the toll. Under 1x, a full day of
    # ordinary movement does not pay for one round trip.
    row["daily_vol_over_fee"] = round(vol / fee_pct, 2) if fee_pct else None

    if vol < fee_pct:
        reasons.append(f"a typical day moves {vol:.2f}%, under the {fee_pct:.2f}% "
                       f"round-trip fee")
    if notional < min_notional:
        reasons.append(f"${notional:,.0f}/day is below the ${min_notional:,.0f} "
                       f"depth floor - orders would cross the spread and pay taker")

    row["verdict"] = "tradeable" if not reasons else "not viable"
    row["reason"] = "; ".join(reasons) if reasons else (
        f"moves {vol:.2f}% a day against a {fee_pct:.2f}% round trip "
        f"({row['daily_vol_over_fee']}x) on ${notional:,.0f}/day of depth")
    return row


async def scan(session=None, coins=None, *, concurrency=8, days=WINDOW_DAYS,
               fee_pct=ROUND_TRIP_FEE_PCT, min_notional=MIN_24H_NOTIONAL_USD):
    """Measure the whole venue. Returns rows sorted best-first."""
    own = session is None
    if own:
        import aiohttp
        session = aiohttp.ClientSession()
    try:
        pids = coins if coins is not None else await list_usd_products(session)
        sem = asyncio.Semaphore(concurrency)

        async def one(pid):
            async with sem:
                r = await measure_one(session, pid, days=days)
                await asyncio.sleep(0.05)     # the candles endpoint rate-limits
                return assess(r, fee_pct=fee_pct, min_notional=min_notional)

        rows = await asyncio.gather(*[one(p) for p in pids])
    finally:
        if own:
            await session.close()

    tradeable = [r for r in rows if r["verdict"] == "tradeable"]
    tradeable.sort(key=lambda r: -(r.get("daily_vol_pct") or 0))
    rest = [r for r in rows if r["verdict"] != "tradeable"]
    return {
        "scanned": len(rows),
        "tradeable": len(tradeable),
        "not_viable": sum(1 for r in rows if r["verdict"] == "not viable"),
        "no_data": sum(1 for r in rows if r["verdict"] == "no data"),
        "fee_pct": fee_pct,
        "min_notional_usd": min_notional,
        "rows": tradeable + rest,
        "note": ("Measuring a coin is not proposing to trade it. The trading "
                 "universe stays locked to the coins a human named - see "
                 "coin_rotation.universe(). Correlation across this asset class "
                 "runs about +0.574, so more coins is not more bets."),
    }
