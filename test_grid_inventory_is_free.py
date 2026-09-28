"""$923.23 of grid inventory was locked behind resting stops, unnamed.

Closing the placement path (b050507) stopped NEW resting stops going on
grid coin. It did not cancel the ones already at the venue. At 09:44Z
the census read:

    asset          units      available    locked
    XLM      3011.20574764  1030.43934739  $419.30
    NEAR       40.57515480    24.62415480   $81.29
    LINK       14.14000000     7.51000000   $90.11
    SOL         1.23586526     0.45991521   $91.50
    ALGO     1134.34638900     0.04638900  $141.64
    ACH     18872.30459504  7127.00459504   $70.14
    JASMY    5862.75696592     0.75696592   $29.25

$923.23, six of them live grid branches. ALGO had 0.046 of 1134.35 units
free - the entire position was reserved by a resting sell.

Two costs, and neither was reported anywhere:

  1. The grid cannot sell what the venue has reserved. A branch whose
     slice comes good tries to sell units the exchange is holding for
     another order, and the sale fails or is cut short.
  2. If one fires it sells 75% of the position in a single order while
     the branch goes on tracking it slice by slice - which is how the
     coin shortfall grew from $473.28/5 branches to $510.72/7.

coin_tracked_is_held sees the SYMPTOM, after the coin is gone. This
names the CAUSE while it can still be undone: cancelling a resting sell
places no order and frees the units.

The check reads locked units, never infers them. A holding whose
available balance the venue did not return is UNKNOWN - a caller that
cannot tell "nothing locked" from "could not tell" is exactly the caller
that reports $0.00 locked on a fully reserved position.
"""
import invariants as inv


def h(asset, units, available, price):
    return {"asset": asset, "units": units, "available_units": available,
            "price": price, "locked_units": None if available is None else units - available}


LIVE = [h("XLM", 3011.20574764, 1030.43934739, 0.211675),
        h("NEAR", 40.5751548, 24.6241548, 5.098),
        h("LINK", 14.14, 7.51, 13.592),
        h("SOL", 1.23586526, 0.45991521, 117.84),
        h("ACH", 18872.30459504, 7127.00459504, 0.005965)]
TRACKED = {"XLM-USD": 2200.0, "NEAR-USD": 30.0, "LINK-USD": 10.0,
           "SOL-USD": 1.0, "ACH-USD": 12000.0}


def test_locked_grid_inventory_is_a_fail_not_a_silence():
    v = inv.grid_inventory_is_free(TRACKED, LIVE)
    assert v["status"] == inv.FAIL
    assert v["locked_usd"] > 700
    assert "XLM" in v["detail"]


def test_it_names_every_branch_whose_coin_is_reserved():
    v = inv.grid_inventory_is_free(TRACKED, LIVE)
    assert sorted(v["locked_positions"], key=lambda r: r["product_id"]) == sorted(
        v["locked_positions"], key=lambda r: r["product_id"])
    assert {r["product_id"] for r in v["locked_positions"]} == set(TRACKED)


def test_a_holding_the_grid_does_not_track_is_not_this_checks_business():
    """JASMY was locked too, and is not a grid branch. Reporting it here
    would inflate the figure the owner would act on."""
    rows = LIVE + [h("JASMY", 5862.75696592, 0.75696592, 0.004989)]
    v = inv.grid_inventory_is_free(TRACKED, rows)
    assert "JASMY" not in v["detail"]
    assert all(r["product_id"] != "JASMY-USD" for r in v["locked_positions"])


def test_nothing_locked_passes():
    free = [h("XLM", 3011.2, 3011.2, 0.211675), h("SOL", 1.2, 1.2, 117.84)]
    v = inv.grid_inventory_is_free({"XLM-USD": 2200.0, "SOL-USD": 1.0}, free)
    assert v["status"] == inv.OK


def test_an_unreadable_available_balance_is_unknown_never_zero_locked():
    """The loudest false reassurance available: a fully reserved position
    reported as nothing locked because the venue did not say."""
    blind = [{"asset": "XLM", "units": 3011.2, "available_units": None, "price": 0.211675}]
    v = inv.grid_inventory_is_free({"XLM-USD": 2200.0}, blind)
    assert v["status"] == inv.UNKNOWN
    assert "XLM" in v["detail"]


def test_an_unreadable_holding_list_is_unknown_not_a_pass():
    v = inv.grid_inventory_is_free({"XLM-USD": 2200.0}, None)
    assert v["status"] == inv.UNKNOWN


def test_no_tracked_positions_is_unknown_not_a_pass():
    v = inv.grid_inventory_is_free({}, LIVE)
    assert v["status"] == inv.UNKNOWN


def test_one_locked_coin_among_free_ones_still_fails():
    rows = [h("XLM", 3011.2, 3011.2, 0.211675),
            h("ALGO", 1134.34638900, 0.04638900, 0.125905)]
    v = inv.grid_inventory_is_free({"XLM-USD": 2200.0, "ALGO-USD": 1000.0}, rows)
    assert v["status"] == inv.FAIL
    assert "ALGO" in v["detail"] and "XLM" not in v["detail"]


def test_dust_sized_locks_do_not_raise_an_alarm():
    """A few units reserved by a partially-filled order is not a finding."""
    rows = [h("XLM", 3011.2, 3011.19, 0.211675)]
    v = inv.grid_inventory_is_free({"XLM-USD": 2200.0}, rows)
    assert v["status"] == inv.OK


def test_the_detail_says_cancelling_frees_it_rather_than_implying_a_sale():
    v = inv.grid_inventory_is_free(TRACKED, LIVE)
    d = v["detail"].lower()
    assert "cancel" in d
    assert "places no order" in d or "no order" in d
