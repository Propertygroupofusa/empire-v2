"""An unreadable balance must not erase a shortfall just measured.

WHAT THIS EXISTS TO CATCH, measured live 2026-10-08. Between 12:25:03Z
and 12:43:49Z the buy gate refused crypto_grid_16 (LINK-USD) twenty-four
times in a row, one refusal per cycle, on a confirmed shortfall: the
books claimed 10.36 units and the wallet owned 4.48. At 12:44:58Z the
branch bought 1.05 more LINK anyway, taking the claim to 11.41 against
5.53 owned.

Nothing about the shortfall had changed. A single balance read failed,
`wallet_owned_units` returned None as it is designed to, and the gate's
UNKNOWN doctrine passed the buy. The wallet reading is cached for
WALLET_UNITS_TTL_SECONDS, so one rate-limited read is one open cycle,
and one open cycle is one buy into a branch that cannot exit what it
already holds.

THE DOCTRINE IS NARROWED, NOT REVERSED, and the second test here is the
one that matters most: a branch nobody has ever measured short still
buys straight through an unreadable window. Refusing those would freeze
the fleet on a rate limit, which is the failure the doctrine was written
for and which this change must not reintroduce.
"""
import asyncio
import time
import unittest

import crypto_grid_bot as g

LINK_PRICE = 12.96
# The real branch: ten slices, 10.36 units claimed, 4.48 owned.
LINK_SLICES = [{"qty": 1.036, "entry_price": 13.1} for _ in range(10)]
LINK_OWNED_SHORT = {"LINK": 4.48}
LINK_OWNED_WHOLE = {"LINK": 10.36}


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def verdict(product="LINK-USD", slices=None, price=LINK_PRICE, units=None):
    return run(g.branch_backing_verdict(
        product, LINK_SLICES if slices is None else slices, price, units))


class BackingMemoryCase(unittest.TestCase):
    """The memory is module state, so every test starts and ends empty."""

    def setUp(self):
        g._CONFIRMED_SHORT.clear()

    def tearDown(self):
        g._CONFIRMED_SHORT.clear()


class TestAConfirmedShortfallSurvivesAnUnreadableBalance(BackingMemoryCase):

    def test_the_live_2026_10_08_sequence(self):
        """Refused on a readable map, then the map goes unreadable."""
        ok, _why = verdict(units=LINK_OWNED_SHORT)
        self.assertFalse(ok, 'the readable shortfall was not refused at all')

        ok2, why2 = verdict(units=None)
        self.assertFalse(
            ok2, 'an unreadable balance let the buy through on a branch '
                 'measured short seconds earlier - the 12:44:58Z buy')
        self.assertIn("unreadable", why2)
        self.assertIn("measured short", why2)

    def test_the_reason_still_names_the_owners_remedy(self):
        verdict(units=LINK_OWNED_SHORT)
        _ok, why = verdict(units=None)
        self.assertIn("reconcile-slices", why)
        self.assertNotIn("sell", why.lower().replace("a sell sizes", ""))

    def test_twenty_four_refusals_then_one_unreadable_cycle(self):
        for _ in range(24):
            self.assertFalse(verdict(units=LINK_OWNED_SHORT)[0])
        self.assertFalse(verdict(units=None)[0])


class TestAnUnmeasuredBranchStillBuys(BackingMemoryCase):
    """The doctrine this change must not break: a rate limit cannot
    freeze a branch nobody has measured short."""

    def test_none_units_with_an_empty_memory_does_not_refuse(self):
        ok, why = verdict(units=None)
        self.assertTrue(ok, 'an unreadable balance refused a branch with no '
                            'confirmed shortfall - this freezes the fleet')
        self.assertIn("unreadable", why)

    def test_the_memory_is_per_product(self):
        """A shortfall on one coin must not refuse a different coin."""
        self.assertFalse(verdict(units=LINK_OWNED_SHORT)[0])
        ok, _why = run(g.branch_backing_verdict(
            "SOL-USD", [{"qty": 1.0, "entry_price": 100.0}], 100.0, None))
        self.assertTrue(ok, 'LINK being short refused a buy on SOL')

    def test_a_branch_whose_coin_is_merely_locked_never_enters_the_memory(self):
        """THE FOUR-HOUR FALSE-REFUSAL GUARD. The units map is OWNED, so
        coin sitting under the fleet's own resting order is present. Such
        a branch must read clean and must not be remembered as short."""
        ok, why = verdict(units=LINK_OWNED_WHOLE)
        self.assertTrue(ok)
        self.assertEqual(why, "backed")
        self.assertNotIn("LINK-USD", g._CONFIRMED_SHORT)
        self.assertTrue(verdict(units=None)[0],
                        'a fully-owned branch was remembered as short')


