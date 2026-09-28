"""The split that was computed by hand on every review pass.

A hand computation sitting beside a check is exactly how two numbers
that must agree stop agreeing - the failure this codebase produces
over and over. So this module applies the SAME full/has-room predicate
no_dead_capital applies, to the SAME branch payload, and these tests
pin that together rather than trusting it.

Fixture is the live book of 2026-09-28 13:55Z, where the hand figure
was 18.5x: $37.90 on $4,486.63 against $1.65 on $3,611.79.
"""
import capital_productivity as cp
import invariants as inv


def br(pid, usd, n, lv, unreal=0.0):
    return {"product_id": pid, "allocated_usd": usd, "open_slices": n,
            "num_levels": lv, "total_unrealized_net_usd": unreal}


def tr(pid, pnl):
    return {"product_id": pid, "pnl": pnl}


LIVE = [
    br("ZEC-USD", 2272.62, 7, 3, -91.55), br("ETH-USD", 400.00, 4, 3, -5.36),
    br("SHIB-USD", 267.37, 7, 3, -12.83), br("BCH-USD", 171.75, 4, 3, -18.09),
    br("PEPE-USD", 166.54, 7, 3, -3.63),  br("SOL-USD", 128.48, 4, 3, -5.52),
    br("ACH-USD", 91.16, 3, 3, -4.82),    br("ONDO-USD", 58.31, 3, 3, -3.20),
    br("BTC-USD", 55.56, 3, 3, -0.31),
    br("XRP-USD", 2240.84, 9, 10, -18.96), br("XLM-USD", 695.31, 1, 3, 2.30),
    br("TON-USD", 242.52, 1, 3, -1.09),    br("APE-USD", 240.38, 2, 3, -1.02),
    br("ALGO-USD", 180.57, 1, 3, 3.83),    br("NEAR-USD", 177.76, 2, 3, -3.43),
    br("HBAR-USD", 159.65, 0, 3, 0.0),     br("QNT-USD", 149.91, 2, 3, 47.25),
    br("LINK-USD", 129.23, 2, 3, 0.65),    br("LTC-USD", 77.62, 3, 4, -1.97),
    br("TIA-USD", 58.22, 2, 3, -2.05),     br("FLOKI-USD", 54.21, 2, 3, -0.82),
    br("JASMY-USD", 53.56, 0, 3, 0.0),     br("PRIME-USD", 26.85, 2, 3, -0.23),
]
TRADES = [tr("HBAR-USD", 10.62), tr("NEAR-USD", 7.40), tr("ALGO-USD", 5.00),
          tr("XLM-USD", 3.80), tr("TON-USD", 2.14), tr("LINK-USD", 2.11),
          tr("JASMY-USD", 1.92), tr("LTC-USD", 1.72), tr("ONDO-USD", 1.29),
          tr("TIA-USD", 1.21), tr("QNT-USD", 1.19), tr("ACH-USD", 0.36),
          tr("XRP-USD", 0.30), tr("FLOKI-USD", 0.25), tr("PRIME-USD", 0.24)]


def test_it_reproduces_the_hand_computed_split():
    r = cp.productivity(LIVE, TRADES, 48)
    assert r["can_buy"]["branches"] == 14
    assert r["can_buy"]["capital_usd"] == 4486.63
    assert r["can_buy"]["earned_usd"] == 37.90
    assert r["cannot_buy"]["branches"] == 9
    assert r["cannot_buy"]["capital_usd"] == 3611.79
    assert r["cannot_buy"]["earned_usd"] == 1.65
    assert r["ratio"] == 18.5


def test_the_two_halves_account_for_every_branch_and_every_dollar():
    r = cp.productivity(LIVE, TRADES, 48)
    assert r["can_buy"]["branches"] + r["cannot_buy"]["branches"] == len(LIVE)
    total = round(r["can_buy"]["capital_usd"] + r["cannot_buy"]["capital_usd"], 2)
    assert total == round(sum(b["allocated_usd"] for b in LIVE), 2)
    assert not (set(r["can_buy"]["products"]) & set(r["cannot_buy"]["products"]))


def test_the_bucketing_is_the_same_rule_no_dead_capital_uses():
    # Not a similar rule. The same one, on the same payload - if these
    # ever disagree the dashboard and the safety check are telling the
    # owner two different things about the same book.
    rows = [dict(b, best_slice_net_pct=-1.0) for b in LIVE]
    verdict = inv.no_dead_capital(rows)
    r = cp.productivity(LIVE, TRADES, 48)
    assert set(verdict["full_branches"]) == set(r["cannot_buy"]["products"])
    assert verdict["full_usd"] == r["cannot_buy"]["capital_usd"]


