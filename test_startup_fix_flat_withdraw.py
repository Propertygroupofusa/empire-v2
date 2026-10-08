"""Free a flat branch's stranded claim - and never, ever delete the branch.

Run as written: python3 test_startup_fix_flat_withdraw.py

WHAT IS STRANDED, measured 2026-10-08 20:42Z. crypto_grid_21 (ZEC-USD)
holds $1,767.76 of allocation against ZERO coin, ZERO open slices and zero
completed closes in 39 days. Its trading balance is a confirmed zero, it
refused 199 sell attempts in three hours, and it is 26% of the grid's whole
budget. Nothing about it is a ranking judgement: the branch holds nothing.

WHY IT WAS NOT ALREADY FREED. The right-size refuses a flat branch by
design - branch_rightsize.plan names withdraw_from_grid_branch as "the
tested path" instead. And withdraw_from_grid_branch had exactly one caller
behind the write-guarded POST, i.e. the dashboard Withdraw button, on a
page whose write buttons do not reach the server from the owner's tab.
Tested, correct, and unreachable. Same shape as the right-size before it
got an in-process path, so it gets the same treatment.

THE ONE INVARIANT THAT OUTRANKS THE FEATURE - SECTION [2].

withdraw_from_grid_branch DELETES the branch if the withdrawal drains it
below a cent, and its own log says why that is dangerous: "Its coin is now
owned by no branch: no grid rule will sell it, no breaker watches it, and
any resting order against it stands unmanaged." That is not theoretical -
FLOKI-USD's branch was deleted 2026-10-04, its ~$43 of coin was orphaned,
and on 10-07 a single sell order filled in 8 pieces with no buy side and
nothing reported it for three days.

And the account owner's instruction is flat: "Don't delete any losing
branches. Not none of them." ZEC is a losing branch.

So this step withdraws DOWN TO the keep-alive floor and stops. It never
passes allow_delete, never computes an amount that would trip the floor,
and leaves the branch alive and watched. $1,752.76 freed, $15.00 left
standing. Every test below that mentions deletion is load-bearing.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, ".")
import startup_fix as sfx


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# The live 2026-10-08 20:42Z fleet, as /grid-status reports it.
BRANCHES = [
    # flat, unlocked, far above the floor - the one this exists for
    {"bot_name": "crypto_grid_21", "product_id": "ZEC-USD",
     "allocated_usd": 1767.76, "open_slices": 0, "locked": False, "active": True},
    # flat but LOCKED - withdraw_from_grid_branch refuses a locked branch
    {"bot_name": "crypto_grid_98", "product_id": "LOCKED-USD",
     "allocated_usd": 500.00, "open_slices": 0, "locked": True, "active": True},
    # flat but already AT the floor - nothing to take
    {"bot_name": "crypto_grid_97", "product_id": "TINY-USD",
     "allocated_usd": 15.00, "open_slices": 0, "locked": False, "active": True},
    # flat and BELOW the floor - taking anything would delete it
    {"bot_name": "crypto_grid_96", "product_id": "UNDER-USD",
     "allocated_usd": 9.40, "open_slices": 0, "locked": False, "active": True},
    # NOT flat - real coin is bought against this allocation
    {"bot_name": "crypto_grid_6", "product_id": "XRP-USD",
     "allocated_usd": 1370.04, "open_slices": 13, "locked": False, "active": True},
]

FLOOR = 15.0


class _Grid:
    """Stands in for crypto_grid_bot. Records every withdraw call verbatim."""

    GRID_KEEP_BRANCH_ALIVE_USD = FLOOR

    def __init__(self, branches=None, raise_on=()):
        self.branches = [dict(b) for b in (branches if branches is not None else BRANCHES)]
        self.calls = []
        self.raise_on = set(raise_on)

    async def get_grid_status(self):
        return {"branches": [dict(b) for b in self.branches]}

    async def withdraw_from_grid_branch(self, bot_name, amount, **kw):
        self.calls.append({"bot_name": bot_name, "amount": amount, "kw": dict(kw)})
        if bot_name in self.raise_on:
            raise ValueError("branch vanished")
        return {"ok": True, "bot_name": bot_name, "withdrawn": amount}

    async def _log_activity_safe(self, *_a, **_k):
        return None


class ArmedCase(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get(sfx.FLAT_WITHDRAW_ENV)
        os.environ[sfx.FLAT_WITHDRAW_ENV] = "crypto_grid_21"

    def tearDown(self):
        if self._prev is None:
            os.environ.pop(sfx.FLAT_WITHDRAW_ENV, None)
        else:
            os.environ[sfx.FLAT_WITHDRAW_ENV] = self._prev


class TestItFreesTheStrandedClaim(ArmedCase):

    def test_zec_is_withdrawn_down_to_the_floor(self):
        g = _Grid()
        out = run(sfx.apply_flat_withdraw(g))
        self.assertEqual(out["status"], "APPLIED")
        zec = [c for c in g.calls if c["bot_name"] == "crypto_grid_21"]
        self.assertEqual(len(zec), 1, "ZEC was not withdrawn exactly once")
        self.assertAlmostEqual(zec[0]["amount"], 1767.76 - FLOOR, places=2)
        self.assertAlmostEqual(out["freed_usd"], 1752.76, places=2)

    def test_it_reports_what_it_freed(self):
        g = _Grid()
        out = run(sfx.apply_flat_withdraw(g))
        self.assertEqual(out["rows_written"], 1)
        self.assertTrue(out["settled"])


class TestItNeverDeletesABranch(ArmedCase):
    """THE LESSON OF FLOKI-USD, AND A DIRECT INSTRUCTION FROM THE OWNER.

    A deleted branch orphans its coin: no grid rule sells it, no breaker
    watches it, and any resting order against it stands unmanaged. The
    owner's words: "Don't delete any losing branches. Not none of them."
    """

    def test_allow_delete_is_never_passed(self):
        g = _Grid()
        run(sfx.apply_flat_withdraw(g))
        for c in g.calls:
            self.assertNotIn("allow_delete", c["kw"],
                             f"{c['bot_name']} was offered allow_delete")

    def test_the_amount_never_trips_the_floor(self):
        g = _Grid()
        run(sfx.apply_flat_withdraw(g))
        by = {b["bot_name"]: b["allocated_usd"] for b in BRANCHES}
        for c in g.calls:
            left = round(by[c["bot_name"]] - c["amount"], 2)
            self.assertGreaterEqual(
                left, FLOOR - 0.005,
                f"{c['bot_name']} would be left ${left:,.2f}, under the floor")

    def test_a_branch_already_under_the_floor_is_left_alone(self):
        g = _Grid()
        run(sfx.apply_flat_withdraw(g))
        self.assertNotIn("crypto_grid_96", [c["bot_name"] for c in g.calls],
                         "a branch below the floor can only be emptied by deleting it")

    def test_a_branch_exactly_at_the_floor_is_left_alone(self):
        g = _Grid()
        run(sfx.apply_flat_withdraw(g))
        self.assertNotIn("crypto_grid_97", [c["bot_name"] for c in g.calls])

    def test_the_source_never_mentions_allow_delete(self):
        """Belt and braces: a future edit cannot quietly add it."""
        src = open(os.path.join(os.path.dirname(__file__) or ".", "startup_fix.py")).read()
        i = src.index("async def apply_flat_withdraw")
        j = src.index("\nasync def ", i + 10) if "\nasync def " in src[i + 10:] else len(src)
        body = src[i:j]
        self.assertNotIn("allow_delete=True", body)


class TestItRefusesRatherThanGuesses(ArmedCase):

    def test_a_branch_with_an_open_slice_is_never_touched(self):
        g = _Grid()
        run(sfx.apply_flat_withdraw(g))
        self.assertNotIn("crypto_grid_6", [c["bot_name"] for c in g.calls],
                         "an open slice is real coin bought against that allocation")

    def test_a_locked_branch_is_never_touched(self):
        g = _Grid()
        run(sfx.apply_flat_withdraw(g))
        self.assertNotIn("crypto_grid_98", [c["bot_name"] for c in g.calls])

    def test_over_the_ceiling_writes_nothing(self):
        big = [dict(BRANCHES[0], allocated_usd=sfx.MAX_FLAT_WITHDRAW_USD + 100.0)]
        g = _Grid(branches=big)
        out = run(sfx.apply_flat_withdraw(g))
        self.assertEqual(out["status"], "REFUSED_OVER_CEILING")
        self.assertEqual(g.calls, [], "it wrote despite refusing the plan")
        self.assertFalse(out["settled"])

    def test_the_ceiling_is_a_real_bound(self):
        self.assertGreater(sfx.MAX_FLAT_WITHDRAW_USD, 1752.76)
        self.assertLessEqual(sfx.MAX_FLAT_WITHDRAW_USD, 10000.0)

    def test_nothing_eligible_settles_and_writes_nothing(self):
        g = _Grid(branches=[BRANCHES[4]])          # only the non-flat one
        out = run(sfx.apply_flat_withdraw(g))
        self.assertEqual(out["status"], "NOTHING_TO_DO")
        self.assertTrue(out["settled"], "nothing to do must settle or the ticket churns")
        self.assertEqual(g.calls, [])

    def test_the_target_raising_is_recorded_and_not_swallowed(self):
        """Replaced the old "does not stop the others" case: with one named
        branch there are no others. What matters now is that a raise is
        reported by name, writes nothing, and leaves the ticket unspent so
        the next boot retries."""
        g = _Grid(raise_on=("crypto_grid_21",))
        out = run(sfx.apply_flat_withdraw(g))
        self.assertEqual(out["rows_written"], 0)
        self.assertEqual(out["freed_usd"], 0.0)
        self.assertFalse(out["settled"], "a raise must leave the ticket retryable")
        self.assertIn("crypto_grid_21", [x["bot_name"] for x in out["not_applied"]])
        self.assertIn("branch vanished", out["not_applied"][0]["error"])

    def test_every_branch_failing_is_NOT_settled(self):
        g = _Grid(raise_on=("crypto_grid_21",))
        out = run(sfx.apply_flat_withdraw(g))
        self.assertEqual(out["status"], "NOTHING_WRITTEN")
        self.assertFalse(out["settled"])


class TestItTouchesOnlyTheBranchItIsGiven(ArmedCase):
    """WHAT THE DRY RUN CAUGHT, 2026-10-08. The first version took a
    boolean and swept every flat branch above the floor. Replayed against
    the live fleet it would have freed ZEC's $1,752.76 - right - and also
    stripped ONDO-USD from $86.02 to $15.00. ONDO is a working branch with
    9 closes that was flat only because it had sold two hours earlier.

    Nothing in the payload separates those two: out_of_reach calls ONDO,
    TIA and ZEC all "confirmed_zero", because a flat branch holds no coin
    by definition. So the owner names the branch and this acts on that row
    alone."""

    def test_a_working_branch_that_is_merely_flat_today_is_untouched(self):
        fleet = [
            {"bot_name": "crypto_grid_21", "product_id": "ZEC-USD",
             "allocated_usd": 1767.76, "open_slices": 0, "locked": False},
            {"bot_name": "crypto_grid_4", "product_id": "ONDO-USD",
             "allocated_usd": 86.02, "open_slices": 0, "locked": False},
            {"bot_name": "crypto_grid_7", "product_id": "TIA-USD",
             "allocated_usd": 14.94, "open_slices": 0, "locked": False},
        ]
        g = _Grid(branches=fleet)
        out = run(sfx.apply_flat_withdraw(g))
        self.assertEqual([c["bot_name"] for c in g.calls], ["crypto_grid_21"])
        self.assertAlmostEqual(out["freed_usd"], 1752.76, places=2)
        self.assertEqual(out["target"], "crypto_grid_21")

    def test_a_name_that_does_not_exist_writes_nothing_and_settles(self):
        os.environ[sfx.FLAT_WITHDRAW_ENV] = "crypto_grid_999"
        g = _Grid()
        out = run(sfx.apply_flat_withdraw(g))
        self.assertEqual(out["status"], "NOTHING_TO_DO")
        self.assertEqual(g.calls, [])
        self.assertTrue(out["settled"])

    def test_naming_a_branch_with_open_slices_writes_nothing(self):
        os.environ[sfx.FLAT_WITHDRAW_ENV] = "crypto_grid_6"   # XRP, 13 slices
        g = _Grid()
        out = run(sfx.apply_flat_withdraw(g))
        self.assertEqual(out["status"], "NOTHING_TO_DO")
        self.assertEqual(g.calls, [])


class TestTheFloorComesFromTheEngine(ArmedCase):
    """A step that carries its own copy of a money constant can disagree
    with the module it is calling. GRID_KEEP_BRANCH_ALIVE_USD is
    MIN_TRADE_USD * 3 and has moved before."""

    def test_the_floor_is_read_from_the_grid_module(self):
        g = _Grid()
        g.GRID_KEEP_BRANCH_ALIVE_USD = 40.0
        run(sfx.apply_flat_withdraw(g))
        zec = [c for c in g.calls if c["bot_name"] == "crypto_grid_21"][0]
        self.assertAlmostEqual(zec["amount"], 1767.76 - 40.0, places=2,
                               msg="the floor was hardcoded, not read")

    def test_an_unreadable_floor_refuses_rather_than_assuming(self):
        # Shadow the class attribute on the instance: getattr then returns
        # None, which is what a grid module missing the constant looks like.
        g = _Grid()
        g.GRID_KEEP_BRANCH_ALIVE_USD = None
        out = run(sfx.apply_flat_withdraw(g))
        self.assertEqual(out["status"], "NO_FLOOR")
        self.assertEqual(g.calls, [])
        self.assertFalse(out["settled"])

    def test_a_zero_floor_refuses_too(self):
        """A floor of 0 would mean 'withdraw everything', which is a delete
        wearing a withdrawal's clothes."""
        for bad in (0, 0.0, -5.0):
            g = _Grid()
            g.GRID_KEEP_BRANCH_ALIVE_USD = bad
            out = run(sfx.apply_flat_withdraw(g))
            self.assertEqual(out["status"], "NO_FLOOR", repr(bad))
            self.assertEqual(g.calls, [], repr(bad))


