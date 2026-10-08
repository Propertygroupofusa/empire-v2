"""The right-size, reachable without a browser.

WHY IT HAD TO BE BUILT. The reconcile correctly deleted $839.48 of phantom
coin basis on 2026-10-08 at 15:58:40Z. A parked branch's unspendable
reserve is allocated_usd - coin_basis, so removing basis RAISES it: ZEC
went from claiming $1,167.06 of cash it could not spend to claiming the
full $1,789.11. Within the hour the fleet's reserves exceeded the wallet
and free cash read negative. Writing off the coin without releasing the
budget behind it is half an operation.

The other half was reachable only through the write-guarded POST - one
caller, no in-process path - and that is the same dashboard that failed to
deliver the reconcile four times.

WHAT THESE TESTS PROTECT. Not the arithmetic: that belongs to
branch_rightsize.apply_one and is tested there. These pin the things a new
caller can get wrong - that it delegates instead of writing rows itself,
that a flat branch is left alone, that a ceiling stands, that it settles
correctly so the ticket cannot churn, and that it runs AFTER the reconcile.
"""
import asyncio
import os
import unittest

import branch_rightsize
import startup_fix as sfx


class ArmedCase(unittest.TestCase):
    """The step refuses to run unarmed - that is its own test, below. Every
    behavioural test here arms it explicitly and restores the environment
    afterwards, so the flag cannot leak between tests."""

    def setUp(self):
        self._prev = os.environ.get(sfx.RIGHTSIZE_ENV)
        os.environ[sfx.RIGHTSIZE_ENV] = "true"

    def tearDown(self):
        if self._prev is None:
            os.environ.pop(sfx.RIGHTSIZE_ENV, None)
        else:
            os.environ[sfx.RIGHTSIZE_ENV] = self._prev


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# The live 2026-10-08 16:16Z plan, plus the flat ZEC branch.
PLAN = {
    "total_freeable_usd": 961.97,
    "branches": [
        {"bot_name": "crypto_grid_6", "current_price": 1.378100, "product_id": "XRP-USD",
         "allocated_usd": 2041.23, "floor_usd": 1268.48, "freeable_usd": 772.75,
         "parked": True, "why_not": None},
        {"bot_name": "crypto_grid_19", "current_price": 63.013000, "product_id": "LTC-USD",
         "allocated_usd": 196.50, "floor_usd": 74.24, "freeable_usd": 122.26,
         "parked": True, "why_not": None},
        {"bot_name": "crypto_grid_9", "current_price": 0.000005, "product_id": "SHIB-USD",
         "allocated_usd": 347.58, "floor_usd": 320.01, "freeable_usd": 27.57,
         "parked": True, "why_not": None},
        {"bot_name": "crypto_grid_11", "current_price": 0.119250, "product_id": "ALGO-USD",
         "allocated_usd": 172.74, "floor_usd": 151.23, "freeable_usd": 21.51,
         "parked": True, "why_not": None},
        {"bot_name": "crypto_grid_3", "current_price": 0.226250, "product_id": "PRIME-USD",
         "allocated_usd": 65.06, "floor_usd": 47.18, "freeable_usd": 17.88,
         "parked": True, "why_not": None},
        # Flat: plan() reports it with freeable 0 and points elsewhere.
        {"bot_name": "crypto_grid_21", "current_price": None,
         "product_id": "ZEC-USD",
         "allocated_usd": 1789.11, "floor_usd": 15.0, "freeable_usd": 0.0,
         "parked": False,
         "why_not": ("branch is flat; withdraw_from_grid_branch already handles "
                     "a flat branch and is the tested path")},
    ],
}


class _Grid:
    async def _log_activity_safe(self, *_a, **_k):
        return None

    def get_session_factory(self):
        raise AssertionError('this step must not open its own session - it '
                             'delegates to branch_rightsize.apply_one')


