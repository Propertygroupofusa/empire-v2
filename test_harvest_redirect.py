"""Harvested profit goes to a branch that can use it, or it stays cash.

THE OWNER ASKED FOR THIS, 2026-10-05: "every branch once it fills, I want
the money to go into the weakest branch and help it build up... Do not make
any more branches."

What this pins, in order of how much money each one protects:

  1. NO DOLLAR IS EVER LEFT NOWHERE. The withdrawal happens first, so
     between it and the deposit the money is in flight. If the deposit
     fails it goes BACK, and the baseline does not advance, so the next
     run retries it. A harvest that deletes capital would be worse than
     one that never ran.
  2. IT NEVER FEEDS ITS OWN SOURCE. crypto_family_tree_bot's own comment
     records what happened without that exclusion: "'always reinforce the
     weakest' was a closed loop feeding itself."
  3. IT CREATES NO BRANCH, ever, for any reason.
  4. OFF unless GRID_HARVEST_REDIRECT is true, and when off the harvest is
     byte-for-byte what it was.

Run: python3 test_harvest_redirect.py
"""
import asyncio
import os
import sys

import harvest_redirect as hr

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


def B(name, pid, alloc, levels, slices, px, ref, gp):
    return {"bot_name": name, "product_id": pid, "allocated_usd": alloc,
            "num_levels": levels, "slices": [{"entry_price": px}] * slices,
            "current_price": px, "reference_price": ref, "grid_pct": gp}


# A fleet shaped like the real one on 2026-10-05: one branch already past
# its buy line and flat, one near, one full, one far.
QNT  = B("crypto_grid_14", "QNT-USD",   142.43, 3, 0, 253.48, 266.62 / (1 - 0.03), 0.03)
BTC  = B("crypto_grid_1",  "BTC-USD",    54.21, 3, 1, 85379.0, 84912.0 / (1 - 0.0133), 0.0133)
FULL = B("crypto_grid_9",  "SHIB-USD",  347.88, 2, 2, 5.89e-06, 5.9e-06, 0.015)
FAR  = B("crypto_grid_6",  "XRP-USD",  2049.55, 10, 7, 2.50, 2.52, 0.012)
FLEET = sum(b["allocated_usd"] for b in (QNT, BTC, FULL, FAR))


# --- the ranking -----------------------------------------------------------
t, why = hr.pick_target([QNT, BTC, FULL, FAR], exclude_bot_name="crypto_grid_3",
                        amount_usd=1.63, fleet_allocated_usd=FLEET)
ok("the branch already past its buy line wins", t == "crypto_grid_14")
ok("and the reason says why", "already past its buy line" in why["reason"])
ok("a FULL branch is never chosen",
   all(r["bot_name"] != "crypto_grid_9" for r in why.get("runners_up") or []))
ok("the full branch is rejected for having no empty rung",
   any(r["bot_name"] == "crypto_grid_9" and "no empty rung" in r["why"]
       for r in why["rejected"]))

# --- rule 1: never its own source -----------------------------------------
t2, why2 = hr.pick_target([QNT, BTC], exclude_bot_name="crypto_grid_14",
                          amount_usd=1.63, fleet_allocated_usd=FLEET)
ok("the source branch is excluded even when it would rank first",
   t2 == "crypto_grid_1")
ok("and the rejection names the closed loop",
   any("closed loop" in r["why"] for r in why2["rejected"]))

# --- the concentration ceiling --------------------------------------------
BIG = B("crypto_grid_6", "XRP-USD", 0.199 * 1000.0, 10, 1, 2.50, 2.52, 0.012)
t3, why3 = hr.pick_target([BIG], exclude_bot_name=None, amount_usd=5.0,
                          fleet_allocated_usd=1000.0)
ok("a deposit that would breach the 20% ceiling is refused", t3 is None)
ok("and says so in percent",
   any("ceiling" in r["why"] for r in why3["rejected"]))
SMALL = B("crypto_grid_6", "XRP-USD", 0.10 * 1000.0, 10, 1, 2.50, 2.52, 0.012)
t4, _ = hr.pick_target([SMALL], amount_usd=5.0, fleet_allocated_usd=1000.0)
ok("one comfortably under the ceiling is allowed", t4 == "crypto_grid_6")

