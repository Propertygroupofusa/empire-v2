"""A profitable slice is not a defect if no sell route was open.

Run as written: python3 test_exit_classifier_knows_the_trigger.py

THE BUG THIS FIXES, measured 2026-10-08 18:38-18:42Z. /grid-status/exit-
classification reported, four reads in a row, a minute apart:

    exit_engine_verdict: INVESTIGATE
    qualified_exit_opportunities_with_no_order: 2, then 3
    why: "profitable past the floor, unlocked, and no working order. This is
          the only shape an exit defect can take."

It was not a defect. The grid does not sell per slice; it sells per BRANCH,
when price reaches reference_price * (1 + grid_pct). Measured at 18:45Z:

    ONDO-USD  slice 219  +8.25% net   target 0.490383, price 0.48338  -> 1.449% short
    XLM-USD   slice 224  +1.58% net   target 0.190328, price 0.189586 -> 0.391% short
    TIA-USD   slice 208  +3.31% net   target 0.510674, price 0.4958   -> 3.000% short

All three were simply short of their branch's own step. The classifier had no
idea the trigger existed, so it graded the engine against a rule the engine
does not use - which is precisely the failure its own docstring was written
to prevent, committed in the module built to prevent it. An alarm that is
always on cannot tell anybody anything.

THE TWO SELL ROUTES, AND WHY SUPPRESSION NEEDS BOTH SHUT.

  1. THE RISE TRIGGER.  price >= reference_price * (1 + grid_pct). Exact,
     and computable from published state.
  2. THE PARKED ROUTE.  A branch as full as its levels cannot buy, so it
     sells any slice past GRID_PARKED_MIN_NET_PCT on its own merit without
     waiting for the rise. That floor is the SAME floor this classifier
     already applies, so a past-floor slice on a parked branch really is
     expected to sell - and no order against it really is a defect.

The engine counts parked on tradeable_slices(), which the caller cannot
reproduce from published state - it can only compute open_slices >=
num_levels, a SUPERSET. So the caller passes `parked_possible`, and the
classifier suppresses the defect ONLY when both routes are shut. The error
direction is deliberate: an over-wide parked reading keeps a false alarm in
a narrow case, where a narrow one would HIDE a real defect. Section [4].
"""

import sys

sys.path.insert(0, ".")
import exit_classifier as ec

FAILS = []


