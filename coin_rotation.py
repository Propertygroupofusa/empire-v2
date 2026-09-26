"""Move a FLAT grid branch onto a coin that actually oscillates.

WHY THIS EXISTS, 2026-09-26
===========================

The fleet spent 21 days without a single buy. The proximate cause was a
stale reference (see patches/auto-reanchor.md), but underneath it was a
duller problem: the branches were parked on coins that do not move enough
to trigger anything.

Measured on real candles, complete 2.50% round trips per coin:

    coin    first 60d   next 60d
    BTC             0          0      <- branch 1 held this
    ETC             0          3
    BCH             0          3
    DOGE            0          2
    ARB             2          2
    NEAR            5          5
    ...
    ONDO           16         11
    TIA            11          9

A grid on BTC at a 2.50% step is not a slow strategy. It is a stopped one.


WHY IT RANKS ON SIXTY DAYS AND NOT THIRTY
=========================================

The obvious design - rank coins on recent performance, move to the
winners - is a noise chaser at short lookbacks. Rank persistence between
one window and the next, measured on the same 24 coins:

    lookback     rank correlation     top-8 vs all coins
    15d -> 15d           +0.183       2.50 vs 2.29 trips   (nothing)
    60d -> 60d           +0.344       5.12 vs 3.29 trips   (+56%)

At 15 days the ranking is worthless and acting on it is actively harmful:
over one such window JUP went #1 -> #24 and ARB went #19 -> #2. Selling
ARB to buy JUP on that evidence would have been exactly backwards.

At 60 days a real, monotonic separation appears - bottom-8 delivered 2.12
trips where top-8 delivered 5.12. That is the window this module uses.

+0.344 is weak-to-moderate, not strong. Roughly seven-eighths of what a
coin does next is NOT explained by what it just did. This module will be
wrong about individual coins routinely and is only expected to be right
on average, which is why the margin below is deliberately coarse.


WHY THE DECISION RUNS EVERY CYCLE ANYWAY
========================================

A long lookback is not the same as a slow decision, and conflating them
was the first draft's mistake. The account owner's correction: "it's
going to always change, it's not going to stay the same."

That is right, and it rules out a calendar. A fortnightly rotation lock
assumes you can pick correctly and then rest; against a +0.344 signal
there is no resting. So: measure over a long window because that is where
signal lives, and re-decide on every cycle because the world does not
wait. The brake is an EVIDENCE threshold (ROTATE_MIN_TRIP_MARGIN), not a
clock - capital moves the moment the gap is real and not before.


SAFETY
======

  * FLAT branches only. A branch holding an open slice is never rotated;
    its reference is also its sell trigger and its coin is where its
    money actually is.
  * Moving a flat branch costs NOTHING. allocated_usd is a virtual claim
    on one shared Coinbase wallet, so re-pointing a flat branch places no
    order and pays no fee. This is the whole reason rotation is cheap
    enough to do on evidence this weak.
  * No coin is ever assigned to two branches at once.
  * A branch is only moved onto a STRICTLY better coin, by a margin.
  * GRID_AUTO_ROTATE=false disables it entirely.

This module DECIDES. It deliberately owns no database handle and places
no orders - the caller applies the plan, so the decision can be unit
tested against fixtures with no network and no live account.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


# Sixty days, for the reason argued at the top. Shortening this is the
# single easiest way to turn this module into a noise chaser.
ROTATE_LOOKBACK_DAYS = int(os.getenv("GRID_ROTATE_LOOKBACK_DAYS", "60"))

# Hourly candles. Finer than the 4h resolution the +0.344 figure was
# measured at, so trip counts run higher here than in that table - the
# RANKING is what matters and a finer series can only sharpen it.
ROTATE_GRANULARITY_SECONDS = int(os.getenv("GRID_ROTATE_GRANULARITY", "3600"))

# How many more round trips a candidate must have delivered over the
# lookback before it is worth moving to. This is the brake, and it is
# deliberately coarse: against a +0.344 signal a one- or two-trip edge is
# inside the noise, and acting on it churns capital for nothing.
ROTATE_MIN_TRIP_MARGIN = int(os.getenv("GRID_ROTATE_MIN_TRIP_MARGIN", "3"))

# A coin that cannot even be measured is not a candidate. Guards against
# ranking a coin #1 on four candles of history.
ROTATE_MIN_CANDLES = int(os.getenv("GRID_ROTATE_MIN_CANDLES", "600"))

# A coin in the locked universe that will not load gets pulled again, not
# replaced. Rate limits are the common cause and they clear in seconds.
ROTATE_FETCH_ATTEMPTS = int(os.getenv("GRID_ROTATE_FETCH_ATTEMPTS", "4"))
ROTATE_FETCH_BACKOFF_SECONDS = float(os.getenv("GRID_ROTATE_FETCH_BACKOFF", "1.5"))


# ---- THE UNIVERSE IS LOCKED, AND SMALL, ON PURPOSE ----
#
# Per the account owner, from experience: "last time I tried to do this and
# go and try to pull all these different coins, they all dragged down and I
# lost a lot of money on that."
#
# That is not caution, it is the correct reading of this asset class.
# Measured on 120 days of real daily returns across 16 coins:
#
#     average pairwise correlation          +0.574
#     BTC / ETH                             +0.880
#     DOGE / SUI                            +0.850
#     days where 80%+ fell TOGETHER         37 of 120  (31%)
#     worst day                             16 of 16 down, -9.15% avg
#
# Sixteen coins is not sixteen bets. It is closer to two or three. And a
# grid makes the concentration worse rather than better, because it BUYS
# dips: on each of those 37 days every branch fills with losing slices at
# once and none of them can sell. Widening the coin list widens that.
#
# So the candidate set is a fixed list, never a scan. With
# GRID_COIN_UNIVERSE unset the universe is exactly the coins the fleet
# ALREADY holds - meaning rotation can reshuffle capital among coins that
# were already chosen, and can never introduce a new one on its own.
COIN_UNIVERSE_ENV = "GRID_COIN_UNIVERSE"

# The account owner's own eight - "you already know my seven, my eight
# coins program, we already was dealing with them coins."
#
# This is a NAMED LIST, not a default that drifts. An earlier version of
# this module defaulted the universe to "whatever the fleet currently
# holds", which looked conservative and was not: after a merge collapsed
# eight branches into three (BTC, NEAR, ARB), that default would have
# frozen the fleet onto exactly those three forever, and they are the
# three WORST oscillators in the set. A default that inherits a bad state
# and calls it policy is worse than no default.
#
# Measured 30-day round trips at a 2.50% step, so the ranking is visible
# rather than asserted:
#
#     BONK 8   FLOKI 6   DOGE 6   BTC 4   ETC 4   NEAR 3   BCH 2   ARB 1
#
# Those eight together offer 34 round trips a month. The three the merge
# left standing offer 8 - the concentration discarded 76% of the fleet's
# own available trades.
DEFAULT_COIN_UNIVERSE = [
    "BONK-USD", "FLOKI-USD", "DOGE-USD", "BTC-USD",
    "ETC-USD", "NEAR-USD", "BCH-USD", "ARB-USD",
]


def configured_universe():
    """The explicit coin list, or None when the fleet's own coins are it."""
    raw = (os.getenv(COIN_UNIVERSE_ENV) or "").strip()
    if not raw:
        return None
    coins = [c.strip().upper() for c in raw.replace(";", ",").split(",") if c.strip()]
    return coins or None


