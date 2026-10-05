"""A starved fills sample must blind the checks, not crash one of them.

THE BUG THIS PINS

Live 2026-10-05, /grid-status/invariants reported:

    fee_rate_agreement        UNKNOWN - only None spot fills carried a
                                        maker/taker label
    spacing_evidence_current  UNKNOWN - same cause
    maker_only_holds          UNKNOWN - could not be checked:
                              UnboundLocalError: cannot access local
                              variable '_maker_only' where it is not
                              associated with a value

_maker_only was assigned only inside the `enough_to_conclude` branch, so
a starved sample took the else branch and left it unbound. One missing
label blinded three checks, and the third reported a Python error
instead of the same honest "not enough labelled fills" the other two
managed.

Two separate faults, both fixed here:
  * the mode is read BEFORE the request, because whether maker-only is
    armed is a DB flag the fills sample knows nothing about - it is
    answerable precisely when no fill carries a label
  * "only None spot fills" is not a count. A missing field and a thin
    sample are different faults and no longer share a sentence.

Run: python3 test_invariant_blind_sample.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "routers", "trading_dashboard.py")).read()

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


# Isolate the invariant block so a match elsewhere cannot stand in for one here.
start = SRC.index("measured_leg = None\n        blind_because = None")
block = SRC[start:start + 5200]
CODE = "\n".join(l for l in block.splitlines() if not l.lstrip().startswith("#"))

# --- the binding ----------------------------------------------------------
assign = CODE.index("_maker_only = await g.is_maker_only_active()")
use_guard = CODE.index("if _maker_only and fills.get(")
use_check = CODE.index("if _maker_only:")
ok("the mode is read before it is used as a guard", assign < use_guard)
ok("and before the maker_only_holds check", assign < use_check)

enough = CODE.index('if fills.get("enough_to_conclude")')
ok("the mode is bound OUTSIDE the enough_to_conclude branch", assign < enough)

ok("fills is bound before the request too", CODE.index("fills = {}") < CODE.index("import aiohttp"))
ok("it is bound to a mapping, so .get() is safe on the starved path",
   re.search(r"fills\s*=\s*\{\}", CODE))

ok("the mode read still fails closed to False",
   re.search(r"except Exception:\s*\n\s*_maker_only = False", CODE))
ok("the mode is read exactly once", CODE.count("_maker_only = await") == 1)

# --- the wording ----------------------------------------------------------
ok("a missing count no longer renders as a count",
   "isinstance(_classified, int)" in CODE)
ok("and says the sample size is unknown instead",
   "the sample size is unknown" in block)
ok("a real count still reads as one",
   "carried a maker/taker label" in block)
ok("one fill is not '1 fills'", "'' if _classified == 1 else 's'" in block)

# --- what must NOT have changed -------------------------------------------
ok("a starved sample still blinds the two fee checks, not passes them",
   "A starved sample is UNKNOWN, not a pass" in block)
ok("an UNKNOWN still carries its cause", 'r["blind_because"] = blind_because' in CODE)
ok("maker_only_holds still only runs when the mode is on",
   CODE.index("if _maker_only:") < CODE.index("inv.maker_only_holds("))
ok("its own exception handler is still there",
   '"name": "maker_only_holds", "status": inv.UNKNOWN' in CODE)

failed = [l for l, c in checks if not c]
for l, c in checks:
    print(f"  {'PASS' if c else 'FAIL'}  {l}")
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)
