"""The drawdown breaker must measure market loss, never withdrawn claim.

WHAT WAS WRONG, 2026-10-05. The breaker computes
`(peak_equity - equity) / peak_equity` with equity = allocated_usd +
unrealized. peak_equity was a pure one-way ratchet, but allocated_usd is
also what a right-size, a concentration rotation or a fleet re-scale
DEBITS. So taking money out of a branch on purpose registered as that
branch crashing, and the breaker froze its buys.

Measured on the live fleet that day: the fleet-wide gap between peak and
current equity was $1,285.56, of which $1,085.22 (84.4%) was withdrawn
claim rather than market loss. The figures pinned below are that real
fleet state, so a regression fails against the account's own numbers
rather than against invented ones.

withdraw_from_grid_branch - the harvest's path - was already correct and
is pinned here so a refactor cannot quietly drop it.
"""
import ast
import unittest

import crypto_grid_bot as grid


class FakeSlice:
    def __init__(self, qty, entry_price):
        self.qty = qty
        self.entry_price = entry_price


class PeakAfterWithdrawal(unittest.TestCase):
    """Prevention: money leaving on purpose lowers the high-water mark."""

    def test_the_live_qnt_rotation_no_longer_reads_as_a_crash(self):
        # The real event: peak $285.40, a rotation took $142.97 out,
        # leaving $142.43. The old ratchet left peak alone and the
        # breaker read 50.09%.
        before, after, peak = 285.40, 142.43, 285.40
        old_dd = (peak - after) / peak
        self.assertGreater(old_dd, grid.GRID_DRAWDOWN_BREAKER_PCT)
        self.assertAlmostEqual(old_dd * 100, 50.09, places=1)

        new_peak = grid.peak_after_withdrawal(peak, before, after)
        self.assertAlmostEqual(new_peak, 142.43, places=2)
        new_dd = (new_peak - after) / new_peak
        self.assertAlmostEqual(new_dd, 0.0, places=6)
        self.assertLess(new_dd, grid.GRID_DRAWDOWN_BREAKER_PCT)

    def test_the_live_ltc_rightsize_no_longer_freezes_the_branch(self):
        # LTC: allocated $197.82 -> $74.24 floor, peak $218.53, and
        # -$1.31 of real unrealized that must SURVIVE the adjustment.
        before, after, peak, unreal = 197.82, 74.24, 218.53, -1.31
        self.assertGreater((peak - (after + unreal)) / peak,
                           grid.GRID_DRAWDOWN_BREAKER_PCT)

        new_peak = grid.peak_after_withdrawal(peak, before, after)
        self.assertAlmostEqual(new_peak, 94.95, places=2)   # 218.53 - 123.58
        new_dd = (new_peak - (after + unreal)) / new_peak
        self.assertLess(new_dd, grid.GRID_DRAWDOWN_BREAKER_PCT)

    def test_the_real_market_loss_in_dollars_is_preserved_exactly(self):
        # The whole point: subtract what left, so the dollar gap between
        # peak and equity - which IS the market loss - does not move.
        peak, before, unreal = 500.0, 400.0, -37.50
        after = 250.0
        gap_before = peak - (before + unreal)
        new_peak = grid.peak_after_withdrawal(peak, before, after)
        gap_after = new_peak - (after + unreal)
        self.assertAlmostEqual(gap_before, gap_after, places=6)
        self.assertAlmostEqual(gap_after, 137.50, places=2)

    def test_a_genuine_crash_still_breaches_after_a_withdrawal(self):
        # The safety direction. A branch that really is down 40% on its
        # coin must STILL breach once money is also taken out - the fix
        # must not blunt the breaker.
        peak, before, after = 1000.0, 800.0, 600.0
        unreal = -400.0           # a real 40% hole in the position
        new_peak = grid.peak_after_withdrawal(peak, before, after)
        self.assertAlmostEqual(new_peak, 800.0, places=2)
        dd = (new_peak - (after + unreal)) / new_peak
        self.assertGreater(dd, grid.GRID_DRAWDOWN_BREAKER_PCT)

    def test_a_deposit_never_raises_the_peak(self):
        # peak must keep ratcheting up on its own, from real equity only.
        # Raising it here would hand the branch a high it never reached.
        self.assertIsNone(grid.peak_after_withdrawal(100.0, 100.0, 250.0))

    def test_a_no_op_changes_nothing(self):
        self.assertIsNone(grid.peak_after_withdrawal(100.0, 100.0, 100.0))

    def test_a_null_peak_stays_null_so_the_read_path_self_heals(self):
        # NULL is "not yet initialized" everywhere in this file; the
        # cycle heals it to current equity. Writing a number would
        # invent a high-water mark.
        self.assertIsNone(grid.peak_after_withdrawal(None, 400.0, 100.0))

    def test_unreadable_allocations_change_nothing(self):
        self.assertIsNone(grid.peak_after_withdrawal(100.0, None, 50.0))
        self.assertIsNone(grid.peak_after_withdrawal(100.0, 50.0, None))

    def test_it_never_returns_a_negative_peak(self):
        self.assertEqual(grid.peak_after_withdrawal(10.0, 500.0, 1.0), 0.0)


