"""The buy gate's backing check - does the branch hold what its books claim?

WHAT THIS EXISTS TO CATCH. On 2026-10-04 at 01:53:19Z the grid bought
3.24 LINK ($45.60) into a branch whose books claimed 9.34 units while the
wallet held 3.46 - 37.045% backed, can_be_sold false. Three more branches
were in the same state: SOL 25.0%, ALGO 43.301%, ACH 0.000086%. $245.23
of claimed coin was not in the wallet.

The rung count that opens the buy gate judges a slice by its DOLLAR
BASIS, and an unbacked slice carries an ordinary basis, so the gate was
structurally unable to see any of it.
"""
import ast
import asyncio
import os
import unittest

import account_census
import crypto_grid_bot as g
import slice_backing

HERE = os.path.dirname(__file__) or '.'
SRC = open(os.path.join(HERE, 'crypto_grid_bot.py')).read()
TREE = ast.parse(SRC)


def body_of(name):
    for n in ast.walk(TREE):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return ast.get_source_segment(SRC, n) or ''
    raise AssertionError('function not found: ' + name)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# The real LINK shape, from the live fleet at 2026-10-04T04:11Z.
LINK_SLICES = [{"qty": 0.22, "entry_price": 14.343},
               {"qty": 3.04, "entry_price": 15.212},
               {"qty": 2.84, "entry_price": 14.651},
               {"qty": 3.24, "entry_price": 14.074}]   # the buy that should not have happened
LINK_PRICE = 14.03


class TestTheNormalizerIsShared(unittest.TestCase):
    """A gate that normalizes differently from the owner's page is worse
    than no gate. Same class of bug as re-implementing tradeable_slices."""

    BAL = {"available": True,
           "available_units": {"LINK": 3.46, "BTC": 0.01},
           "held_including_zero": {"LINK": 3.46, "BTC": 0.02, "TIA": 0.0,
                                   "PRIME": 0.0, "USD": 3327.85}}

    def _dashboard_recipe(self, bal):
        """The three lines routers/trading_dashboard.py runs inline."""
        avail = dict(bal.get("available_units") or {})
        for cur, tot in (bal.get("held_including_zero") or {}).items():
            avail.setdefault(cur, 0.0 if not tot else avail.get(cur, 0.0))
        return avail

    def test_it_equals_the_dashboards_inline_recipe(self):
        self.assertEqual(account_census.available_units_map(self.BAL),
                         self._dashboard_recipe(self.BAL))

    def test_the_two_inline_copies_still_match_it(self):
        """Pins the copies in the dashboard and the truth worker. If either
        drifts, this fails instead of the gate silently disagreeing."""
        for path in ('routers/trading_dashboard.py', 'exchange_truth_worker.py'):
            src = open(os.path.join(HERE, path)).read()
            self.assertIn('setdefault(cur, 0.0 if not', src.replace('_cur', 'cur').replace('_tot', 'tot'),
                          path + ' no longer carries the recipe this is pinned to')

    def test_a_confirmed_zero_lands_as_zero(self):
        """fetch_balances filters available_units on total>0, so an asset
        the venue lists at exactly 0.0 is MISSING from it - and a confirmed
        zero is the largest shortfall a branch can have."""
        out = account_census.available_units_map(self.BAL)
        self.assertEqual(out["TIA"], 0.0)
        self.assertEqual(out["PRIME"], 0.0)

    def test_it_never_raises_an_available_figure(self):
        """held_including_zero is available+hold. Letting it overwrite
        would count coin the venue will not release."""
        self.assertEqual(account_census.available_units_map(self.BAL)["BTC"], 0.01)

    def test_unreadable_is_none_not_empty(self):
        self.assertIsNone(account_census.available_units_map({"available": False}))
        self.assertIsNone(account_census.available_units_map(None))

    def test_an_asset_in_neither_map_stays_absent(self):
        out = account_census.available_units_map(self.BAL)
        self.assertNotIn("ZEC", out)