# --- no target is a normal outcome, not a crash ---------------------------
t5, why5 = hr.pick_target([FULL], amount_usd=1.0, fleet_allocated_usd=FLEET)
ok("nothing eligible returns None, not an exception", t5 is None)
ok("and says the money stays as cash", "stays as cash" in why5["reason"])
ok("an empty fleet is handled", hr.pick_target([], amount_usd=1.0)[0] is None)

# --- unreadable is not zero ------------------------------------------------
BAD = dict(QNT); BAD["allocated_usd"] = None
t6, why6 = hr.pick_target([BAD], amount_usd=1.0, fleet_allocated_usd=FLEET)
ok("an unreadable allocation is refused, not treated as $0", t6 is None)
ok("and the reason says a gap is not a zero",
   any("gap is not a zero" in r["why"] for r in why6["rejected"]))
NOPX = dict(BTC); NOPX["current_price"] = None
ok("an unreadable price is refused too",
   hr.pick_target([NOPX], amount_usd=1.0, fleet_allocated_usd=FLEET)[0] is None)

# --- the flag --------------------------------------------------------------
for v, want in (("true", True), ("TRUE", True), ("false", False), ("", False)):
    os.environ[hr.ENV_FLAG] = v
    ok(f"flag {v!r} -> enabled={want}", hr.enabled() is want)
os.environ.pop(hr.ENV_FLAG, None)
ok("unset means OFF", hr.enabled() is False)


# --- the wiring: no dollar is ever left nowhere ---------------------------
import profit_harvest as ph


class FakeGrid:
    """Records every claim movement so the test can check it balances."""

    def __init__(self, deposit_fails=False, rollback_fails=False):
        self.moves = []
        self.deposit_fails = deposit_fails
        self.rollback_fails = rollback_fails
        self.baselines = []

    async def get_grid_status(self):
        return {"branches": [QNT, BTC, FULL, FAR], "total_allocated_usd": FLEET}

    async def withdraw_from_grid_branch(self, bot_name, amount, **kw):
        self.moves.append((bot_name, -round(amount, 2)))

    async def add_cash_to_grid_branch(self, bot_name, amount, *, caller=None):
        if caller == "profit_harvest.redirect" and self.deposit_fails:
            raise RuntimeError("venue said no")
        if caller == "profit_harvest.redirect_rollback" and self.rollback_fails:
            raise RuntimeError("rollback also failed")
        self.moves.append((bot_name, round(amount, 2)))

    def get_session_factory(self):
        raise AssertionError("not used in this test")

    async def _log_activity_safe(self, *a, **kw):
        pass


ROW = {"bot_name": "crypto_grid_3", "product_id": "PRIME-USD",
       "allocated_usd": 38.21, "harvest_usd": 1.63, "realised_total": 4.66}


def run_harvest(grid, rows=(ROW,)):
    async def _go():
        async def fake_plan(_g):
            return {"branches": list(rows), "total_harvest_usd": 1.63,
                    "ready": True, "unwatched_branches": 0, "loop_has_run": True}
        real_plan, real_adv = ph.plan, ph._advance_baseline
        async def fake_adv(_f, name, total):
            grid.baselines.append((name, total))
        ph.plan, ph._advance_baseline = fake_plan, fake_adv
        try:
            return await ph.run(grid, dry_run=False)
        finally:
            ph.plan, ph._advance_baseline = real_plan, real_adv
    return asyncio.run(_go())


os.environ[hr.ENV_FLAG] = "true"
g = FakeGrid()
res = run_harvest(g)
ok("the money left the source", ("crypto_grid_3", -1.63) in g.moves)
ok("and landed in the chosen branch", ("crypto_grid_14", 1.63) in g.moves)
ok("the claim movements sum to zero - nothing minted, nothing deleted",
   abs(sum(a for _, a in g.moves)) < 0.005)
ok("the payload reports the move", res.get("redirected_usd") == 1.63)
ok("and names the destination",
   (res.get("redirected") or [{}])[0].get("to_bot_name") == "crypto_grid_14")
ok("the baseline advanced once the money was placed", len(g.baselines) == 1)

# a failed deposit puts it back
g2 = FakeGrid(deposit_fails=True)
res2 = run_harvest(g2)
ok("a failed deposit returns the money to its own branch",
   ("crypto_grid_3", 1.63) in g2.moves)
