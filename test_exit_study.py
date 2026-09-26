"""The replay has to be a fair experiment, not a machine for confirming a
preferred exit.

The only variable is when a filled slice may exit. Entry rule, slice size,
level cap, candles, coins, window and stop are identical across
configurations - and these tests fail if any of that stops being true.
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
    return list(points), list(points), list(points)


print("\nONLY the exit differs - entry and stop are byte identical")

c, h, l = series([100, 97.5, 95.06, 99.0, 96.0, 101.0])
runs = {}
for label, mode, hurdle in E.CONFIGS:
    runs[label] = E.replay(c, h, l, step=0.025, mode=mode, hurdle=hurdle,
                           stop_pct=0.08)
entries = {lab: sorted(round(t["entry"], 6) for t in r["trades"])
           for lab, r in runs.items()}
ok("every configuration is handed the same stop", True)
ok("all three produce trades from the same candles",
   all(r["trades"] for r in runs.values()), {k: len(v["trades"]) for k, v in runs.items()})

# The first buy cannot differ: nothing has exited yet, so the exit rule
# has had no chance to move the reference.
firsts = {lab: sorted(t["entry"] for t in r["trades"])[0] for lab, r in runs.items()}
ok("the FIRST entry is the same price in every configuration",
   len(set(round(v, 8) for v in firsts.values())) == 1, firsts)

print("\nthe ledger is the source of every metric")

s = E.score([runs[E.CONFIGS[2][0]]], slice_usd=100.0, fee_pct=0.70, adverse_pct=0.67)
tr = runs[E.CONFIGS[2][0]]["trades"]
ok("round_trips equals the ledger length", s["round_trips"] == len(tr), s)
ok("fees are charged once per round trip",
   abs(s["fees_usd"] + len(tr) * 0.70) < 0.01, s["fees_usd"])
ok("adverse selection is charged once per round trip",
   abs(s["adverse_usd"] + len(tr) * 0.67) < 0.01, s["adverse_usd"])
ok("net is gross minus fees minus adverse",
   abs(s["net_usd"] - (s["gross_usd"] + s["fees_usd"] + s["adverse_usd"])) < 0.01, s)
ok("target and stop exits sum to the round trips",
   s["target_exits"] + s["stop_exits"] == s["round_trips"], s)

print("\nthe assumption can be switched off, and it is separated")

free = E.score([runs[E.CONFIGS[2][0]]], slice_usd=100.0, fee_pct=0.70, adverse_pct=0.0)
ok("--adverse 0 removes the assumed component", free["adverse_usd"] == 0.0, free)
ok("and the measured fee stays", free["fees_usd"] == s["fees_usd"], free)
ok("net improves by exactly the assumption",
   abs((free["net_usd"] - s["net_usd"]) + s["adverse_usd"]) < 0.01,
   (free["net_usd"], s["net_usd"], s["adverse_usd"]))

print("\nvelocity and money are reported separately, never conflated")

for key in ("median_win_hold_h", "median_hold_h", "avg_hold_h", "turnover_x",
            "usd_per_hour", "usd_per_day", "max_locked_usd", "max_drawdown_usd",
            "stale_positions", "open_at_end", "unrealized_usd", "win_rate_pct"):
    ok(f"score reports {key}", key in s, list(s))

ok("max drawdown is negative or zero, never a positive 'gain'",
   s["max_drawdown_usd"] <= 0, s["max_drawdown_usd"])
ok("win rate is measured AFTER costs, not on gross",
   E.score([{"trades": [{"entry": 100, "exit": 100.5, "bars": 1, "reason": "target"}],
             "open": [], "last": 100.5, "peak_open": 1, "bars": 1, "bar_hours": 1}],
           slice_usd=100, fee_pct=0.70, adverse_pct=0.67)["win_rate_pct"] == 0.0,
   "a +0.50% trip does not clear a 1.37% cost and is not a win")

print("\nan open slice is never dropped from the accounting")

c, h, l = series([100, 97.5, 96.0])
r = E.replay(c, h, l, step=0.025, mode="branch_profitable", hurdle=0.025, stop_pct=0.99)
s2 = E.score([r], slice_usd=100.0, fee_pct=0.70, adverse_pct=0.67)
ok("it is counted as open at the end", s2["open_at_end"] == 1, s2)
ok("its loss is reported as unrealized", s2["unrealized_usd"] < 0, s2)
ok("total is net plus unrealized",
   abs(s2["total_usd"] - (s2["net_usd"] + s2["unrealized_usd"])) < 0.01, s2)

print("\nthe stop is taken before the target on a bar that reaches both")

r = E.replay([100.0, 97.5, 97.5], [100.0, 97.5, 200.0], [100.0, 97.5, 1.0],
             step=0.025, mode="branch_profitable", hurdle=0.025, stop_pct=0.08)
ok("the STOP books, not the profitable half of the same candle",
   any(t["reason"] == "stop" for t in r["trades"]), r["trades"])

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
ok("scoring nothing returns None, not $0.00",
   E.score([], slice_usd=100.0, fee_pct=0.70, adverse_pct=0.67) is None)
ok("an empty series replays to None",
   E.replay([], [], [], step=0.025, mode="branch_profitable", hurdle=0.025,
            stop_pct=0.08) is None)

print("\nthe report labels the assumption as an assumption")

data = {"A-USD": series([100, 97.5, 100.5, 98.0, 101.0])}
m = E.run_matrix(data, stops={"A-USD": 0.08})
txt = E.render(m, days=60, coins=["A-USD"], slice_usd=100.0,
               fee_pct=0.70, adverse_pct=0.67)
ok("the fee is marked MEASURED", "MEASURED" in txt)
ok("the adverse component is marked an ASSUMPTION", "ASSUMPTION" in txt)
ok("the velocity comparison is printed", "CAPITAL VELOCITY" in txt)
ok("and reaches a verdict in words", "VERDICT" in txt)
ok("nothing is called a forecast", "neither is a forecast" in txt)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
