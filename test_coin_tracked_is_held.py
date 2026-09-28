"""A branch may not claim coin the wallet does not hold.

Live, 2026-09-28 06:19Z. A resting stop-loss fired on ETH-USD and sold
0.347873 units - its locked balance went 0.3479 to 0.0000. Most of that
was ETH no branch tracked, but 0.031600 of it belonged to the grid's own
open slices:

    ETH-USD branch crypto_grid_8
      4 slices, tracked 0.162240
      wallet            0.130640
      SHORTFALL         0.031600   (~$83.74)

The next sale of its oldest slice would have been an order for coin that
does not exist. Nothing said a word, because every other check in this
module compares DOLLARS in aggregate and the fleet-wide totals still
added up.
"""
import invariants as inv

TRACKED = {"ETH-USD": 0.162240, "ZEC-USD": 1.331766, "XRP-USD": 1417.099564}
WALLET = {"ETH": 0.130640, "ZEC": 1.331766, "XRP": 1417.099564}
PRICES = {"ETH-USD": 2650.0, "ZEC-USD": 1549.44, "XRP-USD": 1.4846}


def test_the_live_eth_shortfall_is_caught():
    r = inv.coin_tracked_is_held(TRACKED, WALLET, PRICES)
    assert r["status"] == inv.FAIL
    assert r["short_usd"] == 83.74
    assert r["short_positions"][0]["product_id"] == "ETH-USD"
    assert "order for units that do not exist" in r["detail"]


def test_a_fully_held_fleet_passes():
    whole = dict(WALLET, ETH=0.162240)
    assert inv.coin_tracked_is_held(TRACKED, whole, PRICES)["status"] == inv.OK


def test_more_coin_than_tracked_is_not_a_failure():
    """Untracked coin is a separate finding - the account holds plenty of
    it - and it is not a shortfall."""
    plenty = dict(WALLET, ETH=5.0, ZEC=9.0)
    assert inv.coin_tracked_is_held(TRACKED, plenty, PRICES)["status"] == inv.OK


def test_exchange_dust_is_inside_the_tolerance():
    dusty = dict(WALLET, ETH=0.162240 * 0.999)
    assert inv.coin_tracked_is_held(TRACKED, dusty, PRICES)["status"] == inv.OK


def test_a_shortfall_past_the_tolerance_is_not():
    real = dict(WALLET, ETH=0.162240 * 0.99)
    assert inv.coin_tracked_is_held(TRACKED, real, PRICES)["status"] == inv.FAIL


def test_a_coin_missing_from_the_wallet_reading_is_unknown_not_zero():
    """Reporting a full position as a total shortfall would be the
    loudest false alarm this module could raise."""
    partial = {"ZEC": 1.331766, "XRP": 1417.099564}
    r = inv.coin_tracked_is_held(TRACKED, partial, PRICES)
    assert r["status"] == inv.UNKNOWN
    assert r["unreadable"] == ["ETH-USD"]


def test_an_unknown_coin_never_hides_a_real_shortfall():
    tracked = dict(TRACKED, LINK_USD=None)
    r = inv.coin_tracked_is_held({"ETH-USD": 0.162240, "LINK-USD": 7.0},
                                 {"ETH": 0.130640}, PRICES)
    assert r["status"] == inv.FAIL
    assert r["unreadable"] == ["LINK-USD"]


def test_an_unreadable_wallet_is_unknown_never_a_pass():
    r = inv.coin_tracked_is_held(TRACKED, None, PRICES)
    assert r["status"] == inv.UNKNOWN
    assert "Not knowing is not the same as being fine" in r["detail"]


def test_a_missing_price_still_reports_the_units():
    r = inv.coin_tracked_is_held(TRACKED, WALLET, {})
    assert r["status"] == inv.FAIL
    assert r["short_positions"][0]["short_usd"] is None
    assert "0.162240" in r["detail"]


def test_it_is_keyed_per_coin_not_per_branch():
    """Two branches can share a coin and the wallet holds one pool - a
    per-branch check would compare each against the whole balance and
    miss the case where together they overdraw it."""
    r = inv.coin_tracked_is_held({"ETH-USD": 0.20}, {"ETH": 0.130640}, PRICES)
    assert r["status"] == inv.FAIL


def test_no_tracked_positions_is_unknown_not_a_clean_bill():
    assert inv.coin_tracked_is_held({}, WALLET, PRICES)["status"] == inv.UNKNOWN
    assert inv.coin_tracked_is_held(None, WALLET, PRICES)["status"] == inv.UNKNOWN