def universe(current_product_ids):
    """Every coin this fleet is allowed to consider, and nothing else.

    Unset env -> the coins already held. That default is deliberate: it
    means the rotation logic can only ever reshuffle money between coins a
    human already put it on, and introducing a new coin stays a human
    decision. Nothing here ever scans a market for something better.
    """
    configured = configured_universe()
    if configured:
        return list(dict.fromkeys(configured))
    # The named eight, PLUS anything the fleet happens to hold that is not
    # on the list. A coin already carrying real money is never made
    # unreachable by a list it predates - the list decides what may be
    # ADDED, it does not strand what is already there.
    return list(dict.fromkeys(
        list(DEFAULT_COIN_UNIVERSE) + [p for p in current_product_ids if p]))


def auto_rotate_enabled() -> bool:
    """Whether flat branches follow the measurement. ON unless switched off.

    Defaults ON because the alternative is what the fleet already did:
    sit on BTC for four months at a 2.50% step, which that coin did not
    clear once. The action it takes places no order and spends nothing,
    and every gate still applies to whatever the branch does next.
    Set GRID_AUTO_ROTATE=false to pin the fleet to its current coins.
    """
    raw = (os.getenv("GRID_AUTO_ROTATE") or "").strip().strip('"').strip("'").lower()
    return raw not in {"0", "false", "no", "off"}