def test_earnings_are_never_converted_to_a_per_day_rate():
    # The withdrawn 0.1183%/day claim divided a long measurement by an
    # instantaneous denominator. Nothing here may reintroduce it.
    r = cp.productivity(LIVE, TRADES, 48)
    blob = str(r).lower()
    assert "per_day" not in blob and "pct_per_day" not in blob
    assert r["denominator_is_a_snapshot"] is True
    assert "must not be converted" in r["not_a_daily_rate"]


def test_the_circularity_is_stated_not_hidden():
    # Selling is what moves a branch out of the full bucket, so a branch
    # can be in the can-buy half BECAUSE it earned. If that caveat ever
    # disappears the figure starts getting read as causal.
    r = cp.productivity(LIVE, TRADES, 48)
    assert "BECAUSE it earned" in r["causation_caveat"]


def test_a_trade_on_a_closed_branch_is_reported_not_dropped():
    r = cp.productivity(LIVE, TRADES + [tr("GONE-USD", 5.00)], 48)
    assert r["unattributed"]["earned_usd"] == 5.00
    assert r["unattributed"]["trades"] == 1
    # and it must not have leaked into either half
    assert r["can_buy"]["earned_usd"] == 37.90
    assert r["cannot_buy"]["earned_usd"] == 1.65


def test_an_unreadable_branch_is_in_neither_bucket():
    # Counting it as "has room" would understate the parked figure,
    # which is the number the owner acts on.
    rows = LIVE + [{"product_id": "X-USD", "allocated_usd": 500.0,
                    "open_slices": None, "num_levels": 3}]
    r = cp.productivity(rows, TRADES, 48)
    assert r["unreadable_branches"] == ["X-USD"]
    assert "X-USD" not in r["can_buy"]["products"]
    assert "X-USD" not in r["cannot_buy"]["products"]


def test_a_full_half_that_earned_nothing_gives_no_ratio_and_says_why():
    r = cp.productivity(LIVE, [t for t in TRADES
                               if t["product_id"] not in ("ACH-USD", "ONDO-USD")], 48)
    assert r["cannot_buy"]["earned_usd"] == 0.0
    assert r["ratio"] is None                 # not infinity, not a huge number
    assert r["status"] == inv.FAIL
    assert "earned NOTHING" in r["detail"]


def test_an_empty_bucket_has_no_productivity_rather_than_zero():
    # "No capital here" and "this capital earned nothing" are different
    # claims and 0 would conflate them.
    only_full = [b for b in LIVE if b["open_slices"] >= b["num_levels"]]
    r = cp.productivity(only_full, TRADES, 48)
    assert r["can_buy"]["capital_usd"] == 0
    assert r["can_buy"]["earned_per_100_usd"] is None
    assert r["ratio"] is None
    assert r["status"] == inv.UNKNOWN


def test_no_branches_is_unknown_not_a_clean_pass():
    r = cp.productivity([], TRADES, 48)
    assert r["status"] == inv.UNKNOWN


def test_no_trades_in_the_window_is_zero_earned_not_an_error():
    r = cp.productivity(LIVE, [], 48)
    assert r["can_buy"]["earned_usd"] == 0.0
    assert r["can_buy"]["earned_per_100_usd"] == 0.0   # real capital, real zero
    assert r["ratio"] is None


def test_an_unreadable_allocation_does_not_poison_the_total():
    rows = [dict(b) for b in LIVE]
    rows[0]["allocated_usd"] = float("nan")
    r = cp.productivity(rows, TRADES, 48)
    assert r["cannot_buy"]["capital_usd"] == r["cannot_buy"]["capital_usd"]  # not NaN
    assert r["cannot_buy"]["capital_usd"] == round(3611.79 - 2272.62, 2)


def test_per_100_is_the_fixtures_own_arithmetic():
    r = cp.productivity(LIVE, TRADES, 48)
    assert r["can_buy"]["earned_per_100_usd"] == round(37.90 / 4486.63 * 100, 4)
    assert r["cannot_buy"]["earned_per_100_usd"] == round(1.65 / 3611.79 * 100, 4)


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS {name}")
            except AssertionError as e:
                fails += 1; print(f"  FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
