"""A slice too small to sell is not a rung.

Against the real live numbers: BCH-USD held 0.00000022 BCH ($0.00007) and
LINK-USD 0.01 LINK ($0.14), each filling the third of three rungs and
locking its branch out of both buying and selling.
"""
import os
import unittest
from types import SimpleNamespace as NS

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import crypto_grid_bot as G


def S(qty, entry, adopted=False, fee=0.0035):
    return NS(qty=qty, entry_price=entry, adopted=adopted, entry_fee_rate=fee)


class TestDustDetection(unittest.TestCase):
    def test_the_real_bch_remnant_is_not_tradeable(self):
        self.assertFalse(G.slice_is_tradeable(S(2.2e-07, 309.32)))
        self.assertAlmostEqual(G.slice_basis_usd(S(2.2e-07, 309.32)), 0.000068, places=5)

    def test_the_real_link_remnant_is_not_tradeable(self):
        self.assertFalse(G.slice_is_tradeable(S(0.009999999999998899, 14.343)))
        self.assertAlmostEqual(G.slice_basis_usd(S(0.01, 14.343)), 0.14343, places=4)

    def test_the_real_bch_positions_are_tradeable(self):
        self.assertTrue(G.slice_is_tradeable(S(0.12644109, 339.58)))   # $42.94
        self.assertTrue(G.slice_is_tradeable(S(0.18181268, 330.78)))   # $60.14

    def test_the_smallest_rung_the_engine_buys_is_never_dust(self):
        """MIN_TRADE_USD is $5.00, five times the dust floor, so no slice
        this engine created can be mistaken for a remnant."""
        self.assertGreater(G.MIN_TRADE_USD, G.GRID_DUST_SLICE_USD)
        self.assertTrue(G.slice_is_tradeable(S(1.0, G.MIN_TRADE_USD)))

    def test_it_reads_a_dict_as_well_as_a_row(self):
        self.assertFalse(G.slice_is_tradeable({"qty": 2.2e-07, "entry_price": 309.32}))
        self.assertTrue(G.slice_is_tradeable({"qty": 1.0, "entry_price": 50.0}))

    def test_missing_fields_read_as_zero_and_are_not_tradeable(self):
        self.assertFalse(G.slice_is_tradeable(NS(qty=None, entry_price=None)))
        self.assertFalse(G.slice_is_tradeable({}))

    def test_a_small_but_real_position_still_fills_a_rung(self):
        """Erring low is the safe direction: anything above the floor keeps
        blocking a rung exactly as before."""
        self.assertTrue(G.slice_is_tradeable(S(1.0, 1.01)))


class TestRungCounting(unittest.TestCase):
    def test_bch_frees_a_rung_once_the_remnant_is_discounted(self):
        bch = [S(0.12644109, 339.58, adopted=True), S(0.18181268, 330.78),
               S(2.2e-07, 309.32)]
        self.assertEqual(len(bch), 3)
        self.assertEqual(len(G.tradeable_slices(bch)), 2)
        self.assertTrue(len(bch) >= 3, "was parked on the raw count")
        self.assertFalse(len(G.tradeable_slices(bch)) >= 3, "now has a rung")

    def test_link_frees_a_rung_too(self):
        link = [S(0.01, 14.343), S(2.99, 15.212), S(3.1, 14.651)]
        self.assertEqual(len(G.tradeable_slices(link)), 2)

    def test_a_branch_full_of_real_slices_stays_parked(self):
        """The change must not hand a genuinely full branch a free rung."""
        xrp = [S(100.0, 0.934)] * 7
        self.assertEqual(len(G.tradeable_slices(xrp)), 7)
        self.assertTrue(len(G.tradeable_slices(xrp)) >= 3)

    def test_a_flat_branch_is_still_flat(self):
        self.assertEqual(G.tradeable_slices([]), [])

    def test_a_branch_of_nothing_but_dust_reads_as_having_no_rungs_filled(self):
        self.assertEqual(len(G.tradeable_slices([S(1e-07, 1.0)] * 3)), 0)


class TestEscapePicker(unittest.TestCase):
    """_pick_parked_slice_to_sell must never offer a remnant as the way out."""

    def test_it_no_longer_picks_the_dust_over_real_slices(self):
        # The real BCH shape at price 317.55: dust is +1.80%, both real
        # slices are negative. Percentage alone picked the dust.
        slices = [S(0.12644109, 339.58), S(0.18181268, 330.78),
                  S(2.2e-07, 309.32)]
        got, pct = G._pick_parked_slice_to_sell(slices, 317.55, 0.015, 0.0075,
                                                0.010)
        self.assertIsNone(got, f"offered a slice netting {pct}")

    def test_a_real_qualifying_slice_is_still_offered(self):
        slices = [S(1.0, 100.0), S(1.0, 50.0)]
        got, pct = G._pick_parked_slice_to_sell(slices, 110.0, 0.015, 0.0075,
                                                0.010)
        self.assertIsNotNone(got)
        self.assertEqual(got.entry_price, 50.0, "should pick the best, not the first")
        self.assertGreater(pct, 0.010)

    def test_dust_does_not_mask_a_real_qualifying_slice(self):
        """The bug's worst form: dust wins on percentage and the real
        sellable slice never gets offered."""
        slices = [S(1.0, 50.0), S(2.2e-07, 1.0)]
        got, _ = G._pick_parked_slice_to_sell(slices, 110.0, 0.015, 0.0075,
                                              0.010)
        self.assertIsNotNone(got)
        self.assertEqual(got.entry_price, 50.0)

    def test_the_profit_floor_is_unchanged(self):
        """Nothing here loosens the floor - a real slice under it is still
        refused."""
        slices = [S(1.0, 100.0)]
        got, _ = G._pick_parked_slice_to_sell(slices, 100.5, 0.015, 0.0075,
                                              0.010)
        self.assertIsNone(got, "0.5% gross is under the 1.0% net floor")


if __name__ == "__main__":
    unittest.main(verbosity=2)