class TestOwnedIsNotAvailable(unittest.TestCase):
    """THE BUG THIS CLASS EXISTS FOR. The gate shipped reading
    available_units and refused buys on SOL, LINK, ALGO and ACH for four
    hours. Measured 2026-10-04 05:13Z:

        SOL   claims   1.034600  owns   1.034600  available   0.258650
        LINK  claims   9.340000  owns  10.090000  available   3.460000
        ALGO  claims 492.700000  owns 1347.646389 available 213.346389
        ACH   claims 5345.2000   owns 5345.204595 available   0.004595

    Every one owns at least what it claims. All $245.23 called "not in
    the wallet" was the fleet's own resting sell orders."""

    BAL = {"available": True,
           "available_units": {"SOL": 0.25865002, "LINK": 3.46},
           "held_including_zero": {"SOL": 1.03460007, "LINK": 10.09, "TIA": 0.0}}

    def test_unreadable_is_none_for_BOTH_maps(self):
        """None means nobody could read it; {} would mean the account owns
        nothing, which would be the largest shortfall there is."""
        for fn in (account_census.owned_units_map,
                   account_census.available_units_map):
            self.assertIsNone(fn({"available": False}), fn.__name__)
            self.assertIsNone(fn(None), fn.__name__)
            self.assertIsNone(fn({}), fn.__name__)

    def test_the_two_maps_differ_when_coin_is_on_hold(self):
        av = account_census.available_units_map(self.BAL)
        ow = account_census.owned_units_map(self.BAL)
        self.assertLess(av["SOL"], ow["SOL"])
        self.assertEqual(ow["SOL"], 1.03460007)

    def test_the_gate_reads_OWNED(self):
        b = body_of('wallet_owned_units')
        self.assertIn('owned_units_map', b)
        self.assertNotIn('available_units_map', b,
                         'the gate is back on AVAILABLE - it will refuse '
                         'branches whose coin is merely on a resting order')

    def test_coin_on_hold_is_not_a_refusal(self):
        """The four real branches, each with its real owned units."""
        for pid, claim, owns, px in (
                ("SOL-USD", 1.03460007, 1.03460007, 120.53),
                ("LINK-USD", 9.34, 10.09, 14.027),
                ("ALGO-USD", 492.70, 1347.646389, 0.12896),
                ("ACH-USD", 5345.20, 5345.204595, 0.00621)):
            ok, why = run(g.branch_backing_verdict(
                pid, [{"qty": claim, "entry_price": px}], px,
                {pid.split("-")[0]: owns}))
            self.assertTrue(ok, '%s owns what it claims and must not be '
                                'refused: %s' % (pid, why))

    def test_a_GENUINE_shortfall_is_still_refused(self):
        """QNT, from slice_backing's own docstring: 0.675982 claimed,
        0.00097323 actually owned. The gate must still catch this."""
        ok, why = run(g.branch_backing_verdict(
            "QNT-USD", [{"qty": 0.675982, "entry_price": 130.0}], 130.0,
            {"QNT": 0.00097323}))
        self.assertFalse(ok)
        self.assertIn('0.144% backed', why)

    def test_it_agrees_with_reconcile_about_what_is_owned(self):
        """reconcile-slices picks held_including_zero and its comment says
        why. These two must never disagree about ownership."""
        src = open(os.path.join(HERE, 'routers/trading_dashboard.py')).read()
        i = src.index('OWNED, NOT AVAILABLE')
        self.assertIn('held_including_zero', src[i:i+1200])
        import inspect
        self.assertIn('held_including_zero',
                      inspect.getsource(account_census.owned_units_map),
                      'owned_units_map no longer reads the owned map')


class TestItRefusesTheRealThing(unittest.TestCase):
    def test_it_refuses_the_link_branch_that_took_the_45_dollars(self):
        ok, why = run(g.branch_backing_verdict(
            "LINK-USD", LINK_SLICES, LINK_PRICE, {"LINK": 3.46}))
        self.assertFalse(ok)
        self.assertIn("37.045% backed", why)
        self.assertIn("9.34000000 LINK", why)

    def test_it_refuses_all_four_live_unbacked_branches(self):
        cases = [("SOL-USD", 1.03460007, 0.25865002, 120.0, "25.000"),
                 ("LINK-USD", 9.34, 3.46, 14.03, "37.045"),
                 ("ALGO-USD", 492.70, 213.346389, 0.1289, "43.301"),
                 ("ACH-USD", 5345.20, 0.00459504, 0.00621, "0.000")]
        for pid, claim, have, px, pct in cases:
            asset = pid.split("-")[0]
            ok, why = run(g.branch_backing_verdict(
                pid, [{"qty": claim, "entry_price": px}], px, {asset: have}))
            self.assertFalse(ok, pid + ' must be refused')
            self.assertIn(pct, why, pid)

    def test_a_fully_backed_branch_is_allowed(self):
        ok, why = run(g.branch_backing_verdict(
            "BTC-USD", [{"qty": 0.01, "entry_price": 84000.0}], 84000.0,
            {"BTC": 0.01}))
        self.assertTrue(ok)
        self.assertEqual(why, "backed")

    def test_fifty_percent_exactly_is_backed(self):
        """BACKED_ENOUGH_PCT is the boundary and it belongs to slice_backing,
        not to this gate - so the gate must not invent its own."""
        ok, _ = run(g.branch_backing_verdict(
            "X-USD", [{"qty": 2.0, "entry_price": 100.0}], 100.0, {"X": 1.0}))
        self.assertTrue(ok)
        ok2, _ = run(g.branch_backing_verdict(
            "X-USD", [{"qty": 2.0, "entry_price": 100.0}], 100.0, {"X": 0.99}))
        self.assertFalse(ok2)

    def test_a_confirmed_zero_balance_is_refused(self):
        ok, why = run(g.branch_backing_verdict(
            "ACH-USD", [{"qty": 5345.2, "entry_price": 0.00621}], 0.00621,
            {"ACH": 0.0}))
        self.assertFalse(ok)

    def test_the_reason_names_the_owners_remedy_and_nothing_else(self):
        _ok, why = run(g.branch_backing_verdict(
            "LINK-USD", LINK_SLICES, LINK_PRICE, {"LINK": 3.46}))
        self.assertIn("reconcile-slices", why)
        self.assertNotIn("sell", why.lower().replace("a sell sizes", ""))