class PeakForFlatBranch(unittest.TestCase):
    """Repair: a branch with no position cannot be in drawdown."""

    def test_it_repairs_the_live_qnt_row(self):
        # Prevention cannot undo a peak already wrong in the database.
        # QNT's $285.40 against $142.43 and ZERO slices is the one case
        # that can be fixed from first principles.
        self.assertAlmostEqual(
            grid.peak_for_flat_branch(285.40, 142.43, []), 142.43, places=2)

    def test_it_refuses_to_touch_a_branch_still_holding_coin(self):
        # ZEC: peak $2,279.94, allocated $1,808.37, three open slices.
        # Its gap mixes withdrawn claim with real market loss and
        # nothing stored can separate them, so it stays as it is.
        self.assertIsNone(grid.peak_for_flat_branch(
            2279.94, 1808.37, [FakeSlice(0.1, 1650.61)]))

    def test_it_leaves_a_flat_branch_that_is_already_correct_alone(self):
        self.assertIsNone(grid.peak_for_flat_branch(142.43, 142.43, []))
        self.assertIsNone(grid.peak_for_flat_branch(100.0, 142.43, []))

    def test_it_never_lowers_a_peak_on_unreadable_input(self):
        self.assertIsNone(grid.peak_for_flat_branch(None, 142.43, []))
        self.assertIsNone(grid.peak_for_flat_branch(285.40, None, []))

    def test_a_flat_branch_cannot_be_frozen_after_the_clamp(self):
        peak = grid.peak_for_flat_branch(285.40, 142.43, [])
        dd = (peak - 142.43) / peak
        self.assertAlmostEqual(dd, 0.0, places=6)
        self.assertLess(dd, grid.GRID_DRAWDOWN_BREAKER_PCT)


