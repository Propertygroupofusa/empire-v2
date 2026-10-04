"""The concentration gate, as WIRED - not as a library.

position_caps.py had 21 passing tests and zero call sites. These tests are
about the wiring: that execute_futures_trade actually consults it, that
BUYS are gated and SELLS never are, that the ledger sums across a pass,
and that six simultaneous META orders are refused at the second one.
"""
import ast
import os
import unittest

SRC = open(os.path.join(os.path.dirname(__file__) or '.', 'prop_bot.py')).read()
TREE = ast.parse(SRC)


def body_of(name):
    for n in ast.walk(TREE):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return ast.get_source_segment(SRC, n) or ''
    raise AssertionError('function not found: ' + name)


class TestItIsActuallyWired(unittest.TestCase):
    """The failure mode of the original: built, tested, never called."""

    def test_execute_futures_trade_consults_the_gate(self):
        b = body_of('execute_futures_trade')
        self.assertIn('_check_concentration', b)

    def test_the_gate_is_awaited_not_called_bare(self):
        """It is async. A bare call returns a coroutine, which is truthy,
        and the verdict comparison would silently pass everything."""
        b = body_of('execute_futures_trade')
        self.assertIn('await _check_concentration(', b)

    def test_it_refuses_by_returning_false_before_the_order(self):
        b = body_of('execute_futures_trade')
        gate = b.index('_check_concentration')
        post = b.index('/v2/orders')
        self.assertLess(gate, post, 'the gate must run BEFORE the order is sent')
        self.assertIn('return False', b[gate:post])

    def test_every_outer_cycle_opens_a_pass(self):
        """Three cycles place buys. A cycle that never opens a pass
        inherits the last one, which refuses more than it should."""
        for fn in ('run_prop_cycle', 'run_alpaca_branch_cycles',
                   'run_opening_bar_live_cycle'):
            try:
                b = body_of(fn)
            except AssertionError:
                continue
            self.assertIn('_begin_cap_pass', b, fn + ' never opens a pass')

    def test_the_pass_count_matches_the_cycle_count(self):
        self.assertEqual(SRC.count('_begin_cap_pass(equity)'), 3)

    def test_no_phantom_equity_helper(self):
        """The first draft called _last_known_equity(), which does not
        exist - it would have refused every buy via the except clause
        while looking like a working guard."""
        calls = [n for n in ast.walk(TREE)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == '_last_known_equity']
        self.assertEqual(calls, [], 'calls a function that does not exist')

    def test_every_name_the_gate_calls_exists(self):
        b = body_of('_check_concentration')
        for name in ('get_account_equity', '_total_alpaca_branch_notional',
                     '_total_opening_bar_notional', 'open_prop_positions'):
            self.assertIn(name, b)
            self.assertIn(name, SRC.replace(b, ''), name + ' is not defined anywhere else')


class TestItCallsOnlyMethodsThatExist(unittest.TestCase):
    """The mistake I made TWICE in this one task: calling a name I assumed
    rather than one I had read. _last_known_equity() does not exist, and
    CapLedger has .record() not .commit(). Either one throws, the except
    clause refuses, and EVERY buy is blocked by a guard that looks fine.
    These assert the names against the real objects."""

    def test_the_ledger_methods_the_gate_uses_are_real(self):
        import position_caps as pc
        led = pc.CapLedger()
        b = body_of('_check_concentration')
        import re
        for meth in set(re.findall(r'_CAP_LEDGER\.(\w+)\(', b)):
            self.assertTrue(hasattr(led, meth),
                            'CapLedger has no .%s() - the gate would throw '
                            'and refuse every order' % meth)

    def test_the_position_caps_names_the_gate_uses_are_real(self):
        import position_caps as pc
        b = body_of('_check_concentration')
        import re
        for attr in set(re.findall(r'position_caps\.(\w+)', b)):
            self.assertTrue(hasattr(pc, attr),
                            'position_caps has no %s' % attr)

    def test_the_gate_actually_runs_end_to_end(self):
        """Not a source check: execute the real function against the real
        module and require a verdict, not an exception-driven refusal."""
        import asyncio, position_caps as pc
        import prop_bot as pb
        pb._begin_cap_pass(1007.0)
        got = asyncio.new_event_loop().run_until_complete(
            pb._check_concentration(None, 'META', 0.163475, 748.91))
        self.assertIn(got[0], (pc.ALLOW, pc.REFUSE))
        self.assertNotIn('error', str(got[1] or '').lower(),
                         'gate errored instead of deciding: %r' % (got,))


class TestBuysOnly(unittest.TestCase):
    def test_the_gate_is_inside_a_buy_only_branch(self):
        """A gate that can refuse a SELL could trap the account in a
        position it needs to leave - worse than the bug being fixed."""
        b = body_of('execute_futures_trade')
        i = b.index('_check_concentration')
        before = b[:i]
        self.assertIn('if side == "buy":', before)
        guard = before.rindex('if side == "buy":')
        between = before[guard:]
        self.assertNotIn('\n    ', between.replace('\n        ', '\n@@'),
                         'the gate appears to have left the buy-only block')