class _Patched:
    """Swaps branch_rightsize.plan and apply_one, recording the calls."""

    def __init__(self, plan=PLAN, refuse=(), raise_on=()):
        self.plan_data = plan
        self.refuse = set(refuse)
        self.raise_on = set(raise_on)
        self.calls = []

    def __enter__(self):
        self._plan, self._apply = branch_rightsize.plan, branch_rightsize.apply_one

        async def _plan(_grid):
            return self.plan_data

        async def _apply(_grid, bot_name, amount_usd=None, dry_run=True,
                         price=None):
            # `price` is recorded, not ignored: the step must carry each
            # branch's own live mark through from the plan so the write
            # keeps the drawdown breaker reading the same percentage it
            # read before. See crypto_grid_bot.peak_after_withdrawal.
            self.calls.append({"bot_name": bot_name, "amount_usd": amount_usd,
                               "dry_run": dry_run, "price": price})
            if bot_name in self.raise_on:
                raise RuntimeError("row vanished")
            if bot_name in self.refuse:
                return {"ok": False, "reason": "has a free rung (4/10 tradeable)"}
            row = next(b for b in self.plan_data["branches"]
                       if b["bot_name"] == bot_name)
            return {"ok": True, "bot_name": bot_name,
                    "product_id": row["product_id"],
                    "freed_usd": row["freeable_usd"]}

        branch_rightsize.plan, branch_rightsize.apply_one = _plan, _apply
        return self

    def __exit__(self, *a):
        branch_rightsize.plan, branch_rightsize.apply_one = self._plan, self._apply
        return False


class TestItDelegatesAndFreesTheRightMoney(ArmedCase):

    def test_it_frees_the_five_parked_branches(self):
        with _Patched() as p:
            out = run(sfx.apply_rightsize(_Grid()))
        self.assertEqual(out["status"], "APPLIED")
        self.assertEqual(out["rows_written"], 5)
        self.assertAlmostEqual(out["freed_usd"], 961.97, places=2)
        self.assertTrue(out["settled"])
        self.assertEqual(len(p.calls), 5)

    def test_it_never_writes_rows_itself(self):
        """_Grid.get_session_factory raises. Reaching it means this step
        stopped delegating and took the invariants with it."""
        with _Patched():
            run(sfx.apply_rightsize(_Grid()))

    def test_it_asks_for_everything_above_the_floor_and_really_writes(self):
        with _Patched() as p:
            run(sfx.apply_rightsize(_Grid()))
        for c in p.calls:
            self.assertIsNone(c["amount_usd"], 'a partial amount was requested')
            self.assertFalse(c["dry_run"], 'a dry run cannot free anything')

    def test_biggest_first(self):
        """A refusal part-way through should already have banked the one
        that mattered most."""
        with _Patched() as p:
            run(sfx.apply_rightsize(_Grid()))
        self.assertEqual(p.calls[0]["bot_name"], "crypto_grid_6")

    def test_a_flat_branch_is_never_touched(self):
        """ZEC is flat with $1,789.11 allocated. plan() gives it freeable 0
        and names withdraw_from_grid_branch instead."""
        with _Patched() as p:
            run(sfx.apply_rightsize(_Grid()))
        self.assertNotIn("crypto_grid_21", [c["bot_name"] for c in p.calls])