class TestTheMemoryClears(BackingMemoryCase):

    def test_a_clean_reading_clears_it_on_the_next_cycle(self):
        """A reconcile must un-stick the branch immediately, not after
        the TTL."""
        self.assertFalse(verdict(units=LINK_OWNED_SHORT)[0])
        self.assertIn("LINK-USD", g._CONFIRMED_SHORT)

        ok, why = verdict(units=LINK_OWNED_WHOLE)
        self.assertTrue(ok)
        self.assertEqual(why, "backed")
        self.assertNotIn("LINK-USD", g._CONFIRMED_SHORT,
                         'a clean reading did not clear the remembered '
                         'shortfall - a reconciled branch stays frozen')
        self.assertTrue(verdict(units=None)[0])

    def test_it_expires_after_the_ttl(self):
        """A remembered shortfall must never outlive the books it was
        measured against."""
        self.assertFalse(verdict(units=LINK_OWNED_SHORT)[0])
        stamp, why = g._CONFIRMED_SHORT["LINK-USD"]
        g._CONFIRMED_SHORT["LINK-USD"] = (
            stamp - g.CONFIRMED_SHORT_TTL_SECONDS - 1.0, why)

        ok, _why = verdict(units=None)
        self.assertTrue(ok, 'an expired shortfall still refused the buy')
        self.assertNotIn("LINK-USD", g._CONFIRMED_SHORT)

    def test_the_ttl_is_a_real_bound(self):
        self.assertGreater(g.CONFIRMED_SHORT_TTL_SECONDS, 0)
        self.assertLessEqual(g.CONFIRMED_SHORT_TTL_SECONDS, 24 * 3600)


class TestTheAssetAbsentPath(BackingMemoryCase):
    """A readable map that simply does not name the asset is the other
    UNKNOWN, and it gets the same rule."""

    def test_absent_after_a_confirmed_shortfall_is_refused(self):
        self.assertFalse(verdict(units=LINK_OWNED_SHORT)[0])
        ok, why = verdict(units={"BTC": 1.0})
        self.assertFalse(ok, 'the asset dropping out of a readable map let '
                             'the buy through on a branch measured short')
        self.assertIn("absent", why)

    def test_absent_with_an_empty_memory_does_not_refuse(self):
        ok, why = verdict(units={"BTC": 1.0})
        self.assertTrue(ok)
        self.assertIn("unknown", why.lower())


class TestItStillRefusesWhatItAlwaysDid(BackingMemoryCase):
    """Regression cover: the readable-map behaviour is untouched."""

    def test_a_confirmed_zero_is_still_refused(self):
        ok, _why = run(g.branch_backing_verdict(
            "ZEC-USD", [{"qty": 0.378166652152, "entry_price": 1600.0}],
            1219.48, {"ZEC": 0.0}))
        self.assertFalse(ok)

    def test_a_fully_backed_branch_is_still_backed(self):
        ok, why = run(g.branch_backing_verdict(
            "BTC-USD", [{"qty": 0.001, "entry_price": 80000.0}], 82000.0,
            {"BTC": 0.01}))
        self.assertTrue(ok)
        self.assertEqual(why, "backed")

    def test_the_threshold_still_belongs_to_slice_backing(self):
        """This gate must not grow its own percentage. The boundary case
        from test_backing_gate.py, re-asserted here so a change to the
        memory cannot quietly move it."""
        self.assertTrue(run(g.branch_backing_verdict(
            "X-USD", [{"qty": 2.0, "entry_price": 100.0}], 100.0,
            {"X": 1.0}))[0])
        self.assertFalse(run(g.branch_backing_verdict(
            "X-USD", [{"qty": 2.0, "entry_price": 100.0}], 100.0,
            {"X": 0.99}))[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
