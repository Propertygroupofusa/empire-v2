"""How many regimes, measured - and the confound that nearly faked one.

THE OWNER'S INSTRUCTION, 2026-10-05: "Different types of tests to see how
many regimes there are."

THE CENTRAL TEST IN THIS FILE IS NOT ABOUT CLASSIFICATION. It is that a
regime the fleet never traded through reads as UNOBSERVED and never as bad.
On the full 31-day candle window UP/HIVOL showed 7 days and ZERO closes,
which reads as "this strategy cannot earn in a rising volatile market" - a
large claim with an obvious fix attached, and false. Sep 10-25 is a
contiguous 16-day stretch where the fleet closed nothing in ANY regime
because it was not trading, and all 7 UP/HIVOL days sit inside it. The dead
ledger was the cause, not the weather. Every assertion below that separates
`observed` from `profitable` exists because of that.

MEASURED, live window 2026-09-26..2026-10-05, 125 own closes:
    DOWN/HIVOL  3 days  27.7 closes/day  $28.46/day  1.436% per close
    FLAT/HIVOL  2 days   9.5 closes/day   $8.11/day  2.653%
    UP/LOVOL    1 day    8.0 closes/day   $8.43/day  2.299%
    FLAT/LOVOL  3 days   4.0 closes/day   $4.82/day  2.687%
    DOWN/LOVOL  1 day    3.0 closes/day   $1.94/day  1.312%

Run: python3 test_regime_classifier.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import regime_classifier as rc

FAIL = []
def ok(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond: FAIL.append(name)


print("\n=== 1. six regimes exist and are named ===")
ok("3 trends x 2 volatility states = 6", len(rc.ALL_REGIMES) == 6)
ok("no duplicates", len(set(rc.ALL_REGIMES)) == 6)
ok("every label is TREND/VOL", all("/" in r for r in rc.ALL_REGIMES))

print("\n=== 2. the boundaries, measured 2026-10-05 ===")
D, U, V = (rc.DEFAULT_TREND_DOWN_PCT, rc.DEFAULT_TREND_UP_PCT,
           rc.DEFAULT_VOL_SPLIT_PCT)
ok("a -5.33% day on 7.75% range is DOWN/HIVOL",
   rc.classify(-5.33, 7.75) == "DOWN/HIVOL")
ok("a +7.14% day on 8.71% range is UP/HIVOL", rc.classify(7.14, 8.71) == "UP/HIVOL")
ok("a +0.12% day on 1.52% range is FLAT/LOVOL", rc.classify(0.12, 1.52) == "FLAT/LOVOL")
ok("today, -1.03% on 4.60%, is DOWN/LOVOL", rc.classify(-1.03, 4.60) == "DOWN/LOVOL")
ok("exactly AT the down threshold is FLAT, not DOWN", rc.classify(D, 1.0).startswith("FLAT"))
ok("exactly AT the up threshold is FLAT, not UP", rc.classify(U, 1.0).startswith("FLAT"))
ok("exactly AT the volatility split is LOVOL", rc.classify(0.0, V).endswith("LOVOL"))
ok("an unreadable return is None, never FLAT", rc.classify(None, 5.0) is None)
ok("an unreadable range is None, never LOVOL", rc.classify(0.0, None) is None)

print("\n=== 3. a thin day has no shape, rather than a zero one ===")
bar = [1, 1.0, 1.1, 1.0, 1.05, 100]
ok("four bars is enough", rc.day_shape([bar] * 4) is not None)
ok("three bars is not", rc.day_shape([bar] * 3) is None)
ok("no bars at all is None", rc.day_shape([]) is None)
ok("None is None", rc.day_shape(None) is None)
sh = rc.day_shape([[1, 10.0, 12.0, 10.0, 11.0, 1]] * 4)
ok("return is close over open", abs(sh["return_pct"] - 10.0) < 1e-9)
ok("range is high over low", abs(sh["range_pct"] - 20.0) < 1e-9)
ok("a zero open is refused, never a divide",
   rc.day_shape([[1, 0.0, 0.0, 0.0, 1.0, 1]] * 4) is None)

print("\n=== 4. thresholds refit, but never on a handful of days ===")
mk = lambda r, v: {"return_pct": r, "range_pct": v}
ok("8 days is refused - terciles of noise are not a distribution",
   rc.fit([mk(i, i) for i in range(8)]) is None)
f = rc.fit([mk(i, i) for i in range(9)])
ok("9 days fits", f is not None and f["fitted_on_days"] == 9)
ok("and the fit is ordered down < up", f["down"] < f["up"])
ok("an empty window is refused", rc.fit([]) is None)
ok("unreadable rows are dropped, not counted",
   rc.fit([mk(1, 1)] * 9 + [mk(None, None)] * 5)["fitted_on_days"] == 9)

print("\n=== 5. THE CONFOUND - an unobserved regime is never called bad ===")
# The live window, exactly as measured. UP/HIVOL is absent from it.
LIVE = {"2026-09-26": "FLAT/LOVOL", "2026-09-27": "FLAT/HIVOL",
        "2026-09-28": "DOWN/HIVOL", "2026-09-29": "DOWN/HIVOL",
        "2026-09-30": "FLAT/LOVOL", "2026-10-01": "FLAT/HIVOL",
        "2026-10-02": "DOWN/HIVOL", "2026-10-03": "FLAT/LOVOL",
        "2026-10-04": "UP/LOVOL",   "2026-10-05": "DOWN/LOVOL"}
C = {"2026-09-26": [{"pnl": 0.06, "notional": 10.0}],
     "2026-09-27": [{"pnl": 0.96, "notional": 40.0}] * 13,
     "2026-09-28": [{"pnl": 1.19, "notional": 60.0}] * 35,
     "2026-09-29": [{"pnl": 0.29, "notional": 60.0}] * 24,
     "2026-09-30": [{"pnl": 1.63, "notional": 55.0}] * 7,
     "2026-10-01": [{"pnl": 0.61, "notional": 50.0}] * 6,
     "2026-10-02": [{"pnl": 1.53, "notional": 70.0}] * 24,
     "2026-10-03": [{"pnl": 0.74, "notional": 45.0}] * 4,
     "2026-10-04": [{"pnl": 1.05, "notional": 50.0}] * 8,
     "2026-10-05": [{"pnl": 0.65, "notional": 50.0}] * 3}
t = rc.tally(LIVE, C)
ok("five regimes observed in the live window", t["observed_count"] == 5)
ok("UP/HIVOL is reported as UNOBSERVED", t["unobserved"] == ["UP/HIVOL"])
ok("it is NOT in the results at all - absence is not a zero row",
   "UP/HIVOL" not in {r["regime"] for r in t["regimes"]})
ok("and the payload says plainly that unobserved means no data",
   "NO DATA" in t["unobserved_is"] and "not a zero" in t["unobserved_is"])
ok("the detail names it as unobserved rather than worst",
   "Unobserved: UP/HIVOL" in t["detail"] and t["worst"] != "UP/HIVOL")

print("\n=== 6. what the live window actually produced ===")
by = {r["regime"]: r for r in t["regimes"]}
ok("DOWN/HIVOL is the best day rate", t["best"] == "DOWN/HIVOL")
ok("DOWN/LOVOL is the worst", t["worst"] == "DOWN/LOVOL")
ok("DOWN/HIVOL ran 27.7 closes a day", by["DOWN/HIVOL"]["closes_per_day"] == 27.67)
ok("DOWN/LOVOL ran 3.0", by["DOWN/LOVOL"]["closes_per_day"] == 3.0)
ok("high-volatility days out-close every low-volatility day",
   min(by[r]["closes_per_day"] for r in by if r.endswith("HIVOL"))
   > max(by[r]["closes_per_day"] for r in by if r.endswith("LOVOL")))
ok("the spread between best and worst day rate is reported",
   t["day_rate_spread_x"] and t["day_rate_spread_x"] > 10)
ok("every regime's days are counted", sum(r["days"] for r in t["regimes"]) == 10)
ok("every close is counted once", sum(r["closes"] for r in t["regimes"]) == 125)

print("\n=== 7. a thin regime reports no rate rather than a fake one ===")
thin = rc.tally({"d1": "UP/LOVOL"}, {"d1": [{"pnl": 9.0, "notional": 10.0}] * 2})
r0 = thin["regimes"][0]
ok("two closes give no net% per close", r0["net_pct_per_close"] is None)
ok("and it is flagged as not evidence", r0["rate_is_evidence"] is False)
ok("but the dollars are still counted", r0["realized_usd"] == 18.0)
fat = rc.tally({"d1": "UP/LOVOL"}, {"d1": [{"pnl": 9.0, "notional": 10.0}] * 3})
ok("three closes do give a rate", fat["regimes"][0]["net_pct_per_close"] == 90.0)

print("\n=== 8. a day the fleet never traded contributes nothing but a day ===")
z = rc.tally({"d1": "UP/HIVOL"}, {})
ok("a live day with no closes is still a day", z["regimes"][0]["days"] == 1)
ok("with zero closes", z["regimes"][0]["closes"] == 0)
ok("and no rate claimed from it", z["regimes"][0]["net_pct_per_close"] is None)
ok("empty input does not crash", rc.tally({}, {})["observed_count"] == 0)
ok("None input does not crash", rc.tally(None, None)["observed_count"] == 0)

print("\n=== 9. it reads nothing and moves nothing ===")
CODE = "\n".join(l for l in open("regime_classifier.py").read().splitlines()
                 if not l.strip().startswith("#"))
for forbidden in ("place_order", "db.commit", "requests.", "aiohttp",
                  "os.getenv", "await "):
    ok(f"never references {forbidden}", forbidden not in CODE)

print(f"\n{'ALL PASSED' if not FAIL else str(len(FAIL)) + ' FAILED'}")
for f in FAIL: print("   FAILED:", f)
sys.exit(1 if FAIL else 0)