class EffectivePeakEquity(unittest.TestCase):
    """The whole decision, tested by behaviour instead of by shape.

    An earlier draft asserted on the source structure and a mutation that
    computed the clamp and then threw the result away walked straight
    through. Composing the three rules into one pure function means the
    test can just ask what the breaker will see.
    """

    DD = None   # set in setUp from the live constant

    def setUp(self):
        self.DD = grid.GRID_DRAWDOWN_BREAKER_PCT

    def dd(self, peak, equity):
        return (peak - equity) / peak if peak > 0 else 0.0

    def test_the_live_qnt_row_comes_back_unfrozen(self):
        # peak $285.40, allocated $142.43, ZERO slices -> the breaker
        # was reading 50.09% and had frozen the branch's buys.
        peak, clamped = grid.effective_peak_equity(285.40, 142.43, [], 142.43)
        self.assertAlmostEqual(peak, 142.43, places=2)
        self.assertAlmostEqual(clamped, 285.40, places=2)
        self.assertLess(self.dd(peak, 142.43), self.DD)

    def test_the_live_zec_row_is_left_exactly_as_it_was(self):
        # Three open slices, so its gap mixes withdrawn claim with real
        # market loss. Nothing stored separates them; it stays frozen.
        equity = 1808.37 - 114.73
        peak, clamped = grid.effective_peak_equity(
            2279.94, 1808.37, [FakeSlice(0.126, 1650.61)], equity)
        self.assertAlmostEqual(peak, 2279.94, places=2)
        self.assertIsNone(clamped)
        self.assertGreater(self.dd(peak, equity), self.DD)

    def test_a_null_peak_self_heals_to_current_equity(self):
        peak, clamped = grid.effective_peak_equity(None, 500.0, [FakeSlice(1, 1)], 480.0)
        self.assertAlmostEqual(peak, 480.0, places=2)
        self.assertIsNone(clamped)
        self.assertAlmostEqual(self.dd(peak, 480.0), 0.0, places=6)

    def test_it_still_ratchets_up_on_a_real_new_high(self):
        peak, clamped = grid.effective_peak_equity(400.0, 380.0, [FakeSlice(1, 1)], 450.0)
        self.assertAlmostEqual(peak, 450.0, places=2)
        self.assertIsNone(clamped)

    def test_a_genuine_position_loss_is_still_measured_in_full(self):
        # 40% down on the coin, nothing withdrawn. Must still breach.
        peak, clamped = grid.effective_peak_equity(1000.0, 1000.0, [FakeSlice(10, 60)], 600.0)
        self.assertAlmostEqual(peak, 1000.0, places=2)
        self.assertIsNone(clamped)
        self.assertGreater(self.dd(peak, 600.0), self.DD)

    def test_the_clamp_and_the_ratchet_agree(self):
        # The clamp and the ratchet reach for the same number on a flat
        # branch, because with no slices equity IS allocated_usd. Swapping
        # their order was tried as a mutation and changed no result, so
        # this asserts the OUTCOME rather than the sequence - the earlier
        # version of this test claimed an ordering dependency that does
        # not exist.
        peak, clamped = grid.effective_peak_equity(285.40, 142.43, [], 142.43)
        self.assertAlmostEqual(peak, 142.43, places=2,
                               msg="the ratchet undid the flat-branch clamp")
        self.assertNotAlmostEqual(peak, 285.40, places=2)

    def test_the_result_is_actually_returned_not_just_computed(self):
        # The M10 mutation: clamp computed, result dropped on the floor.
        # Behaviour catches what structure could not.
        for stored, alloc in ((285.40, 142.43), (1000.0, 15.0), (70.0, 18.0)):
            peak, _ = grid.effective_peak_equity(stored, alloc, [], alloc)
            self.assertAlmostEqual(peak, alloc, places=2,
                                   msg=f"peak {stored} -> {peak}, expected {alloc}")

    def test_a_flat_branch_is_never_frozen_whatever_its_history(self):
        # The property that makes the repair safe: no slices, no position,
        # no drawdown. Sweep a wide range of wrong stored peaks.
        for stored in (20.0, 100.0, 285.40, 1000.0, 99999.0):
            peak, _ = grid.effective_peak_equity(stored, 142.43, [], 142.43)
            self.assertLess(self.dd(peak, 142.43), self.DD,
                            msg=f"a flat branch froze with a stored peak of {stored}")

    def test_withdrawal_then_read_leaves_the_branch_trading(self):
        # End to end on the LTC numbers: right-size lowers the peak, then
        # the cycle reads it. Before the fix this sequence read 66.63%.
        before, after, peak, unreal = 197.82, 74.24, 218.53, -1.31
        new_stored = grid.peak_after_withdrawal(peak, before, after)
        eff, _ = grid.effective_peak_equity(
            new_stored, after, [FakeSlice(1.06, 70.0)], after + unreal)
        self.assertLess(self.dd(eff, after + unreal), self.DD)


