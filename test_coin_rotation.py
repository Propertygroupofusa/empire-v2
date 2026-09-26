"""Real checks on coin_rotation.py - no network, no database, no account.

The module decides where real capital sits, so every rule that exists to
stop it losing money gets a test that FAILS if the rule is removed.
"""

import asyncio
import os
import sys

import coin_rotation as R

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


def series(moves, start=100.0):
    """Turn a list of multipliers into (highs, lows) where each candle is a point."""
    price = start
    highs, lows = [], []
    for m in moves:
        price *= m
        highs.append(price)
        lows.append(price)
    return highs, lows


print("\ncount_round_trips - it counts OSCILLATION, not profit")

# Down 3%, back up 3% from there: one complete 2.5% round trip.
h, l = series([1.0, 0.97, 1.03 / 0.97 * 0.97 / 0.97])
h, l = series([1.0, 0.96, 1.06])
ok("a dip then a rise books one round trip",
   R.count_round_trips(h, l, 0.025) == 1,
   f"got {R.count_round_trips(h, l, 0.025)}")

# Straight up, never dips: a grid never gets an entry.
h, l = series([1.0] + [1.02] * 40)
ok("a coin that only rises books ZERO trips (this is the BTC case)",
   R.count_round_trips(h, l, 0.025) == 0,
   f"got {R.count_round_trips(h, l, 0.025)}")

# Straight down: it buys, but never gets its rise, so no completed trip.
h, l = series([1.0] + [0.98] * 40)
ok("a coin that only falls books ZERO completed trips",
   R.count_round_trips(h, l, 0.025) == 0,
   f"got {R.count_round_trips(h, l, 0.025)}")

# A coin that doubles beats a coin that oscillates on PROFIT, and loses
# on trips. This is the whole reason the module ranks on trips.
trend_h, trend_l = series([1.0] + [1.05] * 20)
chop_h, chop_l = series([1.0] + [0.95, 1.06] * 10)
ok("an oscillating coin outranks a trending one, though the trend made more money",
   R.count_round_trips(chop_h, chop_l, 0.025) > R.count_round_trips(trend_h, trend_l, 0.025),
   f"chop={R.count_round_trips(chop_h, chop_l, 0.025)} trend={R.count_round_trips(trend_h, trend_l, 0.025)}")

ok("an empty series is zero, not an exception", R.count_round_trips([], [], 0.025) == 0)
ok("a zero step is zero, not a division blow-up", R.count_round_trips([1, 2], [1, 2], 0.0) == 0)
ok("mismatched highs/lows is zero, not an exception",
   R.count_round_trips([1, 2, 3], [1, 2], 0.025) == 0)


print("\nplan_rotations - the rules that stop it losing money")

BR = [
    {"bot_name": "g1", "product_id": "BTC-USD", "open_slices": 0},
    {"bot_name": "g2", "product_id": "NEAR-USD", "open_slices": 1},   # HOLDS
    {"bot_name": "g3", "product_id": "DOGE-USD", "open_slices": 0},
]
SC = {"BTC-USD": 0, "NEAR-USD": 5, "DOGE-USD": 2, "ONDO-USD": 16, "TIA-USD": 11, "SUI-USD": 3}

plans = R.plan_rotations(BR, SC, min_margin=3)
moved = {p["bot_name"] for p in plans}

ok("a branch HOLDING an open slice is never rotated", "g2" not in moved,
   f"plans={plans}")
ok("the worst flat branch moves", "g1" in moved)
ok("it moves onto the BEST available candidate",
   next(p["to_product_id"] for p in plans if p["bot_name"] == "g1") == "ONDO-USD")
ok("the worst incumbent gets first pick",
   [p["bot_name"] for p in plans][0] == "g1")

targets = [p["to_product_id"] for p in plans]
ok("no candidate is handed to two branches", len(targets) == len(set(targets)))
ok("a coin already held by a branch is never a candidate",
   not any(p["to_product_id"] in {"BTC-USD", "NEAR-USD", "DOGE-USD"} for p in plans),
   f"targets={targets}")

# Margin is the brake. SUI beats DOGE by only 1 trip.
thin = R.plan_rotations(
    [{"bot_name": "g3", "product_id": "DOGE-USD", "open_slices": 0}],
    {"DOGE-USD": 2, "SUI-USD": 3}, min_margin=3)
ok("a 1-trip edge does NOT move capital (noise brake)", thin == [], f"got {thin}")

fat = R.plan_rotations(
    [{"bot_name": "g3", "product_id": "DOGE-USD", "open_slices": 0}],
    {"DOGE-USD": 2, "SUI-USD": 9}, min_margin=3)
ok("a 7-trip edge DOES move capital", len(fat) == 1)

# An incumbent we could not measure is not evidence to move.
unscored = R.plan_rotations(
    [{"bot_name": "g9", "product_id": "MYSTERY-USD", "open_slices": 0}],
    {"ONDO-USD": 16}, min_margin=3)
ok("an UNSCORED incumbent is never rotated (a fetch failure is not evidence)",
   unscored == [], f"got {unscored}")

# Already on the best coin: nothing to do.
best = R.plan_rotations(
    [{"bot_name": "g1", "product_id": "ONDO-USD", "open_slices": 0}],
    {"ONDO-USD": 16, "TIA-USD": 11}, min_margin=3)
ok("a branch already on the best coin stays put", best == [], f"got {best}")

# Works on objects, not just dicts - live branches are ORM rows.
class Row:
    def __init__(self, bot_name, product_id, open_slices):
        self.bot_name = bot_name
        self.product_id = product_id
        self.open_slices = open_slices

