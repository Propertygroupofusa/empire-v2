"""A gain on coin the wallet does not hold is not a gain.

Measured live 2026-09-30 20:35Z. QNT-USD reported +$87.90 unrealized -
the largest single gain in the fleet - on 0.675982 claimed units. The
venue held 0.00097323, so its escape sell had been refused 182 times in
24 hours with BELOW_BASE_INCREMENT (0.00097323 is under QNT's 0.001
minimum). None of that was visible beside the +$87.90.

Fleet-wide: books claimed $7,053.11 of coin, $1,199.90 of it was not in
the wallet, four branches sat at 0.000% backed.

The rule this shares with the rest of the codebase: UNKNOWN is a third
verdict. An unreadable balance is not a shortfall.
"""
import slice_backing as sb


def _branch(pid, qty, price, unreal, entry=1.0):
    return {"product_id": pid, "current_price": price,
            "total_unrealized_net_usd": unreal,
            "slices": [{"qty": qty, "entry_price": entry}]}


# --- the live case, reproduced --------------------------------------------

def test_qnt_gain_on_coin_that_is_not_there_is_marked_phantom():
    out = sb.assess([_branch("QNT-USD", 0.675982, 291.84, 87.90)],
                    {"QNT": 0.00097323})
    r = out["rows"][0]
    assert r["backed"] is False
    assert r["unrealized_is_phantom"] is True
    assert r["can_be_sold"] is False
    assert r["backed_pct"] < 1.0, r["backed_pct"]
    assert out["phantom_unrealized_usd"] == 87.90
    assert "not money that can be taken" in r["why"]


def test_the_shortfall_and_the_phantom_gain_are_different_numbers():
    # The shortfall is what the BOOKS overstate; the phantom gain is what
    # the HEADLINE overstates. Blending them double-counts.
    out = sb.assess([_branch("QNT-USD", 0.675982, 291.84, 87.90)],
                    {"QNT": 0.00097323})
    # 0.675982 - 0.00097323 = 0.67500877 units short, x $291.84 = $196.99.
    # NOT $197.28 - that is the full claimed VALUE, which is what you get
    # from a source that omits QNT entirely and so reads held as zero. The
    # two differ by the dust the venue really does hold, and picking the
    # wrong one is how a shortfall gets overstated by the amount of coin
    # that is actually there.
    assert out["not_in_wallet_usd"] == 196.99, out["not_in_wallet_usd"]
    assert out["phantom_unrealized_usd"] == 87.90
    assert out["not_in_wallet_usd"] != out["phantom_unrealized_usd"]


# --- a real, fully-backed branch is left alone ----------------------------

def test_a_backed_branch_is_not_flagged():
    out = sb.assess([_branch("TON-USD", 51.21, 1.48, 2.0)], {"TON": 51.21})
    r = out["rows"][0]
    assert r["backed"] is True and r["unrealized_is_phantom"] is False
    assert r["why"] is None
    assert out["unbacked_branches"] == 0
    assert out["phantom_unrealized_usd"] == 0.0


def test_a_rounding_gap_is_not_a_finding():
    # 93.9% backed, like ZEC live - real, and must not be called unbacked.
    out = sb.assess([_branch("ZEC-USD", 1.418971, 1420.4, -334.05)],
                    {"ZEC": 1.331766})
    r = out["rows"][0]
    assert r["backed"] is True, r["backed_pct"]
    assert r["unrealized_is_phantom"] is False


def test_a_loss_on_unbacked_coin_is_not_called_a_phantom_gain():
    # PEPE live: 0.000% backed but DOWN. There is no gain to overstate.
    out = sb.assess([_branch("PEPE-USD", 32268212.95, 0.0000042, -3.28)],
                    {"PEPE": 0.06879848})
    r = out["rows"][0]
    assert r["backed"] is False
    assert r["unrealized_is_phantom"] is False
    assert out["phantom_unrealized_usd"] == 0.0
    assert r["why"].endswith(".")


# --- UNKNOWN is a third verdict -------------------------------------------

def test_an_unreadable_balance_is_unknown_not_unbacked():
    out = sb.assess([_branch("QNT-USD", 0.675982, 291.84, 87.90)],
                    {}, unreadable_assets=["QNT"])
    assert out["rows"] == []
    assert out["unknown_count"] == 1
    assert out["unknown"][0]["this_is_unknown_not_unbacked"] is True
    assert out["unbacked_branches"] == 0
    assert out["not_in_wallet_usd"] == 0.0, "an unread balance became a shortfall"
    assert out["phantom_unrealized_usd"] == 0.0


def test_an_asset_missing_from_the_reading_is_also_unknown():
    out = sb.assess([_branch("QNT-USD", 0.675982, 291.84, 87.90)], {"TON": 1.0})
    assert out["unknown_count"] == 1
    assert out["not_in_wallet_usd"] == 0.0


# --- arithmetic and edges -------------------------------------------------

def test_holding_more_than_claimed_is_never_a_shortfall():
    out = sb.assess([_branch("FLOKI-USD", 854519.0, 0.00005, 0.0)],
                    {"FLOKI": 2336831.48})
    r = out["rows"][0]
    assert r["backed"] is True and r["short_usd"] == 0.0
    assert out["not_in_wallet_usd"] == 0.0


def test_fleet_totals_add_up_and_rank_worst_first():
    out = sb.assess(
        [_branch("QNT-USD", 0.675982, 291.84, 87.90),
         _branch("PEPE-USD", 32268212.95, 0.0000042, -3.28),
         _branch("TON-USD", 51.21, 1.48, 2.0)],
        {"QNT": 0.00097323, "PEPE": 0.0688, "TON": 51.21})
    assert out["branches_measured"] == 3
    assert out["unbacked_branches"] == 2
    assert out["rows"][0]["asset"] == "QNT", "worst gap must sort first"
    assert out["backed_pct_of_claim"] is not None


def test_garbage_numbers_do_not_crash_or_fabricate():
    out = sb.assess([{"product_id": "X-USD", "current_price": None,
                      "total_unrealized_net_usd": "oops",
                      "slices": [{"qty": None, "entry_price": 1}]}], {"X": 1.0})
    assert out["readable"] is True
    assert out["rows"] == [] and out["unknown"] == []


def test_it_changes_nothing():
    out = sb.assess([_branch("QNT-USD", 0.675982, 291.84, 87.90)],
                    {"QNT": 0.00097323})
    assert out["is_a_measurement_not_a_change"] is True
    assert "reconcile-slices" in out["note"]


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS {name}")
            except AssertionError as e:
                fails += 1; print(f"  FAIL {name}: {e}")
            except Exception as e:
                fails += 1; print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{fails} failure(s)")
    sys.exit(1 if fails else 0)