class TestASecondMoneyOperationNeedsItsOwnConsent(unittest.TestCase):
    """THE LESSON OF 2026-10-08: a ticket armed for the reconcile carried a
    stale level change along with it and lowered LINK from 10 rungs to 6,
    unasked. This is the third money step on that ticket and it is armed
    on its own, like the right-size before it."""

    def setUp(self):
        self._prev = os.environ.pop(sfx.FLAT_WITHDRAW_ENV, None)

    def tearDown(self):
        if self._prev is not None:
            os.environ[sfx.FLAT_WITHDRAW_ENV] = self._prev

    def test_unarmed_it_writes_nothing_and_settles(self):
        g = _Grid()
        out = run(sfx.apply_flat_withdraw(g))
        self.assertEqual(out["status"], "NOT_ARMED")
        self.assertEqual(out["rows_written"], 0)
        self.assertEqual(out["freed_usd"], 0.0)
        self.assertTrue(out["settled"], "an unarmed step that does not settle "
                                        "leaves the ticket retrying forever")
        self.assertEqual(g.calls, [], "it wrote while unarmed")

    def test_it_does_not_even_read_the_fleet_while_unarmed(self):
        g = _Grid()
        run(sfx.apply_flat_withdraw(g))
        self.assertEqual(g.calls, [])

    def test_its_variable_is_its_own(self):
        self.assertNotEqual(sfx.FLAT_WITHDRAW_ENV, sfx.RIGHTSIZE_ENV)
        self.assertNotEqual(sfx.FLAT_WITHDRAW_ENV, sfx.TICKET_ENV)

    def test_a_bare_yes_is_NOT_a_target(self):
        """It used to arm a fleet-wide sweep. A stale "true" left in Railway
        must refuse, never quietly mean something new."""
        for off in ("", "0", "false", "no", "off", "1", "true", "TRUE", "yes", "on"):
            os.environ[sfx.FLAT_WITHDRAW_ENV] = off
            self.assertIsNone(sfx.flat_withdraw_target(), repr(off))

    def test_a_branch_name_is_the_target(self):
        for raw, want in (("crypto_grid_21", "crypto_grid_21"),
                          ("  crypto_grid_21  ", "crypto_grid_21"),
                          ('"crypto_grid_21"', "crypto_grid_21"),
                          ("'crypto_grid_21'", "crypto_grid_21")):
            os.environ[sfx.FLAT_WITHDRAW_ENV] = raw
            self.assertEqual(sfx.flat_withdraw_target(), want, repr(raw))


class TestItIsWiredIntoTheBoot(unittest.TestCase):

    def test_it_runs_after_the_rightsize(self):
        """The right-size can give a parked branch a free rung and change
        what 'flat' means for the next step; planning this first would
        measure a fleet that is about to move."""
        import ast
        src = open(os.path.join(os.path.dirname(__file__) or ".", "startup_fix.py")).read()
        tree = ast.parse(src)
        body = next(ast.get_source_segment(src, n) for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == "run_at_boot")
        self.assertLess(body.index("apply_rightsize(grid"),
                        body.index("apply_flat_withdraw(grid"))

    def test_it_is_counted_in_the_total_and_the_gates(self):
        src = open(os.path.join(os.path.dirname(__file__) or ".", "startup_fix.py")).read()
        self.assertIn('"flat_withdraw"', src)
        self.assertIn('out["rows_written_total"] = lv + rc + rs + fw', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