def ok(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


def section(t):
    print("\n" + t)


FLOOR = 0.01


def sl(pid="X-USD", qty=10.0, net=1.0, pct=0.02, entry=1.0, sid=1):
    return {"id": sid, "product_id": pid, "qty": qty, "entry_price": entry,
            "unrealized_net_usd": net, "unrealized_net_pct": pct}


def hold(avail=10.0, locked=0.0):
    return {"available_units": avail, "locked_units": locked,
            "units": avail + locked}


def br(reached=True, parked=False, price=None, target=None):
    return {"rise_trigger_reached": reached, "parked_possible": parked,
            "current_price": price, "target_price": target}


# A slice that is profitable, past the floor, unlocked - the old defect shape.
GOOD = sl(net=5.0, pct=0.05)
FREE = hold(avail=10.0, locked=0.0)

section("[1] the old verdict survives when a route really was open")
b, why = ec.classify_slice(GOOD, FREE, FLOOR, branch=br(reached=True))
ok("rise trigger reached + profitable + unlocked + no order -> still a defect",
   b == ec.QUALIFIED_EXIT_MISSING)
ok("and still names itself the only shape a defect can take", "only shape" in why)
b, _ = ec.classify_slice(GOOD, FREE, FLOOR, branch=br(reached=False, parked=True))
ok("parked branch past the floor -> still a defect, the parked route was open",
   b == ec.QUALIFIED_EXIT_MISSING)

section("[2] THE LIVE FALSE ALARM - both routes shut is not a defect")
b, why = ec.classify_slice(GOOD, FREE, FLOOR,
                           branch=br(reached=False, parked=False))
ok("neither route open -> BRANCH_BELOW_ITS_RISE_TRIGGER",
   b == ec.BRANCH_BELOW_ITS_RISE_TRIGGER)
ok("and it is NOT the defect bucket", b != ec.QUALIFIED_EXIT_MISSING)
ok("and the reason says the grid sells per branch, not per slice",
   "per branch" in why.lower() or "branch" in why.lower())

section("[3] the three real slices from 2026-10-08 18:45Z")
LIVE = [
    # (name, net_pct, price, target, parked_possible)
    ("ONDO-USD slice 219", 0.0825, 0.48338, 0.490383, False),
    ("XLM-USD  slice 224", 0.0158, 0.189586, 0.190328, False),
    ("TIA-USD  slice 208", 0.0331, 0.4958, 0.510674, False),
]
for name, pct, price, target, parked in LIVE:
    reached = price >= target
    b, _ = ec.classify_slice(sl(net=1.0, pct=pct), FREE, FLOOR,
                             branch=br(reached=reached, parked=parked,
                                       price=price, target=target))
    ok(f"{name} is not a defect - {100 * (target - price) / price:.3f}% short "
       f"of its branch step", b == ec.BRANCH_BELOW_ITS_RISE_TRIGGER)

r = ec.classify(
    [sl(pid="ONDO-USD", net=2.14, pct=0.0825, sid=219),
     sl(pid="XLM-USD", net=0.97, pct=0.0158, sid=224),
     sl(pid="TIA-USD", net=0.24, pct=0.0331, sid=208)],
    {"ONDO": FREE, "XLM": FREE, "TIA": FREE}, FLOOR,
    branches={"ONDO-USD": br(False, False, 0.48338, 0.490383),
              "XLM-USD": br(False, False, 0.189586, 0.190328),
              "TIA-USD": br(False, False, 0.4958, 0.510674)})
ok("the whole live case now reads CLEAN, not INVESTIGATE",
   r["exit_engine_verdict"] == "CLEAN")
ok("with zero qualified exits missing",
   r["qualified_exit_opportunities_with_no_order"] == 0)
ok("and all three in the new bucket",
   r["counts"][ec.BRANCH_BELOW_ITS_RISE_TRIGGER] == 3)
ok("investigate_the_sell_engine is False", r["investigate_the_sell_engine"] is False)
ok("their capital is still reported, not hidden",
   r["capital_usd"][ec.BRANCH_BELOW_ITS_RISE_TRIGGER] > 0)

section("[4] THE ERROR DIRECTION - a wide parked reading must not HIDE a defect")
b, _ = ec.classify_slice(GOOD, FREE, FLOOR, branch=br(reached=False, parked=True))
ok("parked_possible is a SUPERSET of the engine's parked, so it keeps the "
   "defect verdict rather than suppressing it",
   b == ec.QUALIFIED_EXIT_MISSING)
ok("suppression requires BOTH routes shut - never one",
   ec.classify_slice(GOOD, FREE, FLOOR, branch=br(True, True))[0]
   == ec.QUALIFIED_EXIT_MISSING)

section("[5] UNKNOWN is still a third verdict - never a quiet pass")
b, why = ec.classify_slice(GOOD, FREE, FLOOR, branch=None)
ok("no branch state supplied -> UNKNOWN_READING, not a defect and not clean",
   b == ec.UNKNOWN_READING)
ok("and it says the trigger was not measured", "not measured" in why.lower()
   or "was not supplied" in why.lower())
ok("rise_trigger_reached None -> UNKNOWN_READING",
   ec.classify_slice(GOOD, FREE, FLOOR, branch=br(reached=None))[0]
   == ec.UNKNOWN_READING)
ok("parked_possible None -> UNKNOWN_READING",
   ec.classify_slice(GOOD, FREE, FLOOR,
                     branch={"rise_trigger_reached": False,
                             "parked_possible": None})[0]
   == ec.UNKNOWN_READING)
ok("an UNKNOWN pile still refuses to read as clean",
   ec.unknown_is_not_clean(
       ec.classify([GOOD], {"X": FREE}, FLOOR, branches=None)) is not None)

section("[6] the new bucket cannot steal slices from the older verdicts")
ok("underwater stays underwater, trigger or no trigger",
   ec.classify_slice(sl(net=-5.0, pct=-0.04), FREE, FLOOR,
                     branch=br(False, False))[0] == ec.UNDERWATER)
ok("a locked currency still reads PROFITABLE_WORKING",
   ec.classify_slice(GOOD, hold(avail=0.0, locked=10.0), FLOOR,
                     branch=br(False, False))[0] == ec.PROFITABLE_WORKING)
ok("below the floor still reads PROFITABLE_BELOW_MIN",
   ec.classify_slice(sl(net=0.06, pct=0.0012), FREE, FLOOR,
                     branch=br(False, False))[0] == ec.PROFITABLE_BELOW_MIN)
ok("an unpriceable slice is still UNKNOWN_READING",
   ec.classify_slice(sl(net=None), FREE, FLOOR,
                     branch=br(False, False))[0] == ec.UNKNOWN_READING)

section("[7] the new bucket is published, and is not the defect bucket")
ok("it is in BUCKETS", ec.BRANCH_BELOW_ITS_RISE_TRIGGER in ec.BUCKETS)
ok("DEFECT_BUCKET is unchanged", ec.DEFECT_BUCKET == ec.QUALIFIED_EXIT_MISSING)
ok("every bucket appears in counts even when empty",
   set(ec.classify([], {}, FLOOR)["counts"]) == set(ec.BUCKETS))
ok("the invariant text now mentions the new bucket, so a reader is not left "
   "to infer it",
   "RISE_TRIGGER" in ec.classify([], {}, FLOOR)["invariant"].upper())

section("[8] never raises on rubbish")
for bad in ("nonsense", 5, [], {"rise_trigger_reached": "yes",
                                "parked_possible": "no"}):
    b, _ = ec.classify_slice(GOOD, FREE, FLOOR, branch=bad)
    ok(f"branch={bad!r} is handled without raising, and is not a defect",
       b in ec.BUCKETS and b != ec.QUALIFIED_EXIT_MISSING)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
