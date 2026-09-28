"""A top-up must never register coin the branch already tracks.

Every branch that ended up claiming coin the wallet does not hold is an
ADOPTED branch that was later topped up:

    PEPE  adopted 20.23M at 09-27 12:17, bought 9.10M at 09-28 02:58,
          topped up 18.05M at 09-28 03:08 -> 47.39M tracked, 39.09M held
    BCH   3 adopted slices + 1 real buy         -> 0.379323 units short
    ZEC, XRP  same shape, and the trimmer took more on top

coin_topup decided how much of a coin was still free with

    units_under_branch = allocated_usd / price

- a cost-basis DOLLAR figure divided by TODAY's price. Those are different
quantities. When the price has risen since the slices were written the
quotient understates the units already tracked, and the difference is
handed back as "free" and registered a second time.

The branch's own slices are the exact answer and were already in the
payload the function receives.
"""
import coin_topup


HELD_UNITS = 39_093_467.0
PRICE_NOW = 0.00000487
ENTRY = 0.00000443


def branch(tracked_qty, allocated_usd, with_slices=True):
    b = {"product_id": "PEPE-USD", "bot_name": "g18", "active": True,
         "allocated_usd": allocated_usd, "num_levels": 3,
         "stop_loss_pct_override": 0.0}
    if with_slices:
        b["slices"] = [{"qty": tracked_qty, "entry_price": ENTRY}]
    return [b]


def held(units=HELD_UNITS, price=PRICE_NOW):
    return [{"asset": "PEPE", "usd": round(units * price, 2), "price": price,
             "units": units, "available_units": units}]


def added_units(out):
    return sum(s["qty"] for t in (out.get("topups") or []) for s in t["slices"])


def test_a_top_up_never_takes_tracked_units_past_held_units():
    """The whole point. 20.23M tracked against 39.09M held leaves 18.86M
    free, and not one unit more."""
    tracked = 20_232_505.0
    out = coin_topup.plan(held(), branch(tracked, 89.62), account_total_usd=10_000.0)
    assert added_units(out) + tracked <= HELD_UNITS + 1.0


def test_the_estimate_it_replaced_would_have_over_tracked():
    """Kept as the control: allocated_usd / price, on these same numbers,
    calls 18.4M of already-tracked coin free."""
    tracked = 20_232_505.0
    old_estimate = 89.62 / PRICE_NOW
    assert old_estimate < tracked, "the estimate understates the tracked units"
    over = tracked - old_estimate
    assert over > 1_000_000, f"the old path would free {over:,.0f} tracked units"


def test_a_branch_already_holding_everything_is_refused():
    """allocated_usd of 100 leaves $90.38 idle by the DOLLAR rule, so the
    refusal has to come from the units check - 38M of 39.09M tracked
    leaves 1.09M free, about $5, under the $60 a slice needs. Picked so
    the earlier NO_IDLE_COIN rule cannot be what passes this test."""
    out = coin_topup.plan(held(), branch(38_000_000.0, 100.0), account_total_usd=10_000.0)
    assert not out.get("topups")
    assert out["refusals"][0]["reason"] == "TOO_LITTLE_AVAILABLE_TO_TRADE"


def test_a_branch_tracking_more_than_is_held_adds_nothing():
    """The state the fleet is actually in right now - it must not deepen."""
    out = coin_topup.plan(held(), branch(47_389_839.0, 205.14), account_total_usd=10_000.0)
    assert not out.get("topups")


def test_a_genuinely_untracked_holding_is_still_topped_up():
    """The fix must not disarm the feature: a branch with almost nothing
    tracked still gets the idle coin."""
    out = coin_topup.plan(held(), branch(100.0, 1.0), account_total_usd=10_000.0)
    assert added_units(out) > 1_000_000


def test_a_payload_without_slices_falls_back_to_the_old_estimate():
    """Never treats every held unit as free just because slices are
    missing from a payload."""
    out = coin_topup.plan(held(), branch(0, 89.62, with_slices=False),
                          account_total_usd=10_000.0)
    added = added_units(out)
    assert added <= HELD_UNITS - (89.62 / PRICE_NOW) + 1.0


def test_it_uses_units_not_dollars_for_the_subtraction():
    """Asserted on the source: the docstrings here quote the old
    expression verbatim while explaining it, so a substring search over
    the module would match them."""
    import ast
    import pathlib
    src = pathlib.Path(coin_topup.__file__).read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "plan")
    body = ast.get_source_segment(src, fn)
    assert 'r.get("tracked_units")' in body
    # The fallback may still divide; the primary path must not.
    primary = body.split("if tracked_units:")[1].split("else:")[0]
    assert "/" not in primary
