"""The replay has to be a fair comparison, not a machine for confirming a
preferred exit mode.

Every rule that keeps it fair gets a test: the same candles drive every
configuration, the worst intra-bar ordering is used, unrealized is never
dropped, and a coin that will not load is left out rather than counted as
a free flat row.
"""

import asyncio

import exit_study as E

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


def series(points):
    """closes == highs == lows, so a bar triggers only at its own price."""
    return list(points), list(points), list(points)


print("\nthe arithmetic of one trip is the step minus the fee, and nothing else")

c, h, l = series([100, 97.5, 100.5])   # dip 2.5%, then rise past the target
r = E.replay(c, h, l, step=0.025, mode="branch", hurdle=0.025,
             stop_pct=E.NO_STOP, fee_pct=0.70)
ok("a dip then a rise books one trip", r["trips"] == 1, r)
ok("and nets the step minus the fee",
   abs(r["realized_pct"] - (2.5 - 0.70)) < 0.05, r["realized_pct"])
ok("leaving nothing open", r["open"] == 0 and r["unrealized_pct"] == 0, r)

r0 = E.replay(c, h, l, step=0.025, mode="branch", hurdle=0.025,
              stop_pct=E.NO_STOP, fee_pct=0.0)
ok("a zero fee keeps the whole step", abs(r0["realized_pct"] - 2.5) < 0.05, r0)

print("\nthe stop is checked BEFORE the sell - the worse ordering within a bar")

# One bar reaches both the stop and the sell target. A fair replay must
# take the stop, not cherry-pick the profitable half of the same candle.
closes = [100.0, 97.5, 97.5]
highs = [100.0, 97.5, 200.0]
lows = [100.0, 97.5, 1.0]
r = E.replay(closes, highs, lows, step=0.025, mode="branch", hurdle=0.025,
             stop_pct=0.08, fee_pct=0.70)
ok("a bar that touches both books the STOP", r["stops"] == 1, r)
ok("and the trip is realized as a loss", r["realized_pct"] < 0, r["realized_pct"])

print("\nan open slice is never quietly dropped")

c, h, l = series([100, 97.5, 96.0])     # buys, then falls and stays down
r = E.replay(c, h, l, step=0.025, mode="branch", hurdle=0.025,
             stop_pct=E.NO_STOP, fee_pct=0.70)
ok("the slice is still open at the end", r["open"] == 1, r)
ok("and its loss is reported as unrealized", r["unrealized_pct"] < 0, r)
ok("total is realized plus unrealized",
   abs(r["total_pct"] - (r["realized_pct"] + r["unrealized_pct"])) < 1e-9, r)

print("\nper-slice exits differ from the branch gate in the way claimed")

# Price dips twice, then rises to just past the SECOND slice's own target
# but not past the branch gate measured from the last fill.
c, h, l = series([100, 97.5, 95.0625, 96.8])
branch = E.replay(c, h, l, step=0.025, mode="branch", hurdle=0.025,
                  stop_pct=E.NO_STOP, fee_pct=0.70)
per = E.replay(c, h, l, step=0.025, mode="slice", hurdle=0.0167,
               stop_pct=E.NO_STOP, fee_pct=0.70)
ok("the branch gate leaves both slices open", branch["open"] == 2, branch)
ok("a per-slice hurdle releases one of them", per["open"] < branch["open"], per)
ok("which is the whole claim being tested", per["trips"] > branch["trips"])

print("\nthe comparison is fair: same candles, every configuration")

data = {"A-USD": series([100, 97.5, 100.5, 98.0, 101.0]),
        "B-USD": series([50, 48.75, 50.3, 49.0, 51.0])}
m = E.run_matrix(data, fee_pct=0.70,
                 stops={"none": {p: E.NO_STOP for p in data}})
ok("every exit mode is run", len({k[0] for k in m}) == 3, list(m))
ok("each aggregate counts both coins", all(a["coins"] == 2 for a in m.values()), m)

print("\na coin that will not load is left out, not counted as a free flat row")


def fetcher_for(table):
    async def _f(session, pid, start, end, granularity=3600):
        if pid not in table:
            raise RuntimeError("HTTP 404")
        return table[pid]
    return _f


got = asyncio.run(E.fetch(["A-USD", "GONE-USD"], 60,
                          fetcher=fetcher_for({"A-USD": series([100, 97.5, 100.5])}),
                          session=object()))
ok("the missing coin is absent", "GONE-USD" not in got and "A-USD" in got, list(got))
ok("a zero row would have flattered every config equally - it is not there",
   E.aggregate([E.replay(*got["A-USD"], step=0.025, mode="branch", hurdle=0.025,
                         stop_pct=E.NO_STOP, fee_pct=0.70)])["coins"] == 1)

ok("an empty series returns None rather than a zero result",
   E.replay([], [], [], step=0.025, mode="branch", hurdle=0.025,
            stop_pct=0.08, fee_pct=0.7) is None)
ok("aggregating nothing returns None, not $0.00", E.aggregate([]) is None)

print("\nthe report states what it assumes")

txt = E.render(m, fee_pct=0.70, days=60)
ok("it prints realized and unrealized separately",
   "realized" in txt and "unreal" in txt)
ok("it says fills are assumed at the trigger price",
   "fill at its trigger price" in txt)
ok("it refuses to call any of it a forecast", "neither is a forecast" in txt)
ok("it names the best configuration outright", "Best:" in txt)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
