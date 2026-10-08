"""Six answers, one of which is a bug. The rest must never read as one.

Run as written: python3 test_exit_classifier.py

Section [7] replays the REAL book as measured 2026-10-07 03:10Z, so a change
that alters the verdict on live data is caught against ground truth rather
than against a fixture somebody invented.
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


FLOOR = 0.01   # GRID_PARKED_MIN_NET_PCT


def sl(pid="X-USD", qty=10.0, net=1.0, pct=0.02, entry=1.0, sid=1):
    return {"id": sid, "product_id": pid, "qty": qty, "entry_price": entry,
            "unrealized_net_usd": net, "unrealized_net_pct": pct}


def hold(avail=10.0, locked=0.0, units=None):
    return {"available_units": avail, "locked_units": locked,
            "units": units if units is not None else (avail + locked)}


# ADDED 2026-10-08 ALONGSIDE BRANCH_BELOW_ITS_RISE_TRIGGER. Every assertion
# below about the defect bucket was written when "profitable past the floor,
# unlocked, no order" was the whole test. It is not: the grid sells per
# BRANCH, and a slice whose branch never reached its step was never offered
# for sale at all. These tests still assert exactly what they always did -
# the behaviour WITH a sell route open - and OPEN is what says so out loud
# instead of leaving it to a default. The suppression case is pinned in
# test_exit_classifier_knows_the_trigger.py, section [2].
OPEN = {"rise_trigger_reached": True, "parked_possible": False}


section("[1] underwater is the rule working, never a defect")
b, why = ec.classify_slice(sl(net=-5.0, pct=-0.04), hold(), FLOOR)
ok("a slice below its entry is UNDERWATER", b == ec.UNDERWATER)
ok("and says so as the rule working, not a jam", "rule working" in why)
ok("exactly zero net is NOT profit - the fee round trip is already in it",
   ec.classify_slice(sl(net=0.0, pct=0.0), hold(), FLOOR)[0] == ec.UNDERWATER)

section("[2] a resting order is health, and it is what the 958 rows really were")
b, why = ec.classify_slice(sl(), hold(avail=0.5, locked=9.5), FLOOR)
ok("profitable with coin locked by its own order is PROFITABLE_WORKING",
   b == ec.PROFITABLE_WORKING)
ok("and the reason names the 958 expiry rows", "958" in why)

section("[3] committed elsewhere is not a missed exit")
b, _ = ec.classify_slice(sl(qty=10.0), hold(avail=4.0, locked=0.0), FLOOR)
ok("profitable but not enough unlocked is PROFITABLE_LOCKED",
   b == ec.PROFITABLE_LOCKED)

section("[4] THE FLOOR. Profit is not the bar - the account's floor is.")
b, why = ec.classify_slice(sl(net=0.41, pct=0.0058), hold(), FLOOR)
ok("+0.58% against a 1.00% floor is PROFITABLE_BELOW_MIN, held on purpose",
   b == ec.PROFITABLE_BELOW_MIN)
ok("the reason quotes both numbers rather than saying 'too small'",
   "0.580%" in why and "1.00%" in why)
ok("exactly AT the floor qualifies - the floor is a minimum, not a gap",
   ec.classify_slice(sl(net=1.0, pct=0.01), hold(), FLOOR, branch=OPEN)[0]
   == ec.QUALIFIED_EXIT_MISSING)
# A first pass of this analysis used net>0 as "profitable" and reported THREE
# defects. All three were under the floor. The bar is the engine's, not zero.
ok("net>0 alone does NOT make a defect - the error this test exists for",
   ec.classify_slice(sl(net=0.06, pct=0.0012), hold(), FLOOR)[0]
   != ec.QUALIFIED_EXIT_MISSING)

section("[5] the one real defect shape")
b, why = ec.classify_slice(sl(net=5.0, pct=0.05), hold(avail=10.0, locked=0.0),
                           FLOOR, branch=OPEN)
ok("profitable past the floor, unlocked, no order, A ROUTE OPEN "
   "-> QUALIFIED_EXIT_MISSING",
   b == ec.QUALIFIED_EXIT_MISSING)
ok("and it names itself the only shape a defect can take",
   "only shape" in why)
ok("the module publishes which bucket is the bug", ec.DEFECT_BUCKET == ec.QUALIFIED_EXIT_MISSING)

section("[6] UNKNOWN is a THIRD verdict - never a defect, never a pass")
ok("an unpriceable slice is UNKNOWN_READING",
   ec.classify_slice(sl(net=None), hold(), FLOOR)[0] == ec.UNKNOWN_READING)
ok("an unreadable balance is UNKNOWN_READING, not 'unlocked'",
   ec.classify_slice(sl(), {"available_units": None}, FLOOR)[0] == ec.UNKNOWN_READING)
ok("a MISSING floor is UNKNOWN - this module never grades against a bar "
   "the engine does not use",
   ec.classify_slice(sl(), hold(), None)[0] == ec.UNKNOWN_READING)
ok("a slice with no product id cannot be matched to a balance",
   ec.classify([{"id": 9, "qty": 1, "entry_price": 1, "unrealized_net_usd": 1,
                 "unrealized_net_pct": 0.9}], {}, FLOOR)["counts"][ec.UNKNOWN_READING] == 1)
ok("an unparseable number is None, never 0.0",
   ec._num("not-a-number") is None and ec._num("3.5") == 3.5)

section("[7] THE LIVE BOOK, 2026-10-07 03:10Z - 64 slices, measured")
live_slices, live_hold = [], {
    "AAA": hold(avail=1e9, locked=0.0),     # plenty free
    "BBB": hold(avail=0.0, locked=1e9),     # all behind an order
    "CCC": hold(avail=1e9, locked=0.0),
    "ZEC": hold(avail=0.0, locked=0.0),
}
for i in range(57):      # the underwater majority
    live_slices.append(sl(pid="AAA-USD", qty=1.0, entry=61.4, net=-2.0,
                          pct=-0.04, sid=100 + i))
live_slices.append(sl(pid="BBB-USD", qty=1.0, entry=13.61, net=0.5,
                      pct=0.03, sid=200))                       # working
for n, p, sid in ((0.13, 0.008, 301), (0.41, 0.0058, 302), (0.06, 0.0012, 303)):
    live_slices.append(sl(pid="CCC-USD", qty=1.0, entry=44.84, net=n,
                          pct=p, sid=sid))                      # below floor
for i in range(3):       # the three ZEC slices that cannot be priced
    live_slices.append({"id": 53 + i, "product_id": "ZEC-USD", "qty": 0.126,
                        "entry_price": 1645.6, "unrealized_net_usd": None,
                        "unrealized_net_pct": None})

r = ec.classify(live_slices, live_hold, FLOOR)
ok("64 slices in, 64 classified", r["slices_total"] == 64)
ok("57 underwater", r["counts"][ec.UNDERWATER] == 57)
ok("1 working", r["counts"][ec.PROFITABLE_WORKING] == 1)
ok("3 below the floor", r["counts"][ec.PROFITABLE_BELOW_MIN] == 3)
ok("3 unreadable", r["counts"][ec.UNKNOWN_READING] == 3)
ok("*** ZERO qualified exits with no order ***",
   r["qualified_exit_opportunities_with_no_order"] == 0)
ok("so the verdict is CLEAN and the sell engine is not to be investigated",
   r["exit_engine_verdict"] == "CLEAN" and r["investigate_the_sell_engine"] is False)
ok("the unreadable capital is the ZEC basis, carried separately",
   abs(r["capital_usd"][ec.UNKNOWN_READING] - 622.04) < 1.0)

section("[8] one defect flips the verdict, and nothing else does")
more = list(live_slices) + [sl(pid="CCC-USD", qty=1.0, net=9.0, pct=0.09, sid=999)]
r2 = ec.classify(more, live_hold, FLOOR,
                 branches={s["product_id"]: OPEN for s in more})
ok("a single real defect sets INVESTIGATE",
   r2["qualified_exit_opportunities_with_no_order"] == 1
   and r2["exit_engine_verdict"] == "INVESTIGATE")
drowned = list(live_slices) + [sl(pid="AAA-USD", net=-9.0, pct=-0.4, sid=998)
                               for _ in range(200)]
r3 = ec.classify(drowned, live_hold, FLOOR)
ok("200 MORE underwater slices do NOT flip it - a market is not a bug",
   r3["exit_engine_verdict"] == "CLEAN")

section("[9] it refuses to publish the statistic that caused the error")
# The explanatory key is CALLED not_a_failure_rate, so a bare "rate" scan
# fails on the very thing documenting the absence. Check for a published
# RATIO instead: a numeric key whose name reads as a rate.
_rate_keys = [k for k, v in r.items()
              if k != "not_a_failure_rate"
              and ("rate" in k or "ratio" in k or "pct_of" in k)
              and isinstance(v, (int, float))]
ok("no failure rate or ratio is published as a number", _rate_keys == [])
ok("and it says why that ratio was abandoned",
   "95.8%" in r["not_a_failure_rate"] and "order_rested was zero" in r["not_a_failure_rate"])
ok("the invariant is stated in the payload, not left to the reader",
   "ONLY condition" in r["invariant"])

section("[10] UNKNOWN never reads as healthy silence")
u = ec.unknown_is_not_clean(r)
ok("the unreadable slices are surfaced beside a CLEAN verdict",
   u is not None and u["unreadable_slices"] == 3)
ok("and are named an accounting queue, not an execution one",
   "accounting queue" in u["means"])
ok("with nothing unreadable it returns None rather than an empty alarm",
   ec.unknown_is_not_clean(ec.classify([sl(net=-1.0, pct=-0.1)], {"X": hold()},
                                       FLOOR)) is None)

section("[11] it cannot move money")
# CODE, not prose. The docstring promises the module "cancels none", so a
# bare word scan flags the sentence that makes the promise. Strip the
# docstrings and comments first, then look for call syntax.
import ast
_tree = ast.parse(open("exit_classifier.py").read())
_src_nodoc = "\n".join(
    l.split("#", 1)[0] for l in ast.unparse(
        ast.parse(open("exit_classifier.py").read())).splitlines())
for n in ast.walk(_tree):
    if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) \
            and isinstance(n.value.value, str):
        n.value.value = ""
_code = ast.unparse(_tree)
for bad in ("place_order", "place_maker", "place_market", ".cancel(",
            "cancel_order", "session", "await ", "requests.", "urllib",
            "db.", ".commit(", "open("):
    ok("no %-14s in the module's CODE" % bad, bad not in _code)
ok("and it imports nothing that could reach a venue or a database",
   not any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(_tree)))

print("\nALL PASS" if not FAILS else "\n%d FAILED: " % len(FAILS) + "; ".join(FAILS))
sys.exit(0 if not FAILS else 1)