class TheWiringIsActuallyThere(unittest.TestCase):
    """A correct helper nothing calls fixes nothing. Pin every call site.

    These assert on PARSED CALLS, not on substrings. The first draft of
    this class searched the raw source text and three of eight mutations
    walked straight through it: the helper names appear in the comments
    that explain each site, so deleting the actual call left the comment
    behind and the substring test still passed. A test that a comment can
    satisfy is not a test. Line numbers come from the AST for the same
    reason - the ordering check was being satisfied by a comment block
    sitting above the code it described.
    """

    @staticmethod
    def _calls(path, name):
        """Line numbers of every real call to `name` in `path`."""
        tree = ast.parse(open(path).read())
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            fname = getattr(f, 'id', None) or getattr(f, 'attr', None)
            if fname == name:
                out.append(node.lineno)
        return sorted(out)

    @staticmethod
    def _stmt_line(path, text):
        """Line number of the single source line whose code is `text`.

        Comments are stripped before matching so a line that merely
        mentions the statement cannot be mistaken for it.
        """
        hits = []
        for n, line in enumerate(open(path).read().splitlines(), 1):
            code = line.split('#', 1)[0].strip()
            if code == text:
                hits.append(n)
        if len(hits) != 1:
            raise AssertionError(f"expected exactly one `{text}` in {path}, found {hits}")
        return hits[0]

    def test_the_cycle_uses_the_composed_decision(self):
        # If the cycle ever hand-rolls the three rules again, the
        # behavioural tests above stop covering what actually runs.
        calls = self._calls('crypto_grid_bot.py', 'effective_peak_equity')
        self.assertTrue(calls, "run_grid_branch_cycle no longer calls effective_peak_equity")

    def test_the_concentration_rotation_really_lowers_the_peak(self):
        site = self._stmt_line('crypto_grid_bot.py',
                               'source.allocated_usd = round(source.allocated_usd - amount, 2)')
        calls = self._calls('crypto_grid_bot.py', 'peak_after_withdrawal')
        near = [n for n in calls if site - 3 <= n <= site + 12]
        self.assertTrue(near, f"no peak_after_withdrawal call near line {site}; calls at {calls}")

    def test_the_fleet_rescale_really_lowers_the_peak(self):
        site = self._stmt_line('crypto_grid_bot.py', 'branch.allocated_usd = new_usd')
        calls = self._calls('crypto_grid_bot.py', 'peak_after_withdrawal')
        near = [n for n in calls if site <= n <= site + 8]
        self.assertTrue(near, f"no peak_after_withdrawal call near line {site}; calls at {calls}")

    def test_the_rightsize_really_lowers_the_peak(self):
        site = self._stmt_line('branch_rightsize.py', 'branch.allocated_usd = new_alloc')
        calls = self._calls('branch_rightsize.py', 'peak_after_withdrawal')
        near = [n for n in calls if site <= n <= site + 12]
        self.assertTrue(near, f"no peak_after_withdrawal call near line {site}; calls at {calls}")

    def test_every_grid_branch_debit_site_is_accounted_for(self):
        # The guard against a FOURTH path growing later and quietly
        # reintroducing the fault. Every statement that lowers a grid
        # branch's allocated_usd must sit within a few lines of a
        # peak_after_withdrawal call, or reset the peak itself the way
        # the FLAT-only harvest path does.
        known = {
            'crypto_grid_bot.py': [
                'source.allocated_usd = round(source.allocated_usd - amount, 2)',
                'branch.allocated_usd = new_usd',
            ],
            'branch_rightsize.py': ['branch.allocated_usd = new_alloc'],
        }
        for path, stmts in known.items():
            calls = self._calls(path, 'peak_after_withdrawal')
            for s in stmts:
                site = self._stmt_line(path, s)
                self.assertTrue([n for n in calls if site - 3 <= n <= site + 12],
                                f"{path}:{site} `{s}` debits a branch with no peak adjustment")

    def test_the_harvest_path_still_resets_its_own_peak(self):
        # withdraw_from_grid_branch was already right. It is FLAT-only,
        # so the blunt assignment is exact there. Pinned so a refactor
        # cannot drop it on the assumption the new helper covers it.
        src = open('crypto_grid_bot.py').read()
        i = src.index('async def withdraw_from_grid_branch')
        body = src[i:i + 7000]
        self.assertIn('branch.peak_equity = branch.allocated_usd', body)

    def test_nothing_here_sells_cancels_or_places_an_order(self):
        # The breaker only ever decides whether a branch may BUY. This
        # change must not have grown a selling path.
        rs = open('branch_rightsize.py').read()
        for name in ('place_market_sell', 'place_market_buy', '_submit_order',
                     'close_all_grid_slices', 'cancel_order'):
            self.assertNotIn(name, rs, f"branch_rightsize must not reference {name}")


if __name__ == '__main__':
    unittest.main(verbosity=2)
