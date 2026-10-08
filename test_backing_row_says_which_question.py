"""A backing row must answer for itself which question it answers.

THE REPORT THAT WAS WRONG, 2026-10-08. A fleet summary listed
"ALGO 279.353611" among the shortfalls. ALGO-USD claims 1150.9 units and
OWNS 2005.846389 - 174.285% - and reads short only in the AVAILABLE
block, because 1134.3 of its units were sitting under the fleet's own
resting sell order. The coin was not missing. It was working.

The block said so correctly in its top-level `note`. The ROW did not,
and the row is what got quoted. That is the identical owned-versus-
available confusion that forced the retraction of "$245.23 of claimed
coin is not in the wallet" on 2026-10-04, resurfacing one level down.

So every row now carries its own basis, its own question, and - when it
is the available reading - an explicit statement that its shortfall is
not evidence the coin is missing. A row lifted out of its block cannot
be read as the other block's answer.
"""
import unittest

import slice_backing as sb

ALGO = [{
    "product_id": "ALGO-USD",
    "slices": [{"qty": 1150.9}],
    "current_price": 0.12216,
    "total_unrealized_net_usd": 0.0,
}]
ALGO_AVAILABLE = {"ALGO": 871.546389}      # under a resting sell
ALGO_OWNED = {"ALGO": 2005.8463889999998}  # what the account holds


class TestTheAlgoRowCannotBeMisread(unittest.TestCase):

    def test_the_available_row_says_it_is_not_an_existence_claim(self):
        row = sb.assess(ALGO, ALGO_AVAILABLE)["rows"][0]
        self.assertEqual(row["units_are"], "available")
        self.assertIn("sell RIGHT NOW", row["question"])
        self.assertIn("NOT evidence the coin is missing", row["shortfall_is"])
        self.assertGreater(row["short_units"], 0,
                           'the available reading should still show the gap')

    def test_the_owned_row_says_algo_is_not_short_at_all(self):
        out = sb.assess(ALGO, ALGO_OWNED, units_are="owned")
        row = out["rows"][0]
        self.assertEqual(row["units_are"], "owned")
        self.assertIn("EXIST", row["question"])
        self.assertEqual(row["short_units"], 0.0,
                         'ALGO owns more than it claims and must read short 0')
        self.assertTrue(row["backed"])
        self.assertEqual(out["unbacked"], [])

    def test_the_two_readings_disagree_and_each_says_why(self):
        avail = sb.assess(ALGO, ALGO_AVAILABLE)["rows"][0]
        owned = sb.assess(ALGO, ALGO_OWNED, units_are="owned")["rows"][0]
        self.assertNotEqual(avail["short_units"], owned["short_units"])
        self.assertNotEqual(avail["units_are"], owned["units_are"])
        self.assertNotEqual(avail["question"], owned["question"])

    def test_a_real_shortfall_still_reads_as_one_on_the_owned_basis(self):
        """The guard against over-correcting: LINK really is short."""
        link = [{"product_id": "LINK-USD", "slices": [{"qty": 11.41}],
                 "current_price": 12.902, "total_unrealized_net_usd": 0.0}]
        row = sb.assess(link, {"LINK": 5.53}, units_are="owned")["rows"][0]
        self.assertEqual(row["units_are"], "owned")
        self.assertAlmostEqual(row["short_units"], 5.88, places=6)
        self.assertIn("genuinely not in the account", row["shortfall_is"])

    def test_unknown_rows_carry_the_basis_too(self):
        out = sb.assess(ALGO, {"BTC": 1.0}, units_are="owned")
        unk = out["unknown"][0]
        self.assertEqual(unk["units_are"], "owned")
        self.assertIn("EXIST", unk["question"])
        self.assertTrue(unk["this_is_unknown_not_unbacked"])

    def test_every_row_carries_a_basis_on_both_readings(self):
        for units, basis in ((ALGO_AVAILABLE, "available"),
                             (ALGO_OWNED, "owned")):
            out = sb.assess(ALGO, units, units_are=basis)
            for row in out["rows"]:
                self.assertEqual(row["units_are"], basis)
                self.assertTrue(row["question"])
                self.assertTrue(row["shortfall_is"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