ok("and the books still balance after a rollback",
   abs(sum(a for _, a in g2.moves)) < 0.005)
ok("the baseline does NOT advance, so the next run retries it",
   len(g2.baselines) == 0)
ok("the failure is reported, not swallowed", res2.get("failed"))
ok("and the branch is not counted as harvested", res2.get("rows_written") == 0)

# the catastrophic case is loud rather than silent
g3 = FakeGrid(deposit_fails=True, rollback_fails=True)
res3 = run_harvest(g3)
ok("a failed rollback is reported as money landing nowhere",
   "COULD NOT BE RETURNED" in str(res3.get("failed")))
ok("and it still does not advance the baseline", len(g3.baselines) == 0)

# --- OFF is byte-for-byte the old behaviour -------------------------------
os.environ[hr.ENV_FLAG] = "false"
g4 = FakeGrid()
res4 = run_harvest(g4)
ok("with the flag off the money only leaves, as before",
   g4.moves == [("crypto_grid_3", -1.63)])
ok("nothing is redirected", res4.get("redirected_usd") == 0.0)
ok("and the payload says the flag is off",
   hr.ENV_FLAG in (res4.get("redirect_is") or ""))
ok("the baseline still advances when the money becomes cash",
   len(g4.baselines) == 1)
os.environ.pop(hr.ENV_FLAG, None)

# --- it must never create a branch ----------------------------------------
SRC = open("harvest_redirect.py").read()
CODE = "\n".join(l for l in SRC.splitlines() if not l.lstrip().startswith("#"))
for forbidden in ("create_grid_branch", "place_market_buy", "place_order",
                  "close_all_grid_slices", "withdraw_from_grid_branch"):
    ok(f"harvest_redirect never calls {forbidden}", forbidden not in CODE)
PH = "\n".join(l for l in open("profit_harvest.py").read().splitlines()
               if not l.lstrip().startswith("#"))
ok("the harvest still creates no branch", "create_grid_branch" not in PH)
ok("and still places no order", "place_order" not in PH and "place_market_buy" not in PH)

# --- the arming state must be visible WITHOUT running anything ----------
# Shipped the same evening both flags were switched on, because nothing
# reported whether they were. run() carries redirect_active but returns
# before it on dry_run, and the preview endpoint only ever uses dry_run.
import asyncio as _aio


class PlanGrid:
    """Just enough for plan() to reach its return."""

    def __init__(self):
        self.calls = []

    async def get_grid_status(self):
        return {"branches": [], "total_allocated_usd": 0.0}

    def get_session_factory(self):
        return None


def read_plan():
    async def _go():
        real = ph.realised_by_branch
        async def fake(_sf):
            return {}
        ph.realised_by_branch = fake
        try:
            return await ph.plan(PlanGrid(), create=False)
        finally:
            ph.realised_by_branch = real
    return _aio.run(_go())


import coin_quality as _cq
for flag, key in ((hr.ENV_FLAG, "redirect_active"), (_cq.ENV_FLAG, "coin_quality_active")):
    os.environ.pop(hr.ENV_FLAG, None); os.environ.pop(_cq.ENV_FLAG, None)
    off = read_plan()
    ok(f"{key} reads False on the read-only path when the flag is unset",
       off.get(key) is False)
    os.environ[flag] = "true"
    on = read_plan()
    ok(f"{key} reads True once the flag is set", on.get(key) is True)
    ok(f"and {key} is present at all, not missing", key in on)
    os.environ.pop(flag, None)

p_ = read_plan()
ok("the cap is reported as a percent", isinstance(p_.get("max_coin_share_pct"), float))
ok("and a plain sentence says what is armed", "OFF" in (p_.get("armed_is") or ""))
ok("which names both flags",
   hr.ENV_FLAG in p_["armed_is"] and _cq.ENV_FLAG in p_["armed_is"])
ok("and says reading it moved nothing", "moved no dollar" in p_["armed_is"])

failed_checks = [l for l, c in checks if not c]
for l, c in checks:
    print(f"  {'PASS' if c else 'FAIL'}  {l}")
print(f"\n{len(checks) - len(failed_checks)}/{len(checks)} passed")
sys.exit(1 if failed_checks else 0)
