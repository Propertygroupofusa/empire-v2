#!/usr/bin/env python3
"""Dashboard trigger == execution trigger. The one invariant worth a gate.

THIS IS THE BUG THAT SHIPPED TWICE, both times pointing at ALGO-USD:

  d7f0bb7  fleet_watchdog._exit_threshold modelled the sell as
           "slice net% >= grid_pct". The real rule compares the PRICE to the
           BRANCH REFERENCE and never looks at the slice's entry. The
           watchdog told the owner a branch was ready to sell when it was
           3.3% short, and he was advised to act on it.

  284228b  LOCKED_PROFIT, a DIFFERENT function in the SAME file, still used
           a flat 1.0% - the parked-sell floor - on branches that were not
           parked. It queued advice to cancel a protective resting order to
           free coin that would not have sold.

One fix did not cover the other, because nothing asserted that the observers
and the executor agree. That is what this file does. Three implementations of
one rule now exist:

    crypto_grid_bot.py:7050        EXECUTION - the only one that moves money
    scripts/fleet_watchdog.py      _exit_threshold()
    scripts/capital_map.py         branch_can_sell()

A reader that disagrees with the executor is worse than no reader: it does
not merely fail to help, it actively recommends the wrong action.

UNITS. grid_pct is a FRACTION (0.03 == 3%), not a percent. A proposed spec
for this test wrote the trigger as reference_price * (1 + grid_pct / 100),
which is the same expression off by 100x and would pass a careless reading.
test_execution_formula_is_still_a_fraction pins it against the real source.

No hypothesis, no pytest, no plugins - this repo has none of them. Plain
random with a fixed seed, run directly.
"""

import os
import random
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "scripts"))

_failed = []