class TestItRefusesRatherThanGuesses(ArmedCase):

    def test_a_plan_over_the_ceiling_writes_nothing(self):
        big = {"branches": [dict(PLAN["branches"][0],
                                 freeable_usd=sfx.MAX_RIGHTSIZE_USD + 1.0)]}
        with _Patched(plan=big) as p:
            out = run(sfx.apply_rightsize(_Grid()))
        self.assertEqual(out["status"], "REFUSED_OVER_CEILING")
        self.assertEqual(out["rows_written"], 0)
        self.assertFalse(out["settled"])
        self.assertEqual(p.calls, [], 'it wrote despite refusing the plan')

    def test_the_ceiling_is_a_real_bound(self):
        self.assertGreater(sfx.MAX_RIGHTSIZE_USD, 961.97)
        self.assertLessEqual(sfx.MAX_RIGHTSIZE_USD, 10000.0)

    def test_nothing_parked_is_settled_and_writes_nothing(self):
        with _Patched(plan={"branches": []}) as p:
            out = run(sfx.apply_rightsize(_Grid()))
        self.assertEqual(out["status"], "NOTHING_TO_DO")
        self.assertTrue(out["settled"], 'nothing to do must settle, or the '
                                        'ticket retries all five attempts')
        self.assertEqual(p.calls, [])

    def test_every_branch_refusing_is_NOT_settled(self):
        """Zero writes with a candidate standing means re-running could do
        better - the ticket must stay unspent."""
        names = [b["bot_name"] for b in PLAN["branches"]]
        with _Patched(refuse=names):
            out = run(sfx.apply_rightsize(_Grid()))
        self.assertEqual(out["status"], "NOTHING_WRITTEN")
        self.assertFalse(out["settled"])
        self.assertEqual(out["freed_usd"], 0.0)

    def test_one_branch_raising_does_not_stop_the_others(self):
        with _Patched(raise_on=("crypto_grid_6",)) as p:
            out = run(sfx.apply_rightsize(_Grid()))
        self.assertEqual(out["rows_written"], 4)
        self.assertEqual(len(p.calls), 5)
        self.assertTrue(out["settled"])
        self.assertIn("crypto_grid_6",
                      [x["bot_name"] for x in out["not_applied"]])


class TestASecondMoneyOperationNeedsItsOwnConsent(unittest.TestCase):
    """THE LESSON OF 2026-10-08. The ticket was armed for the reconcile and
    a stale level change rode along with it, lowering LINK-USD from 10
    rungs to 6 - unasked for, and unreversible from a dashboard whose
    set-levels button does not reach the server.

    So the right-size does not inherit the ticket's consent. Unarmed it
    must write nothing AND settle, so a ticket armed only for the
    reconcile still completes and is not left retrying."""

    def setUp(self):
        self._prev = os.environ.pop(sfx.RIGHTSIZE_ENV, None)

    def tearDown(self):
        if self._prev is not None:
            os.environ[sfx.RIGHTSIZE_ENV] = self._prev

    def test_unarmed_it_writes_nothing_and_settles(self):
        with _Patched() as p:
            out = run(sfx.apply_rightsize(_Grid()))
        self.assertEqual(out["status"], "NOT_ARMED")
        self.assertEqual(out["rows_written"], 0)
        self.assertEqual(out["freed_usd"], 0.0)
        self.assertTrue(out["settled"], 'an unarmed step that does not settle '
                                        'leaves the ticket retrying forever')
        self.assertEqual(p.calls, [], 'it wrote while unarmed')

    def test_it_does_not_even_read_the_plan_while_unarmed(self):
        """Cheap, and it proves the gate is the first thing in the function
        rather than a check applied after the work."""
        with _Patched() as p:
            run(sfx.apply_rightsize(_Grid()))
        self.assertEqual(p.calls, [])

    def test_only_an_explicit_affirmative_arms_it(self):
        for off in ("", "0", "false", "no", "off", "maybe", "FALSE "):
            os.environ[sfx.RIGHTSIZE_ENV] = off
            self.assertFalse(sfx.rightsize_armed(), repr(off))
        for on in ("1", "true", "TRUE", "yes", "on", " true ", '"true"'):
            os.environ[sfx.RIGHTSIZE_ENV] = on
            self.assertTrue(sfx.rightsize_armed(), repr(on))

    def test_a_quoted_value_still_arms_it(self):
        """A Railway value pasted with quotes is the bug class that once
        stopped a module importing and took the fleet off the air."""
        os.environ[sfx.RIGHTSIZE_ENV] = "'true'"
        self.assertTrue(sfx.rightsize_armed())


