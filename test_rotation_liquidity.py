"""A thin book must never be rotated onto, however well it oscillates.

count_round_trips ranks on oscillation, and an illiquid coin has the most
of it - its price jumps because nobody is there. Measured live 2026-09-26,
60-day trips at a 2.50% step against 24h notional:

    SAND   11 trips    $111,534/day
    SNX    10 trips    $209,336/day
    ONDO   14 trips $15,915,661/day

Ranking on trips alone puts SAND and SNX into the fleet ahead of coins
with twenty times the depth. On a book that thin the grid does not get the
maker fill it is priced for - it crosses the spread and pays taker (1.50%
round trip) against a 2.50% step.
"""

import asyncio
import os

# The candidate set is LOCKED to a human-named list (see coin_rotation's
# universe()). These tests are about the depth floor, so the list is named
# explicitly here - which is also the proof that no amount of liquidity or
# oscillation lets a coin in that a human did not put on the list.
os.environ["GRID_COIN_UNIVERSE"] = "FLOKI-USD,SAND-USD,FIL-USD,A-USD,B-USD"

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


FLAT = [{"bot_name": "crypto_grid_5", "product_id": "FLOKI-USD", "open_slices": 0}]
# FLOKI 3 trips is the incumbent; both candidates clear the 3-trip margin.
SCORES = {"FLOKI-USD": 3, "SAND-USD": 11, "FIL-USD": 8}
DEEP = {"SAND-USD": 111_534.0, "FIL-USD": 2_936_171.0}


print("\nthe liquidity floor decides WHAT MONEY MOVES ONTO")

plans = R.plan_rotations(FLAT, SCORES, min_margin=3)
ok("with no depth map at all the floor is off and the best oscillator wins",
   len(plans) == 1 and plans[0]["to_product_id"] == "SAND-USD",
   f"got {plans}")

plans = R.plan_rotations(FLAT, SCORES, min_margin=3, liquidity=DEEP,
                         min_notional=750_000)
ok("the thin top-scorer is refused and the deep one takes the branch",
   len(plans) == 1 and plans[0]["to_product_id"] == "FIL-USD",
   f"got {plans}")

ok("and the refusal is on depth, not on trips - it still beat the incumbent",
   SCORES["SAND-USD"] > SCORES["FIL-USD"] > SCORES["FLOKI-USD"])

plans = R.plan_rotations(FLAT, SCORES, min_margin=3,
                         liquidity={"SAND-USD": 111_534.0}, min_notional=750_000)
ok("a candidate MISSING from the depth map is refused, not assumed deep",
   plans == [], f"got {plans}")

plans = R.plan_rotations(FLAT, SCORES, min_margin=3,
                         liquidity={"SAND-USD": 9e9, "FIL-USD": 9e9},
                         min_notional=750_000)
ok("a floor that everything clears changes nothing",
   len(plans) == 1 and plans[0]["to_product_id"] == "SAND-USD",
   f"got {plans}")

print("\nthe floor gates ARRIVALS only - it never force-sells an incumbent")

# FLOKI is the incumbent and is itself thin. It must not be moved BECAUSE
# it is thin; it moves only because something better and deeper exists.
thin_incumbent = R.plan_rotations(
    FLAT, {"FLOKI-USD": 3, "FIL-USD": 8}, min_margin=3,
    liquidity={"FLOKI-USD": 1.0, "FIL-USD": 2_936_171.0}, min_notional=750_000)
ok("an incumbent below the floor is not tested against it",
   len(thin_incumbent) == 1 and thin_incumbent[0]["from_product_id"] == "FLOKI-USD",
   f"got {thin_incumbent}")

no_better = R.plan_rotations(
    FLAT, {"FLOKI-USD": 3, "FIL-USD": 4}, min_margin=3,
    liquidity={"FIL-USD": 2_936_171.0}, min_notional=750_000)
ok("a thin incumbent with nothing better stays put rather than being dumped",
   no_better == [], f"got {no_better}")

print("\nmeasure_liquidity reads depth without inventing any")


def fetcher_for(table):
    async def _f(pid):
        if pid not in table:
            raise RuntimeError("HTTP 404")
        return table[pid]
    return _f


# Coinbase candles are [time, low, high, open, close, volume], newest first.
rows = [[0, 1.0, 1.0, 1.0, 2.0, 100.0]] * 24
got = asyncio.run(R.measure_liquidity(None, ["A-USD"], fetcher=fetcher_for({"A-USD": rows})))
ok("notional is volume x close summed over the window",
   abs(got.get("A-USD", 0) - 24 * 200.0) < 1e-6, f"got {got}")

got = asyncio.run(R.measure_liquidity(None, ["A-USD", "B-USD"],
                                      fetcher=fetcher_for({"A-USD": rows})))
ok("a coin whose depth will not load is LEFT OUT, never zeroed",
   "B-USD" not in got and "A-USD" in got, f"got {got}")

ok("and being left out is what the planner refuses on",
   R.plan_rotations([{"bot_name": "b", "product_id": "B-USD", "open_slices": 0}],
                    {"B-USD": 1, "A-USD": 9}, min_margin=3,
                    liquidity=got, min_notional=1.0) != [],
   "A-USD was measured and should still be offered")

got = asyncio.run(R.measure_liquidity(None, ["A-USD"],
                                      fetcher=fetcher_for({"A-USD": rows[:3]})))
ok("too few candles to judge is also LEFT OUT rather than scored thin",
   got == {}, f"got {got}")

got = asyncio.run(R.measure_liquidity(None, ["A-USD"],
                                      fetcher=fetcher_for({"A-USD": "not a list"})))
ok("a junk response is left out rather than crashing the sweep",
   got == {}, f"got {got}")

print("\nthe locked list still outranks every measurement")

os.environ["GRID_COIN_UNIVERSE"] = "FLOKI-USD"
ok("a deep, high-scoring coin OFF the human's list is still not a candidate",
   R.plan_rotations(FLAT, SCORES, min_margin=3, liquidity={"FIL-USD": 9e9},
                    min_notional=1.0) == [],
   "the universe lock must beat both trips and depth")
os.environ["GRID_COIN_UNIVERSE"] = "FLOKI-USD,SAND-USD,FIL-USD,A-USD,B-USD"

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