class TestUnknownIsNotARefusal(unittest.TestCase):
    """slice_backing's own doctrine: an unreadable balance is not a
    shortfall. Treating it as one would freeze the fleet on a rate limit."""

    def test_none_units_does_not_refuse(self):
        ok, why = run(g.branch_backing_verdict(
            "LINK-USD", LINK_SLICES, LINK_PRICE, None))
        self.assertTrue(ok)
        self.assertIn("unreadable", why)

    def test_an_asset_absent_from_the_map_does_not_refuse(self):
        ok, why = run(g.branch_backing_verdict(
            "ZEC-USD", [{"qty": 5.0, "entry_price": 40.0}], 40.0, {"BTC": 1.0}))
        self.assertTrue(ok)
        self.assertIn("unknown", why.lower())

    def test_an_empty_map_is_not_the_same_as_none(self):
        """{} means the account holds nothing; None means nobody could read
        it. The first is a real shortfall only for assets it names."""
        ok, _why = run(g.branch_backing_verdict(
            "LINK-USD", LINK_SLICES, LINK_PRICE, {}))
        self.assertTrue(ok, 'an asset absent from {} is unknown, not short')

    def test_the_cache_returns_none_rather_than_an_empty_map(self):
        """RUN, not read. `{}` would mean "the account holds nothing",
        which is a different claim from "nobody could read it" - and a
        source-text check cannot tell the two returns apart."""
        orig = account_census.owned_units_map
        orig_fetch = account_census.fetch_balances
        async def _no_network(_s):
            return {"available": True, "available_units": {}, "held_including_zero": {}}
        try:
            account_census.fetch_balances = _no_network
            account_census.owned_units_map = lambda _b: None
            g._WALLET_UNITS_CACHE.update(units=None, at=0.0)
            self.assertIsNone(run(g.wallet_owned_units()))
        finally:
            account_census.owned_units_map = orig
            account_census.fetch_balances = orig_fetch
            g._WALLET_UNITS_CACHE.update(units=None, at=0.0)

    def test_a_readable_map_is_cached_and_returned(self):
        orig = account_census.owned_units_map
        orig_fetch = account_census.fetch_balances
        calls = []
        async def _no_network(_s):
            return {"available": True, "available_units": {"LINK": 3.46},
                    "held_including_zero": {"LINK": 3.46}}
        try:
            account_census.fetch_balances = _no_network
            account_census.owned_units_map = lambda _b: (calls.append(1)
                                                             or {"LINK": 3.46})
            g._WALLET_UNITS_CACHE.update(units=None, at=0.0)
            first = run(g.wallet_owned_units())
            second = run(g.wallet_owned_units())
            self.assertEqual(first, {"LINK": 3.46})
            self.assertEqual(second, {"LINK": 3.46})
            self.assertEqual(len(calls), 1,
                             'the wallet was re-read inside its own TTL - '
                             '22 branches a cycle is what got this rate-limited')
        finally:
            account_census.owned_units_map = orig
            account_census.fetch_balances = orig_fetch
            g._WALLET_UNITS_CACHE.update(units=None, at=0.0)


    def test_the_cache_expires(self):
        """A cache that never expires is worse than none here: once the
        owner runs reconcile-slices and the books are correct, a frozen
        wallet reading would keep refusing buys forever."""
        orig_fetch = account_census.fetch_balances
        calls = []
        async def _no_network(_s):
            calls.append(1)
            return {"available": True,
                    "available_units": {"LINK": 9.34},
                    "held_including_zero": {"LINK": 9.34}}
        try:
            account_census.fetch_balances = _no_network
            # A reading older than the TTL must not be served.
            g._WALLET_UNITS_CACHE.update(units={"LINK": 3.46},
                                         at=0.0)   # 1970 - long expired
            got = run(g.wallet_owned_units())
            self.assertEqual(len(calls), 1,
                             'an expired wallet reading was served from cache')
            self.assertEqual(got, {"LINK": 9.34},
                             'the stale reading was returned instead of the fresh one')
        finally:
            account_census.fetch_balances = orig_fetch
            g._WALLET_UNITS_CACHE.update(units=None, at=0.0)

    def test_the_ttl_is_a_real_bound_not_a_constant_true(self):
        b = body_of('wallet_owned_units')
        self.assertIn('max_age_seconds', b)
        self.assertIn('_WALLET_UNITS_CACHE["at"]', b)