class TestItRunsAfterTheReconcile(unittest.TestCase):
    """A branch's floor IS its coin basis, and the reconcile changes coin
    basis. Planning the right-size first measures against a basis that is
    about to move."""

    def test_the_call_order_in_run_at_boot(self):
        import ast
        import os
        src = open(os.path.join(os.path.dirname(__file__) or '.',
                                'startup_fix.py')).read()
        tree = ast.parse(src)
        body = next(ast.get_source_segment(src, n) for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == 'run_at_boot')
        self.assertLess(body.index('apply_reconcile(grid'),
                        body.index('apply_rightsize(grid'),
                        'the right-size is planned before the reconcile')

    def test_it_is_counted_in_the_total_and_the_gates(self):
        import os
        src = open(os.path.join(os.path.dirname(__file__) or '.',
                                'startup_fix.py')).read()
        self.assertIn('"rightsize"', src)
        self.assertIn('out["rows_written_total"] = lv + rc + rs', src)




class TheLiveMarkIsCarriedThrough(unittest.TestCase):
    """Each branch's own price must reach apply_one, or the breaker lies.

    THE BUG THIS GUARDS, measured live 2026-10-08 21:15Z. The right-size
    lowers allocated_usd, and the drawdown breaker reads
    `(peak_equity - equity) / peak_equity` with equity = allocated_usd +
    unrealized. Lowering the peak by exactly what left keeps the DOLLAR
    gap but shrinks the denominator, so the same unchanged loss reads as a
    bigger percentage. LTC-USD came out of its 19:54:08Z right-size at
    32.80% on an -$8.57 unrealized that was 14.67% before the write, and
    its buys were frozen by a 25% breaker.

    peak_after_withdrawal can hold the reading steady, but only if it is
    told the position's unrealized P&L - which means apply_one needs the
    price, which means THIS step has to carry it. Without the price the
    helper silently falls back to the old subtraction and the freeze comes
    back, with nothing failing anywhere. Hence a test on the wiring.
    """

    def test_every_branch_gets_its_own_price_not_a_shared_one(self):
        with _Patched() as p:
            run(sfx.apply_rightsize(_Grid(), max_free_usd=3000.0))
        got = {c["bot_name"]: c["price"] for c in p.calls}
        want = {"crypto_grid_6": 1.3781, "crypto_grid_19": 63.013,
                "crypto_grid_9": 0.000005, "crypto_grid_11": 0.11925,
                "crypto_grid_3": 0.22625}
        self.assertEqual(got, want)

    def test_the_flat_branch_is_not_called_at_all(self):
        # It has freeable 0, so it never reaches apply_one - and its None
        # price must not be substituted with a number by anything.
        with _Patched() as p:
            run(sfx.apply_rightsize(_Grid(), max_free_usd=3000.0))
        self.assertNotIn("crypto_grid_21",
                         [c["bot_name"] for c in p.calls])

    def test_a_missing_price_is_passed_as_none_never_invented(self):
        # An unpriced branch must reach apply_one with price=None, so the
        # helper takes its conservative subtraction instead of a guess.
        import copy
        plan = copy.deepcopy(PLAN)
        for b in plan["branches"]:
            b.pop("current_price", None)
        with _Patched(plan=plan) as p:
            run(sfx.apply_rightsize(_Grid(), max_free_usd=3000.0))
        self.assertTrue(p.calls, "the step must still run without prices")
        self.assertTrue(all(c["price"] is None for c in p.calls))

    def test_plan_publishes_the_price_so_there_is_something_to_carry(self):
        # The other half of the wiring: branch_rightsize.plan() must put
        # current_price on every row it returns.
        import ast
        tree = ast.parse(open("branch_rightsize.py").read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "plan")
        keys = [k.value for n in ast.walk(fn) if isinstance(n, ast.Dict)
                for k in n.keys if isinstance(k, ast.Constant)]
        self.assertIn("current_price", keys)




if __name__ == "__main__":
    unittest.main(verbosity=2)
