"""The entry-vs-chance comparison, pinned.

This tool decides whether the bot's entry timing is worth anything. Three
ways it could lie, and a test for each:

  * scoring an entry whose horizon runs off the end of the data, which
    turns "we don't know" into a small loss
  * dropping unfilled entries, which is the survivorship bias the live
    ledger already has - winners close and get counted, losers stay open
  * a control that does not face the same censoring as the real entry,
    which measures the censoring instead of the strategy
"""
import entry_alpha as EA
import random

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


# 5-minute bars, flat at 100 unless a spike is inserted.
def series(n=200, base=100.0):
    times = [1_700_000_000 + i * 300 for i in range(n)]
    return times, [base] * n, [base] * n, [base] * n


print("\nthe fee is the MEASURED one, not a round number")

ok("maker round trip is 0.70%, both legs at the measured 0.35%",
   EA.MAKER_ROUND_TRIP_PCT == 0.70)
ok("taker round trip is 1.50%", EA.TAKER_ROUND_TRIP_PCT == 1.50)
ok("0.50% is neither, and is not used anywhere",
   0.50 not in (EA.MAKER_ROUND_TRIP_PCT, EA.TAKER_ROUND_TRIP_PCT))

print("\nan entry whose horizon runs off the end is UNRESOLVED, not a loss")

times, lows, highs, closes = series(200)
ok("an entry with room resolves",
   EA.resolve_entry(times, highs, lows, closes, 0, 1.0, 3600) is not None)
ok("an entry near the end returns None",
   EA.resolve_entry(times, highs, lows, closes, 199, 1.0, 3600) is None,
   "scoring it would book an unknown as a small loss")
ok("exactly one horizon short of the end still returns None",
   EA.resolve_entry(times, highs, lows, closes, 190, 1.0, 3600) is None)

print("\nan UNFILLED entry is marked and charged, never dropped")

r = EA.resolve_entry(times, highs, lows, closes, 0, 1.0, 3600)
ok("a flat market never reaches a +1% rung", r["filled"] is False)
ok("and is still scored", r is not None)
ok("its gross is the flat mark, ~0%", abs(r["gross_pct"]) < 1e-9, r["gross_pct"])
ok("and it is charged a full round trip",
   abs(r["net_pct"] + EA.MAKER_ROUND_TRIP_PCT) < 1e-9, r["net_pct"])

print("\na FILLED entry books the target, not the close")

t2, l2, h2, c2 = series(200)
for i in range(5, 12):
    h2[i] = 105.0          # spikes through a +1% rung
c2[50] = 80.0              # and then the market collapses afterwards
r = EA.resolve_entry(t2, h2, l2, c2, 0, 1.0, 3600)
ok("it fills", r["filled"] is True)
ok("it books the rung, +1.0%", abs(r["gross_pct"] - 1.0) < 1e-9, r["gross_pct"])
ok("a later collapse cannot un-fill it", r["net_pct"] > -EA.MAKER_ROUND_TRIP_PCT)

print("\nMAE is recorded, so a lucky exit does not hide a bad entry")

t3, l3, h3, c3 = series(200)
for i in range(1, 6):
    l3[i] = 90.0           # -10% before anything good happens
for i in range(6, 12):
    h3[i] = 105.0
r = EA.resolve_entry(t3, h3, l3, c3, 0, 1.0, 3600)
ok("it still fills", r["filled"] is True)
ok("but the -10% excursion is on the record",
   r["mae_pct"] < -9.0, r["mae_pct"])

print("\nnearest_index never clamps")

ok("a timestamp inside the range finds its bar",
   EA.nearest_index(times, times[10] + 60) == 10)
ok("exactly on a bar finds that bar", EA.nearest_index(times, times[10]) == 10)
ok("before the series returns None",
   EA.nearest_index(times, times[0] - 1) is None,
   "clamping would score an entry against a moment the trade never saw")
ok("after the series returns None", EA.nearest_index(times, times[-1] + 1) is None)
ok("an empty series returns None", EA.nearest_index([], 123) is None)

print("\ncontrols face the SAME censoring as the real entry")

rng = random.Random(1)
idx = EA.control_indices(times, 0, 3600, 50, rng)
ok("fifty controls are drawn", len(idx) == 50)
ok("none of them is too close to the end to resolve",
   all(EA.resolve_entry(times, highs, lows, closes, j, 1.0, 3600) is not None
       for j in idx),
   "a control that cannot resolve would silently shrink the control sample")
ok("a series too short for the horizon yields no controls",
   EA.control_indices(times[:5], 0, 3600, 10, rng) == [])

print("\nalpha is real minus control, and says which way")

real = [{"filled": True, "gross_pct": 2.0, "net_pct": 1.3, "mae_pct": -1.0}] * 10
ctrl = [{"filled": True, "gross_pct": 1.0, "net_pct": 0.3, "mae_pct": -1.0}] * 10
a = EA.alpha(real, ctrl)
ok("alpha is the difference in mean net", abs(a["alpha_pct"] - 1.0) < 1e-9)
ok("and it is named in plain words", a["verdict"] == "entries beat chance")
worse = EA.alpha(ctrl, real)
ok("a negative alpha is called what it is",
   worse["verdict"] == "entries are WORSE than chance", worse["verdict"])
ok("an empty side gives None, not zero",
   EA.alpha([], ctrl)["real"]["net_pct"] is None,
   "zero would read as 'no edge' when it means 'no data'")

print("\nthe bootstrap is seeded and reports an interval, not a verdict")

import statistics
rng2 = random.Random(7)
noisy_real = [{"filled": True, "gross_pct": 0, "net_pct": rng2.gauss(0.2, 3),
               "mae_pct": 0} for _ in range(166)]
noisy_ctrl = [{"filled": True, "gross_pct": 0, "net_pct": rng2.gauss(0.0, 3),
               "mae_pct": 0} for _ in range(3320)]
b1 = EA.bootstrap_alpha(noisy_real, noisy_ctrl, iterations=2000)
b2 = EA.bootstrap_alpha(noisy_real, noisy_ctrl, iterations=2000)
ok("the same seed gives the same answer", b1 == b2)
ok("it reports a 90% interval", "ci90_low_pct" in b1 and "ci90_high_pct" in b1)
ok("low is below high", b1["ci90_low_pct"] <= b1["ci90_high_pct"])
ok("it says whether the interval crosses zero", isinstance(b1["crosses_zero"], bool))
ok("a noisy small sample is correctly reported as crossing zero",
   b1["crosses_zero"] is True,
   "166 samples of a 0.2-mean, 3-sd series cannot establish a sign")
ok("it does NOT emit a pass/fail p-value",
   not any("p_value" in k or k == "significant" for k in b1))
ok("an empty side gives no bootstrap, not a confident zero",
   EA.bootstrap_alpha([], noisy_ctrl)["share_same_sign_pct"] is None)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
