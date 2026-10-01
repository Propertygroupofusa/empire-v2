#!/usr/bin/env python3
"""The cost panel must not report BLOCKED once it has stopped being blocked.

WHY. The headline was built from one template with no branch for the
already-swapped-in case:

    "It is not, because {falling_samples_needed} more falling-market
     samples are needed..."

The moment the 30-sample bar was cleared, falling_samples_needed became 0
and the page began printing:

    "It is not, because 0 more falling-market samples are needed"

which is a self-contradiction that reports the fleet as blocked at the
exact moment it stopped being blocked. The account owner read it, saw the
RISING bar shorter than the FALLING bar, and asked for the rising number
to be raised - which would have DELETED the evidence that lowered the bar
in the first place.

That is the cost of stale copy on a money panel, so the two states are
pinned here.
"""
import re, sys
import regime_tag as RT

FAILS = []
def ok(label, cond, detail=""):
    if cond: print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        FAILS.append(label)

SRC = open("routers/trading_dashboard.py").read()
m = re.search(r'@router\.get\("/cost-truth"\)(.*?)\n@router\.', SRC, re.S)
assert m, "cost-truth endpoint not found"
BODY = m.group(1)

print("\n[1] the headline branches on whether the measurement is in force")
ok("there is a branch on may_replace_assumption",
   'view["may_replace_assumption"]' in BODY)
ok("the blocked wording is now inside that branch",
   BODY.index('view["may_replace_assumption"]')
   < BODY.index("more falling-market samples are needed"))

print("\n[2] the two real states produce different, non-contradictory text")
# Enough falling samples -> measured figure in force.
enough = [{"adverse_pct": 0.54, "benchmark_move_pct": -3.0} for _ in range(40)]
v_enough = RT.summarise(enough)
ok("40 falling samples retires the assumption",
   v_enough["may_replace_assumption"] is True)
ok("and nothing more is needed", v_enough["falling_samples_needed"] == 0)

# Not enough -> assumption stands.
few = [{"adverse_pct": 0.54, "benchmark_move_pct": -3.0} for _ in range(5)]
v_few = RT.summarise(few)
ok("5 falling samples does NOT retire it",
   v_few["may_replace_assumption"] is False)
ok("and it says how many more are needed",
   v_few["falling_samples_needed"] == RT.MIN_FALLING_SAMPLES - 5,
   str(v_few["falling_samples_needed"]))

print("\n[3] THE BUG: 'it is not' must never pair with '0 more needed'")
# Reconstruct both headlines the way the endpoint does.
def headline(view, cost_now=1.2435, cost_if=1.1555, in_force=0.5435, fee=0.7):
    if view["may_replace_assumption"]:
        return (f"A round trip is priced at {cost_now}%, and that figure is MEASURED: "
                f"{view['counts'].get('FALLING', 0)} falling-market samples put adverse "
                f"selection at {in_force}%, so the conservative assumption of "
                f"{view['assumed_pct']}% has already been retired")
    return (f"A round trip is priced at {cost_now}% and would be {cost_if}%. It is not, "
            f"because {view['falling_samples_needed']} more falling-market samples are "
            f"needed")

h_enough, h_few = headline(v_enough), headline(v_few)
ok("in-force headline does NOT say 'It is not'", "It is not" not in h_enough, h_enough)
ok("in-force headline does NOT say '0 more'", "0 more" not in h_enough, h_enough)
ok("in-force headline says the figure is MEASURED", "MEASURED" in h_enough)
ok("blocked headline still names a POSITIVE number of samples needed",
   "25 more" in h_few, h_few)
ok("the two states are different sentences", h_enough != h_few)

print("\n[4] more FALLING samples is good, and the panel must say so")
ok("the panel explains the bars are counts, not a scoreboard",
   "how_to_read_the_regime_counts" in BODY)
ok("...and says a taller FALLING bar is GOOD",
   "taller FALLING bar is GOOD" in BODY)
ok("...and warns against tuning RISING upward",
   "remove the evidence" in BODY)

print("\n[5] the figure in force is the WORST regime, never a blend")
mixed = ([{"adverse_pct": 0.10, "benchmark_move_pct": 5.0} for _ in range(900)]
         + [{"adverse_pct": 0.90, "benchmark_move_pct": -5.0} for _ in range(40)])
v = RT.summarise(mixed)
ok("uses the FALLING mean (0.90), not the 900-sample RISING one (0.10)",
   abs(v["adverse_pct_in_force"] - 0.90) < 1e-6, str(v["adverse_pct_in_force"]))
blend = (900*0.10 + 40*0.90) / 940
ok(f"and NOT the blended mean ({blend:.4f})",
   abs(v["adverse_pct_in_force"] - blend) > 0.1)
# This is the guard against exactly what was asked for: swamping the
# sample with rising data must not lower the cost in force.
more_rising = mixed + [{"adverse_pct": 0.10, "benchmark_move_pct": 5.0}
                       for _ in range(5000)]
ok("adding 5,000 MORE rising samples does not move the figure in force",
   abs(RT.summarise(more_rising)["adverse_pct_in_force"]
       - v["adverse_pct_in_force"]) < 1e-9)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
