#!/usr/bin/env python3
"""The stop sweep compared a FRACTION against a PERCENT and never fired.

WHAT IT DID. loss_study.stop_sweep replays each candidate stop against
each trade's recorded max adverse excursion:

    if r["mae_pct"] <= -abs(stop) * 100:      # the bug

mae_pct is stored as a FRACTION (-0.1269 is -12.69%). CANDIDATE_STOPS are
fractions (0.03 is 3%). So for a 3% stop the test asked:

    -0.1269 <= -3.0                           # never true

Every candidate from 3% to 12% therefore reported 0 stopped out and an
IDENTICAL net of $73.34, and the dashboard published "3.0% is best" off a
comparison that could not fire. A real -12.69% excursion was sitting in
the data and no stop level could see it.

It is the same units family as grid_pct, which is also a fraction where a
percent is easy to assume. This one shipped a risk recommendation.

With it fixed the answer REVERSES: a 3% stop would have cut 22 of 81
trades and halved the net.
"""
import json, sys
import loss_study as L

FAILS = []
def ok(label, cond, detail=""):
    if cond: print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        FAILS.append(label)

def trade(pnl, risked, mae_frac):
    """mae_pct in the units the database actually stores: a fraction."""
    return {"pnl": pnl, "qty": 1.0, "entry_price": risked,
            "exit_price": risked + pnl, "mae_pct": mae_frac,
            "closed_at": "2026-09-30T00:00:00"}

print("\n[1] a stop tighter than the excursion MUST cut the trade")
# One trade that went 10% against before closing +$1.
t = [trade(1.0, 100.0, -0.10)]
for stop_pct, should_cut in ((0.03, True), (0.05, True), (0.08, True),
                             (0.12, False)):
    sw = L.stop_sweep(t, candidates=(stop_pct,), min_trades=1)
    row = sw["by_stop"][0]
    ok(f"a {stop_pct*100:.0f}% stop vs a -10% excursion -> "
       f"{'CUT' if should_cut else 'kept'}",
       (row["would_stop_out"] == 1) == should_cut,
       f"stopped_out={row['would_stop_out']}")

print("\n[2] THE BUG: a fraction compared to a percent never fires")
# -0.10 <= -3.0 is False; -0.10 <= -0.03 is True. If the old comparison
# came back, every one of these would report 0.
sw = L.stop_sweep(t, candidates=(0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.12),
                  min_trades=1)
cuts = [o["would_stop_out"] for o in sw["by_stop"]]
ok("not every candidate reports the same thing", len(set(cuts)) > 1,
   f"all candidates returned {cuts} - the comparison is not firing")
nets = [o["net_usd"] for o in sw["by_stop"]]
ok("and not every candidate reports an identical net", len(set(nets)) > 1,
   f"all nets identical at {nets[0]} - this was the live symptom")

print("\n[3] the units are stated, so the next reader cannot re-guess")
src = open("loss_study.py").read()
ok("the comparison line names the units", "mae_pct is stored as a FRACTION" in src)
ok("and the fixed comparison has no stray x100",
   'r["mae_pct"] <= -abs(stop)' in src and 'r["mae_pct"] <= -abs(stop) * 100' not in src)

print("\n[4] monotonic sanity: a tighter stop can only cut MORE, never fewer")
many = [trade(1.0, 100.0, -f/100) for f in (1, 2, 4, 7, 9, 15)]
sw = L.stop_sweep(many, candidates=(0.03, 0.05, 0.08, 0.12), min_trades=1)
by = {o["stop_pct"]: o["would_stop_out"] for o in sw["by_stop"]}
seq = [by[k] for k in sorted(by)]
ok("cut count is non-increasing as the stop widens",
   all(a >= b for a, b in zip(seq, seq[1:])), str(by))

print("\n[5] on the REAL book the answer reverses")
try:
    rt = json.load(open('/tmp/th2.json'))['recent_trades']
except Exception:
    print("  SKIP  no local trade file"); rt = None
if rt:
    sw = L.stop_sweep(rt)
    by = {o["stop_pct"]: o for o in sw["by_stop"]}
    ok("a 3% stop now cuts a substantial number of trades",
       by[3.0]["would_stop_out"] > 10, str(by[3.0]))
    ok("...and nets materially LESS than a wide stop",
       by[3.0]["net_usd"] < by[12.0]["net_usd"],
       f"3%={by[3.0]['net_usd']} vs 12%={by[12.0]['net_usd']}")
    ok("the recommended stop is no longer the tightest candidate",
       sw["best_stop_pct"] != 3.0, str(sw["best_stop_pct"]))

print("\n[6] an unreplayable trade is counted, never assumed")
noe = [trade(1.0, 100.0, None) for _ in range(40)]
sw = L.stop_sweep(noe)
ok("no excursion recorded -> available False, with a reason",
   sw.get("available") is False and "reason" in sw, json.dumps(sw)[:160])
ok("...and it reports how many were usable", sw.get("usable_trades") == 0)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