def count_round_trips(highs, lows, step):
    """Complete buy-a-dip / sell-a-rise cycles a real price series allowed.

    This mirrors what the live branch actually does and nothing more: buy
    when price falls `step` below the reference, sell when it rises `step`
    above that entry, then re-anchor the reference to the exit and repeat.
    One slice at a time, so the count is round trips a SINGLE level would
    have completed - not an optimistic fill-every-level figure.

    Deliberately counts TRIPS, not profit. Profit over a lookback is
    contaminated by trend: a coin that went straight up scores well and
    is then a terrible grid candidate, because a grid needs the price to
    come back. Trips measure oscillation, which is the thing a grid eats.

    Uses the candle high and low rather than the close, because a real
    branch checks live price continuously and would have seen the extreme.
    """
    if not highs or not lows or len(highs) != len(lows) or step <= 0:
        return 0
    reference = lows[0]
    entry = None
    trips = 0
    for high, low in zip(highs, lows):
        if entry is None:
            trigger = reference * (1.0 - step)
            if low <= trigger:
                entry = trigger
        else:
            target = entry * (1.0 + step)
            if high >= target:
                trips += 1
                reference = target
                entry = None
    return trips


async def measure_universe(session, product_ids, step, *,
                           days=None, granularity=None, fetcher=None):
    """Round trips each coin actually delivered over the lookback.

    Returns {product_id: trips}. A coin whose history will not load, or
    loads too thin to judge, is LEFT OUT rather than scored zero - a
    fetch failure is not evidence that a coin does not move, and scoring
    it zero would quietly rotate money away from it.
    """
    if fetcher is None:
        import crypto_selection_backtest as CSB
        fetcher = CSB.fetch_candles_window

    days = days or ROTATE_LOOKBACK_DAYS
    granularity = granularity or ROTATE_GRANULARITY_SECONDS
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    scores = {}
    for product_id in product_ids:
        # Keep pulling THIS coin rather than moving on to a different one.
        # The universe is fixed (see universe() above), so a coin that will
        # not load is a coin to retry, never a coin to substitute - the
        # substitution is exactly the behaviour that widened the list last
        # time and cost real money.
        got = None
        last_exc = None
        for attempt in range(1, ROTATE_FETCH_ATTEMPTS + 1):
            try:
                got = await fetcher(session, product_id, start, end,
                                    granularity=granularity)
                break
            except Exception as exc:
                last_exc = exc
                if attempt < ROTATE_FETCH_ATTEMPTS:
                    delay = ROTATE_FETCH_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    log.info(f"[ROTATE] {product_id}: history attempt {attempt} failed "
                             f"({exc}) - retrying in {delay:.1f}s")
                    await asyncio.sleep(delay)
        if got is None:
            log.warning(f"[ROTATE] {product_id}: history unavailable after "
                        f"{ROTATE_FETCH_ATTEMPTS} attempts ({last_exc}) - not scored, so it "
                        f"is neither a candidate nor a reason to move. No other coin is "
                        f"pulled in its place.")
            continue
        if not got or len(got) < 3:
            continue
        _closes, highs, lows = got[0], got[1], got[2]
        if not highs or len(highs) < ROTATE_MIN_CANDLES:
            log.info(f"[ROTATE] {product_id}: only {len(highs) if highs else 0} candles "
                     f"(need {ROTATE_MIN_CANDLES}) - not scored")
            continue
        scores[product_id] = count_round_trips(highs, lows, step)
    return scores


# product_id -> (measured_at_monotonic, trips). Candle history over sixty
# days does not meaningfully change between one five-minute sweep and the
# next, and re-pulling it every sweep would hammer the candle endpoint into
# a rate limit - which, per measure_universe's own contract, reads as "not
# scored" and would silently disable the veto exactly when it fires most.
_TRIPS_CACHE = {}
TRIPS_CACHE_SECONDS = float(os.getenv("GRID_ROTATE_CACHE_SECONDS", str(6 * 3600)))


async def trips_for(product_ids, *, step, session=None, fetcher=None):
    """Cached round-trip counts for a handful of coins.

    Returns {product_id: trips} for whatever could be measured. A coin
    absent from the result was NOT measurable - callers must treat that as
    "no opinion", never as zero, or a rate-limited fetch turns into a
    verdict that a coin is dead.
    """
    import time as _time

    want = [p for p in dict.fromkeys(product_ids) if p]
    fresh, stale = {}, []
    now = _time.monotonic()
    for pid in want:
        hit = _TRIPS_CACHE.get(pid)
        if hit and (now - hit[0]) < TRIPS_CACHE_SECONDS:
            fresh[pid] = hit[1]
        else:
            stale.append(pid)
    if not stale:
        return fresh

    owns_session = session is None
    if owns_session:
        import aiohttp
        session_cm = aiohttp.ClientSession()
        session = await session_cm.__aenter__()
    try:
        measured = await measure_universe(session, stale, step, fetcher=fetcher)
    finally:
        if owns_session:
            await session_cm.__aexit__(None, None, None)

    for pid, trips in measured.items():
        _TRIPS_CACHE[pid] = (now, trips)
    fresh.update(measured)
    return fresh