class TestItCannotBlockASell(unittest.TestCase):
    def test_the_gate_sits_in_the_buy_branch_only(self):
        src = SRC[SRC.index('def run_grid_branch') if 'def run_grid_branch' in SRC else 0:]
        i = SRC.index('"UNBACKED", _back_reason')
        before = SRC[:i]
        # The buy branch is the one guarded by the rung/trigger test.
        self.assertIn('len(tradeable_slices(slices)) < branch.num_levels', before)
        gate_open = before.rindex('len(tradeable_slices(slices)) < branch.num_levels')
        between = before[gate_open:]
        self.assertNotIn('def ', between,
                         'the check appears to have left the buy branch')

    def test_the_sell_path_never_consults_it(self):
        for fn in ('_pick_parked_slice_to_sell',):
            self.assertNotIn('branch_backing_verdict', body_of(fn))

    def test_the_refusal_is_guarded_by_the_real_verdict(self):
        """A SOURCE CHECK THAT THE CALL EXISTS IS NOT ENOUGH. Replacing
        `if not _back_ok:` with `if False:` leaves the call, the log line
        and the recorder all in place and the gate does nothing - the
        exact mutation that survived the first concentration-gate suite.
        So the guard's own condition is checked, not just its presence."""
        # The INNERMOST `if` spanning the marker line. The enclosing buy
        # branch spans it too, and its test is the rung/trigger BoolOp -
        # which would make this assert about the wrong node and pass or
        # fail for reasons unrelated to the guard. Matched on LINE SPANS,
        # not source segments: get_source_segment re-splits the whole
        # 9k-line file per node, which turned this one test into 39s.
        marker = next(i for i, ln in enumerate(SRC.split('\n'), 1)
                      if '"UNBACKED", _back_reason' in ln)
        hits = [n for n in ast.walk(TREE)
                if isinstance(n, ast.If)
                and n.lineno <= marker <= (n.end_lineno or n.lineno)]
        self.assertTrue(hits, 'no UNBACKED guard found in the buy path at all')
        for node in [min(hits, key=lambda n: (n.end_lineno or n.lineno) - n.lineno)]:
            t = node.test
            self.assertIsInstance(t, ast.UnaryOp,
                                  'the UNBACKED guard is not a negation any more')
            self.assertIsInstance(t.op, ast.Not)
            self.assertIsInstance(t.operand, ast.Name)
            self.assertEqual(t.operand.id, '_back_ok',
                             'the UNBACKED guard no longer tests the verdict')
            self.assertTrue(any(isinstance(n, ast.Return) for n in node.body),
                            'the UNBACKED branch does not return - the buy proceeds')
            return
        self.fail('no UNBACKED guard found in the buy path at all')

    def test_the_verdict_it_guards_on_is_the_one_it_computed(self):
        """`_back_ok` must come from the await, not be set to a constant
        somewhere above the guard."""
        self.assertIn('_back_ok, _back_reason = await branch_backing_verdict(', SRC)
        self.assertEqual(SRC.count('_back_ok ='), 0,
                         '_back_ok is assigned outside the await')

    def test_only_one_call_site(self):
        self.assertEqual(SRC.count('await branch_backing_verdict('), 1)


