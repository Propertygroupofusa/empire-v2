"""The LOCKED badge asserts the coin does not EXIST. Prove it is now read
from owned units, not from available ones.

Measured live on 2026-10-04, the fleet dashboard showed four branches -
SOL, ALGO, LINK, ACH, $541.37 between them - under a red badge reading
"cannot sell: the ledger claims coin the wallet does not hold". Every one
of them was owned in full. The coin was sitting under the fleet's OWN
resting sell orders (and, for SOL, staked), which is precisely the state
available_units subtracts and held_including_zero does not.

These tests pin the distinction in three places: the arithmetic, the
endpoint that publishes it, and the page that renders it.
"""
import ast
import inspect

import account_census
import slice_backing as sb


def _branch(pid, qty, price, unreal=0.0):
    return {"product_id": pid, "current_price": price,
            "total_unrealized_net_usd": unreal,
            "slices": [{"qty": qty, "entry_price": price}]}


# --- the four real branches, with the real wallet numbers ------------------
# claimed units, owned units, price. Owned figures are the live reading that
# retracted the "$245 is missing" finding; ALGO owns nearly 3x its claim.
LIVE = [
    ("ACH-USD",  5345.200000, 5345.204595, 0.016895),
    ("ALGO-USD",  492.700000, 1347.646389, 0.12958),
    ("LINK-USD",    9.340000,   10.090000, 14.01),
    ("SOL-USD",     1.034600,    1.034600, 120.86),
]


def test_against_owned_units_not_one_of_them_is_unbacked():
    branches = [_branch(pid, claim, price) for pid, claim, _own, price in LIVE]
    owned = {pid.split("-")[0]: own for pid, _c, own, _p in LIVE}
    out = sb.assess(branches, owned)
    assert out["unbacked"] == [], out["unbacked"]
    assert out["not_in_wallet_usd"] == 0.0, out["not_in_wallet_usd"]
    for row in out["rows"]:
        assert row["backed"] is True, row


def test_against_available_units_all_four_are_falsely_unbacked():
    """The bug, reproduced. Coin resting under the fleet's own sell orders
    reads as zero available - and every branch is then accused of claiming
    coin it owns outright."""
    branches = [_branch(pid, claim, price) for pid, claim, _own, price in LIVE]
    on_hold_so_nothing_is_available = {pid.split("-")[0]: 0.0
                                       for pid, _c, _o, _p in LIVE}
    out = sb.assess(branches, on_hold_so_nothing_is_available)
    assert len(out["unbacked"]) == 4, out["unbacked"]
    # $410.04 is the claimed SLICE VALUE of the four branches at these
    # prices. It is deliberately not the dashboard's $541.37, which is the
    # branches' allocated_usd - a different measure, and conflating the two
    # is how a shortfall gets quoted at the wrong size.
    assert out["not_in_wallet_usd"] == 410.04, out["not_in_wallet_usd"]


def test_the_two_maps_really_do_disagree():
    """owned_units_map and the dashboard's available map are not the same
    reading - if they ever collapse into one, this whole fix is a no-op."""
    balances = {
        "available": True,
        "available_units": {"ALGO": 0.0},
        "held_including_zero": {"ALGO": 1347.646389},
    }
    owned = account_census.owned_units_map(balances)
    avail = account_census.available_units_map(balances)
    assert owned["ALGO"] == 1347.646389
    assert avail["ALGO"] == 0.0
    assert owned["ALGO"] != avail["ALGO"]


def test_an_unreadable_census_yields_no_owned_map():
    """A gap is not a clean bill of health. None here is what makes the
    endpoint omit the key, which is what makes the page claim nothing."""
    assert account_census.owned_units_map(None) is None
    assert account_census.owned_units_map({"available": False}) is None


# --- the wiring, so this cannot quietly regress ---------------------------

def test_grid_status_publishes_the_owned_measurement():
    import routers.trading_dashboard as td
    src = inspect.getsource(td)
    assert 'data["backing_owned"] = slice_backing.assess(' in src, (
        "the endpoint no longer publishes an owned-units backing report")
    assert "account_census.owned_units_map(_bal)" in src, (
        "backing_owned is no longer built from owned units")
    # The available-based report must SURVIVE: it is the right answer to
    # "can this branch sell this second", which is a real question.
    assert 'data["backing"] = slice_backing.assess(' in src


def test_the_readiness_badge_reads_owned_and_never_falls_back():
    page = open("family_tree_dashboard.html").read()
    line = [l for l in page.splitlines()
            if "const unbacked = " in l and ".unbacked" in l]
    assert len(line) == 1, line
    assert "d.backing_owned" in line[0], line[0]
    # Falling back to d.backing on an unreadable census would reinstate the
    # exact false accusation this change removes.
    assert "d.backing ||" not in line[0], line[0]


def test_the_python_change_is_syntactically_whole():
    ast.parse(open("routers/trading_dashboard.py").read())