def plan_rotations(branches, scores, *, min_margin=None):
    """Which FLAT branches should move, and onto what.

    `branches` is an iterable of objects or dicts carrying bot_name,
    product_id, and a truthy open-slice indicator. `scores` is the map
    from measure_universe().

    Returns a list of plans, worst-incumbent first:
        {bot_name, from_product_id, to_product_id, from_trips, to_trips,
         gain, reason}

    Rules, each of which exists because the alternative loses money:

      1. A branch holding an open slice is skipped. Its money is IN that
         coin; re-pointing it would orphan the position.
      2. A coin already held by any branch - flat or not - is not a
         candidate. Two branches on one coin is one bet at double size,
         not diversification, and it is exactly the concentration this
         fleet already has too much of.
      3. A candidate must beat the incumbent by min_margin trips. Against
         a +0.344 signal a smaller gap is noise.
      4. An unscored incumbent is not moved. Not knowing what a coin did
         is not evidence against it.
      5. Best available candidate goes to the worst scoring incumbent, and
         each candidate is consumed once.
    """
    if min_margin is None:
        min_margin = ROTATE_MIN_TRIP_MARGIN

    def _get(b, key):
        return b.get(key) if isinstance(b, dict) else getattr(b, key, None)

    held = set()
    movable = []
    for b in branches:
        product_id = _get(b, "product_id")
        if product_id:
            held.add(product_id)
        open_slices = _get(b, "open_slices")
        if open_slices is None:
            open_slices = _get(b, "slices")
        if open_slices:
            continue
        if product_id not in scores:
            continue
        movable.append((scores[product_id], _get(b, "bot_name"), product_id))

    # Candidates come from the LOCKED universe only. With GRID_COIN_UNIVERSE
    # unset that set is the coins already held, so `candidates` is empty and
    # this function proposes nothing at all - rotation cannot introduce a
    # coin the fleet was not already on. That is the intended default.
    allowed = set(universe(held))
    candidates = sorted(
        ((trips, pid) for pid, trips in scores.items()
         if pid not in held and pid in allowed),
        reverse=True,
    )
    movable.sort()   # worst incumbent first

    plans = []
    used = set()
    for incumbent_trips, bot_name, from_pid in movable:
        for cand_trips, to_pid in candidates:
            if to_pid in used:
                continue
            gain = cand_trips - incumbent_trips
            if gain < min_margin:
                break          # candidates are sorted; nothing later is better
            used.add(to_pid)
            plans.append({
                "bot_name": bot_name,
                "from_product_id": from_pid,
                "to_product_id": to_pid,
                "from_trips": incumbent_trips,
                "to_trips": cand_trips,
                "gain": gain,
                "reason": (
                    f"{from_pid} completed {incumbent_trips} round trip(s) over the last "
                    f"{ROTATE_LOOKBACK_DAYS} days; {to_pid} completed {cand_trips}. "
                    f"Moving a FLAT branch costs no fee and places no order."
                ),
            })
            break
    return plans


def describe(plans, scores):
    """One human-readable block for the log and the dashboard."""
    if not plans:
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:5]
        best = ", ".join(f"{p} {t}" for p, t in ranked)
        return (f"No rotation: no candidate beats a flat branch's own coin by "
                f"{ROTATE_MIN_TRIP_MARGIN}+ round trips over {ROTATE_LOOKBACK_DAYS} days. "
                f"Best measured: {best}.")
    lines = [f"{len(plans)} branch(es) moving onto coins that actually oscillate "
             f"(measured over {ROTATE_LOOKBACK_DAYS} days, margin {ROTATE_MIN_TRIP_MARGIN}+ trips):"]
    for p in plans:
        lines.append(f"  {p['bot_name']}: {p['from_product_id']} ({p['from_trips']} trips) "
                     f"-> {p['to_product_id']} ({p['to_trips']} trips), +{p['gain']}")
    return "\n".join(lines)