def ok(name, cond, extra=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        _failed.append(name)
        print(f"  FAIL  {name}")
        if extra:
            print(f"        {extra}")


# ---------------------------------------------------------------------------
# The reference model. Written from the executor's source, not from either
# observer, so agreeing with it is evidence and not a tautology.
# ---------------------------------------------------------------------------
PARKED_FLOOR_PCT = 0.010  # GRID_PARKED_MIN_NET_PCT


def reference_can_sell(b):
    """crypto_grid_bot.py:7050 and :7004, as the bot actually evaluates them."""
    slices = b.get("slices") or []
    if not slices:
        return False
    gp, ref, px = b.get("grid_pct"), b.get("reference_price"), b.get("current_price")
    if not gp or not ref or not px:
        return None                      # UNKNOWN is a third verdict
    if px >= ref * (1 + gp):             # _rise_hit - FRACTION, not percent
        return True
    levels = b.get("num_levels") or 0
    adopted_only = all(x.get("adopted") for x in slices)
    if bool(slices) and (len(slices) >= levels or adopted_only):
        return max((x.get("unrealized_net_pct") or 0) for x in slices) >= PARKED_FLOOR_PCT
    return False


def gen_branch(rnd):
    n = rnd.randint(0, 5)
    levels = rnd.choice([0, 1, 3, 3, 6, 7])
    ref = rnd.choice([0.125, 1.5142, 60.19, 1586.44])
    gp = rnd.choice([0.02, 0.025, 0.03, 0.04])
    # Straddle the trigger deliberately - most random prices miss it entirely.
    px = ref * rnd.choice([0.90, 0.99, 1 + gp - 1e-9, 1 + gp, 1 + gp + 1e-9, 1.30])
    return {
        "product_id": "AAA-USD",
        "num_levels": levels,
        "grid_pct": gp,
        "reference_price": ref,
        "current_price": px,
        "allocated_usd": 100.0,
        "slices": [{"adopted": rnd.random() < 0.5,
                    "unrealized_net_pct": rnd.choice(
                        [-0.20, -0.01, 0.0, 0.0099, 0.010, 0.0101, 0.07])}
                   for _ in range(n)],
    }


print("the executor's formula is a FRACTION, and the source still says so")
src = open(os.path.join(HERE, "crypto_grid_bot.py")).read()
ok("crypto_grid_bot.py still computes the rise trigger as (1 + grid_pct)",
   "price >= branch.reference_price * (1 + grid_pct)" in src,
   "the execution rule moved - every observer below is now unverified")
ok("and NOT as (1 + grid_pct / 100), which is the same rule off by 100x",
   "reference_price * (1 + grid_pct / 100)" not in src)
ok("the parked rule is still full-on-rungs OR adopted-only",
   re.search(r"len\(slices\) >= \(branch\.num_levels or 0\)\s*\n\s*or branch_is_adopted_only\(slices\)",
             src) is not None)

print()
print("fleet_watchdog._exit_threshold agrees with the executor")
import fleet_watchdog as fw

rnd = random.Random(20260930)
cases = [gen_branch(rnd) for _ in range(4000)]
mismatch = []
for b in cases:
    want = reference_can_sell(b)
    got, _rule, _gap = fw._exit_threshold(b)
    if want != got:
        mismatch.append((b, want, got))
ok(f"over {len(cases)} generated branches, no disagreement",
   not mismatch,
   f"{len(mismatch)} mismatch(es), first: {mismatch[0] if mismatch else ''}")

print()
print("capital_map.branch_can_sell agrees with the executor")
import capital_map as cm

mismatch2 = [(b, reference_can_sell(b), cm.branch_can_sell(b))
             for b in cases if reference_can_sell(b) != cm.branch_can_sell(b)]
ok(f"over {len(cases)} generated branches, no disagreement",
   not mismatch2,
   f"{len(mismatch2)} mismatch(es), first: {mismatch2[0] if mismatch2 else ''}")

print()
print("trigger_model.can_sell - the canonical reader - agrees with the executor")
import trigger_model as tm

mismatch3 = [(b, reference_can_sell(b), tm.can_sell(b))
             for b in cases if reference_can_sell(b) != tm.can_sell(b)]
ok(f"over {len(cases)} generated branches, no disagreement",
   not mismatch3,
   f"{len(mismatch3)} mismatch(es), first: {mismatch3[0] if mismatch3 else ''}")
ok("and it is the module the /capital-mobility endpoint imports",
   "import trigger_model" in open(os.path.join(HERE, "routers", "trading_dashboard.py")).read()
   or "from trigger_model" in open(os.path.join(HERE, "routers", "trading_dashboard.py")).read())

print()
print("can_buy counts the breaker, not just the parked rule")
ok("a breakered branch with a free rung cannot buy",
   tm.can_buy({"num_levels": 3, "slices": [], "drawdown_breached": True}) is False)
ok("a buys_paused branch with a free rung cannot buy",
   tm.can_buy({"num_levels": 3, "slices": [], "buys_paused": True}) is False)
ok("an ordinary branch with a free rung can",
   tm.can_buy({"num_levels": 3, "slices": []}) is True)

print()
print("mobility never blends the two capabilities")
m = tm.mobility(cases[:200])
ok("can_buy, can_sell and can_do_both are reported separately",
   all(k in m for k in ("can_buy", "can_sell", "can_do_both")))
ok("can_do_both is never larger than either leg",
   m["can_do_both"]["usd"] <= m["can_buy"]["usd"]
   and m["can_do_both"]["usd"] <= m["can_sell"]["usd"],
   str(m["can_do_both"]) + " vs " + str(m["can_buy"]) + " / " + str(m["can_sell"]))
ok("unreadable branches are in none of the three",
   m["sell_unreadable"]["branches"] == sum(1 for b in cases[:200]
                                           if tm.can_sell(b) is None))

print()
print("the mutants the invariant exists to kill")

def _mut(fn_name, b):
    """Would a reader using the WRONG rule disagree here? It must."""
    return fn_name(b)

# 1. The sign flip: (1 - grid_pct) instead of (1 + grid_pct).
def sign_flipped(b):
    sl = b.get("slices") or []
    if not sl:
        return False
    gp, ref, px = b.get("grid_pct"), b.get("reference_price"), b.get("current_price")
    if not gp or not ref or not px:
        return None
    if px >= ref * (1 - gp):
        return True
    levels = b.get("num_levels") or 0
    if len(sl) >= levels or all(x.get("adopted") for x in sl):
        return max((x.get("unrealized_net_pct") or 0) for x in sl) >= PARKED_FLOOR_PCT
    return False

ok("a (1 - grid_pct) sign flip is detected by these cases",
   any(sign_flipped(b) != reference_can_sell(b) for b in cases))

# 2. The percent/fraction slip the proposed spec contained.
def pct_slip(b):
    sl = b.get("slices") or []
    if not sl:
        return False
    gp, ref, px = b.get("grid_pct"), b.get("reference_price"), b.get("current_price")
    if not gp or not ref or not px:
        return None
    if px >= ref * (1 + gp / 100):
        return True
    levels = b.get("num_levels") or 0
    if len(sl) >= levels or all(x.get("adopted") for x in sl):
        return max((x.get("unrealized_net_pct") or 0) for x in sl) >= PARKED_FLOOR_PCT
    return False

ok("a grid_pct/100 units slip is detected by these cases",
   any(pct_slip(b) != reference_can_sell(b) for b in cases))

# 3. THE ORIGINAL BUG: the slice's own gain standing in for the branch rule.
def slice_gain_rule(b):
    sl = b.get("slices") or []
    if not sl:
        return False
    return max((x.get("unrealized_net_pct") or 0) for x in sl) >= (b.get("grid_pct") or 0)

ok("using the SLICE's own gain instead of the branch reference is detected",
   any(slice_gain_rule(b) != reference_can_sell(b) for b in cases))

# 4. THE SECOND BUG: the parked floor applied to branches that are not parked.
def flat_floor_rule(b):
    sl = b.get("slices") or []
    if not sl:
        return False
    return max((x.get("unrealized_net_pct") or 0) for x in sl) >= PARKED_FLOOR_PCT

ok("applying the parked floor to an UNPARKED branch is detected",
   any(flat_floor_rule(b) != reference_can_sell(b) for b in cases))

print()
print("UNKNOWN stays a third verdict and is never folded into False")
u = {"product_id": "AAA-USD", "num_levels": 3, "grid_pct": 0.03,
     "reference_price": None, "current_price": 1.0, "allocated_usd": 10.0,
     "slices": [{"adopted": False, "unrealized_net_pct": 0.5}]}
ok("reference model returns None", reference_can_sell(u) is None)
ok("capital_map returns None", cm.branch_can_sell(u) is None)
ok("watchdog returns None", fw._exit_threshold(u)[0] is None)
ok("and None is not False", reference_can_sell(u) is not False)

print()
if _failed:
    print(f"{len(_failed)} FAILED: " + "; ".join(_failed))
else:
    print("Dashboard trigger == execution trigger, on every generated branch.")
sys.exit(1 if _failed else 0)