class TestItUsesTheSharedMeasurement(unittest.TestCase):
    def test_the_verdict_delegates_to_slice_backing(self):
        b = body_of('branch_backing_verdict')
        self.assertIn('slice_backing.assess(', b)

    def test_it_does_not_reimplement_the_percentage(self):
        b = body_of('branch_backing_verdict')
        for forbidden in ('/ claim', '* 100', 'BACKED_ENOUGH', '>= 50', '< 50'):
            self.assertNotIn(forbidden, b,
                             'the backed threshold belongs to slice_backing')

    def test_it_does_not_use_the_market_book(self):
        """census holdings drop unpriceable assets and roll dust into an
        unnamed count - the documented wrong input to a held-units question.

        Checked on the CODE, not the text: both docstrings say the words
        "NOT account_market_book()" on purpose, and a naive substring
        search matches that warning and passes for the wrong reason."""
        for fn in ('branch_backing_verdict', 'wallet_owned_units'):
            tree = ast.parse(body_of(fn))
            called = {
                (n.func.attr if isinstance(n.func, ast.Attribute) else
                 getattr(n.func, 'id', None))
                for n in ast.walk(tree) if isinstance(n, ast.Call)
            }
            self.assertNotIn('account_market_book', called,
                             fn + ' calls the market book for a units question')
            self.assertNotIn('census', called, fn + ' calls census()')

    def test_the_wallet_read_uses_the_shared_normalizer(self):
        self.assertIn('account_census.owned_units_map',
                      body_of('wallet_owned_units'))


class TestItCallsOnlyNamesThatExist(unittest.TestCase):
    """The concentration gate shipped twice with a method that did not
    exist (_last_known_equity, CapLedger.commit). Each would have thrown,
    been caught, and refused everything while looking like a working gate."""

    def test_account_census_names_are_real(self):
        import re
        for fn in ('wallet_owned_units',):
            for attr in set(re.findall(r'account_census\.(\w+)', body_of(fn))):
                self.assertTrue(hasattr(account_census, attr),
                                'account_census has no ' + attr)

    def test_slice_backing_names_are_real(self):
        import re
        for attr in set(re.findall(r'slice_backing\.(\w+)',
                                   body_of('branch_backing_verdict'))):
            self.assertTrue(hasattr(slice_backing, attr),
                            'slice_backing has no ' + attr)

    def test_assess_returns_the_keys_the_gate_reads(self):
        out = slice_backing.assess(
            [{"product_id": "LINK-USD", "slices": [{"qty": 9.34}],
              "current_price": 14.03, "total_unrealized_net_usd": 0.0}],
            {"LINK": 3.46})
        self.assertIn('unbacked', out)
        self.assertIn('unknown', out)
        row = out['unbacked'][0]
        for k in ('backed_pct', 'claimed_units', 'held_units', 'short_usd', 'asset'):
            self.assertIn(k, row)

    def test_the_gate_runs_end_to_end_without_an_exception(self):
        ok, why = run(g.branch_backing_verdict(
            "LINK-USD", LINK_SLICES, LINK_PRICE, {"LINK": 3.46}))
        self.assertIn(ok, (True, False))
        self.assertNotIn('error', str(why).lower())


class TestSliceQty(unittest.TestCase):
    def test_orm_row_and_dict_agree(self):
        class Row:
            qty, entry_price = 3.24, 14.074
        self.assertEqual(g.slice_qty(Row()),
                         g.slice_qty({"qty": 3.24, "entry_price": 14.074}))

    def test_junk_is_zero_not_an_exception(self):
        self.assertEqual(g.slice_qty({"qty": "nope"}), 0.0)
        self.assertEqual(g.slice_qty({}), 0.0)
        self.assertEqual(g.slice_qty(None), 0.0)

    def test_basis_is_unchanged_by_the_refactor(self):
        self.assertEqual(g.slice_basis_usd({"qty": 2.0, "entry_price": 3.0}), 6.0)
        self.assertEqual(g.slice_basis_usd({"qty": None, "entry_price": 3.0}), 0.0)
        self.assertTrue(g.slice_is_tradeable({"qty": 1, "entry_price": 2}))
        self.assertFalse(g.slice_is_tradeable({"qty": 1, "entry_price": 0.5}))


class TestItIsRecordedAndVisible(unittest.TestCase):
    def test_a_refusal_is_recorded_under_its_own_code(self):
        i = SRC.index('"UNBACKED", _back_reason')
        self.assertIn('_record_gate_decision', SRC[i - 200:i])

    def test_it_logs_loudly_enough_to_notice(self):
        i = SRC.index('🧾 UNBACKED')
        self.assertIn('log.warning', SRC[i - 80:i])


if __name__ == '__main__':
    unittest.main(verbosity=2)