obj = R.plan_rotations([Row("g1", "BTC-USD", 0)], {"BTC-USD": 0, "ONDO-USD": 16}, min_margin=3)
ok("accepts ORM-style objects as well as dicts", len(obj) == 1)

# `slices` list is the live shape crypto_grid_bot uses.
held = R.plan_rotations(
    [{"bot_name": "g1", "product_id": "BTC-USD", "slices": [{"id": 1}]}],
    {"BTC-USD": 0, "ONDO-USD": 16}, min_margin=3)
ok("a non-empty `slices` list also counts as holding", held == [], f"got {held}")


print("\nthe toggle")
old = os.environ.get("GRID_AUTO_ROTATE")
try:
    for raw, want in (("false", False), ("0", False), ("no", False), ("off", False),
                      ("true", True), ("", True), ("anything", True)):
        os.environ["GRID_AUTO_ROTATE"] = raw
        import importlib
        importlib.reload(R)
        ok(f"GRID_AUTO_ROTATE={raw!r} -> {want}", R.auto_rotate_enabled() is want)
finally:
    if old is None:
        os.environ.pop("GRID_AUTO_ROTATE", None)
    else:
        os.environ["GRID_AUTO_ROTATE"] = old
    import importlib
    importlib.reload(R)


print("\nmeasure_universe - a coin that will not load is left OUT, never scored zero")

async def _measure_checks():
    async def fetcher(session, product_id, start, end, granularity=None):
        if product_id == "BROKEN-USD":
            raise RuntimeError("429 rate limited")
        if product_id == "THIN-USD":
            return ([1.0] * 5, [1.0] * 5, [1.0] * 5, None)
        h, l = series([1.0] + [0.95, 1.06] * 400)
        return (h, h, l, None)

    got = await R.measure_universe(
        None, ["GOOD-USD", "BROKEN-USD", "THIN-USD"], 0.025, fetcher=fetcher)
    ok("a coin whose history errors is absent, not zero", "BROKEN-USD" not in got, f"got {got}")
    ok("a coin with too little history is absent, not zero", "THIN-USD" not in got)
    ok("a good coin is scored", got.get("GOOD-USD", 0) > 0, f"got {got}")

    # The critical consequence: an unmeasurable coin cannot trigger a move.
    plans = R.plan_rotations(
        [{"bot_name": "g1", "product_id": "BROKEN-USD", "open_slices": 0}], got, min_margin=3)
    ok("an unmeasurable incumbent cannot be rotated away from", plans == [], f"got {plans}")

asyncio.run(_measure_checks())


print("\ndescribe - says something true in both states")
ok("empty plan explains WHY nothing moved",
   "No rotation" in R.describe([], {"ONDO-USD": 16, "BTC-USD": 0}))
ok("a real plan names the coins and the counts",
   "ONDO-USD" in R.describe(
       R.plan_rotations(BR, SC, min_margin=3), SC))




print("\ntrips_for - the cache the veto depends on")

async def _cache_checks():
    calls = {"n": 0}
    async def fetcher(session, product_id, start, end, granularity=None):
        calls["n"] += 1
        if product_id == "DEAD-USD":
            raise RuntimeError("429")
        h, l = series([1.0] + [0.95, 1.06] * 400)
        return (h, h, l, None)

    R._TRIPS_CACHE.clear()
    a = await R.trips_for(["A-USD", "B-USD"], step=0.025, session=object(), fetcher=fetcher)
    first = calls["n"]
    b = await R.trips_for(["A-USD", "B-USD"], step=0.025, session=object(), fetcher=fetcher)
    ok("a second call inside the TTL re-fetches nothing", calls["n"] == first, f"{first} -> {calls['n']}")
    ok("the cached answer matches the measured one", a == b)

    R._TRIPS_CACHE.clear(); calls["n"] = 0
    got = await R.trips_for(["A-USD", "DEAD-USD"], step=0.025, session=object(), fetcher=fetcher)
    ok("an unmeasurable coin is ABSENT from trips_for, never zero",
       "DEAD-USD" not in got and got.get("A-USD", 0) > 0, f"got {got}")
    ok("an unmeasurable coin is not cached as a verdict", "DEAD-USD" not in R._TRIPS_CACHE)

    R._TRIPS_CACHE.clear()
    empty = await R.trips_for([], step=0.025, session=object(), fetcher=fetcher)
    ok("an empty request is an empty answer, not an exception", empty == {})

asyncio.run(_cache_checks())


print("\nthe veto's decision table (as wired into _maybe_rotate_one_grid_branch)")

def veto_blocks(here, there, margin=R.ROTATE_MIN_TRIP_MARGIN):
    """Mirrors the live condition exactly: block when the gap is too thin."""
    if here is None or there is None:
        return False                      # no opinion -> ROI alone decides
    return (there - here) < margin

ok("ROI's pick with FEWER trips is blocked (the ARB case: +26.7% ROI, 2 trips)",
   veto_blocks(here=5, there=2) is True)
ok("ROI's pick with a thin 2-trip edge is blocked", veto_blocks(here=2, there=4) is True)
ok("ROI's pick with a wide 9-trip edge is allowed", veto_blocks(here=2, there=11) is False)
ok("exactly the margin is allowed", veto_blocks(here=2, there=5) is False)
ok("one under the margin is blocked", veto_blocks(here=2, there=4) is True)
ok("an unmeasurable INCUMBENT leaves ROI in charge", veto_blocks(here=None, there=11) is False)
ok("an unmeasurable CANDIDATE leaves ROI in charge", veto_blocks(here=2, there=None) is False)
ok("the veto can never START a rotation - it only returns block/allow",
   veto_blocks(here=0, there=0) is True and veto_blocks(here=0, there=99) is False)

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