class TestTheLedgerSumsAcrossThePass(unittest.TestCase):
    """The actual arithmetic, against position_caps directly."""

    def setUp(self):
        import position_caps as pc
        self.pc = pc
        # The real 28 Sep shape: $1,007 equity, six META orders.
        self.caps = {'max_per_position': 200.0, 'max_total_notional': 500.0,
                     'max_open_positions': 8}

    def test_six_simultaneous_meta_orders_are_refused_at_the_second(self):
        pc = self.pc
        led = pc.CapLedger()
        verdicts = []
        for _ in range(6):
            v, why, n = pc.check_order('META', 0.163475, 748.91, self.caps,
                                       ledger=led)
            verdicts.append(v)
            if v == pc.ALLOW:
                led.record('META', n)
        self.assertEqual(verdicts[0], pc.ALLOW, 'the first order is fine')
        self.assertTrue(all(v == pc.REFUSE for v in verdicts[1:]),
                        'orders 2-6 must ALL be refused, got ' + str(verdicts))
        self.assertAlmostEqual(led.committed_for('META'), 122.42, places=1)

    def test_without_a_ledger_all_six_would_pass(self):
        """Proves the ledger is what does the work - this is the old bug."""
        pc = self.pc
        verdicts = [pc.check_order('META', 0.163475, 748.91, self.caps,
                                   ledger=None)[0] for _ in range(6)]
        self.assertTrue(all(v == pc.ALLOW for v in verdicts),
                        'the unsummed path lets all six through, which is the bug')

    def test_the_total_cap_stops_a_spread_out_pileup(self):
        """Different symbols, same pass, still summed."""
        pc = self.pc
        led = pc.CapLedger()
        allowed = 0
        for sym in ('META', 'QQQ', 'SPY', 'AAPL', 'MSFT', 'NVDA'):
            v, _w, n = pc.check_order(sym, 0.163475, 748.91, self.caps, ledger=led)
            if v == pc.ALLOW:
                allowed += 1
                led.record(sym, n)
        self.assertLessEqual(led.committed_total(), 500.0)
        self.assertLess(allowed, 6, 'the total cap must bite before the sixth')


class TestTheWiredLedgerAccumulates(unittest.TestCase):
    """The ledger sums *through the wiring*, not just in the library.

    TestTheLedgerSumsAcrossThePass drives position_caps directly, so it
    stays green even if the wiring stops recording. That is the exact
    Sept 28 defect - six META orders each sized against a denominator
    that did not include the other five - so it needs a test that can
    see it. Replacing `_CAP_LEDGER.record(...)` with `pass` must fail
    here.
    """

    def _six_meta_orders(self, equity=1007.0):
        import asyncio
        import prop_bot as pb
        led = pb._begin_cap_pass(equity)
        loop = asyncio.new_event_loop()
        try:
            out = [loop.run_until_complete(
                pb._check_concentration(None, 'META', 0.163475, 748.91))
                for _ in range(6)]
        finally:
            loop.close()
        return led, out

    def test_the_wired_gate_records_what_it_allows(self):
        led, out = self._six_meta_orders()
        allowed = [n for v, _w, n in out if v == 'ALLOW']
        self.assertTrue(allowed, 'the first order must be allowed')
        self.assertAlmostEqual(led.committed_for('META'), sum(allowed),
                               places=2,
                               msg='the ledger did not record the allowed '
                                   'orders - every order is being sized '
                                   'against a denominator missing the others')

    def test_the_wired_gate_refuses_the_pileup(self):
        _led, out = self._six_meta_orders()
        verdicts = [v for v, _w, _n in out]
        self.assertEqual(verdicts[0], 'ALLOW', 'the first order is fine')
        self.assertIn('REFUSE', verdicts,
                      'six identical orders in one pass must hit the cap: '
                      + str(verdicts))
        self.assertLess(verdicts.count('ALLOW'), 6,
                        'all six passed, which IS the Sept 28 bug: '
                        + str(verdicts))
        self.assertEqual(verdicts[-1], 'REFUSE',
                         'the last of six must be refused')

    def test_the_wired_pass_stays_under_its_own_cap(self):
        import position_caps as pc
        led, _out = self._six_meta_orders()
        caps = pc.caps_from_market_brain(1007.0)
        self.assertLessEqual(led.committed_for('META'),
                             caps['max_per_position'] + 0.01,
                             'the gate let the pass exceed its per-position cap')
        self.assertLessEqual(led.committed_total(),
                             caps['max_total_notional'] + 0.01)

    def test_a_new_pass_starts_the_ledger_empty(self):
        """_begin_cap_pass must reset, or yesterday's commitments block
        today's trades forever."""
        led1, _ = self._six_meta_orders()
        self.assertGreater(led1.committed_total(), 0)
        import prop_bot as pb
        led2 = pb._begin_cap_pass(1007.0)
        self.assertEqual(led2.committed_total(), 0.0)
        self.assertIsNot(led1, led2)


class TestFailsClosed(unittest.TestCase):
    def test_unreadable_caps_refuse(self):
        import position_caps as pc
        v, why, _n = pc.check_order('META', 1, 100, {'_unreadable': 'boom'})
        self.assertEqual(v, pc.REFUSE)
        self.assertIn('unreadable', why)

    def test_no_mandate_refuses(self):
        import position_caps as pc
        self.assertEqual(pc.check_order('META', 1, 100, None)[0], pc.REFUSE)

    def test_the_wrapper_catches_and_refuses(self):
        b = body_of('_check_concentration')
        self.assertIn('except Exception', b)
        tail = b[b.index('except Exception'):]
        self.assertIn('REFUSE', tail, 'an error must refuse, never allow')


class TestRefusalsAreVisible(unittest.TestCase):
    def test_a_refusal_is_recorded(self):
        b = body_of('execute_futures_trade')
        self.assertIn('_record_cap_refusal', b)

    def test_there_is_a_way_to_read_them(self):
        self.assertIn('def concentration_refusals', SRC)

    def test_the_refusal_log_is_bounded(self):
        b = body_of('_record_cap_refusal')
        self.assertIn('_CAP_REFUSALS[:-50]', b)


if __name__ == '__main__':
    unittest.main(verbosity=2)
