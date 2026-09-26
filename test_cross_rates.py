"""Tests for the matrix desk's arithmetic, and for what it refuses to say.

The structural tests at the bottom are the important ones. The measured
result is that the rotation signal does not pay, so the module must not
be able to express an instruction to rotate - not in a status, not in a
string, not anywhere the UI could pick it up.
"""
import ast
import statistics

import pytest

import cross_rates as cr


def _series(vals):
    return list(vals)


# --------------------------------------------------------------- ratio

def test_ratio_is_a_over_b():
    assert cr.ratio(10, 4) == 2.5


@pytest.mark.parametrize("a,b", [(None, 2), (2, None), (2, 0), ("x", 2),
                                 (float("nan"), 2), (2, float("inf"))])
def test_ratio_refuses_bad_inputs(a, b):
    assert cr.ratio(a, b) is None


def test_division_by_zero_is_none_not_an_exception():
    assert cr.ratio(1, 0) is None


# -------------------------------------------------------------- zscore

def test_zscore_matches_the_definition():
    hist = [1, 2, 3, 4, 5]
    z = cr.zscore(6, hist)
    assert z == pytest.approx((6 - 3) / statistics.pstdev(hist))


def test_flat_history_is_none_not_infinity():
    """A zero-spread history would make every tick look infinitely extreme."""
    assert cr.zscore(5, [3, 3, 3, 3, 3, 3]) is None


def test_short_history_is_none():
    assert cr.zscore(5, [1, 2, 3]) is None


def test_zscore_ignores_unreadable_points():
    assert cr.zscore(6, [1, 2, None, 3, 4, "x", 5]) is not None


# ------------------------------------------------------------ classify

@pytest.mark.parametrize("z,expect", [
    (0.0, cr.EQUILIBRIUM), (0.99, cr.EQUILIBRIUM), (-0.99, cr.EQUILIBRIUM),
    (1.0, cr.DRIFTING), (-1.5, cr.DRIFTING), (1.99, cr.DRIFTING),
    (2.0, cr.STRETCHED), (-3.4, cr.STRETCHED),
    (None, cr.UNKNOWN),
])
def test_classify_bands(z, expect):
    assert cr.classify(z) == expect


def test_there_is_no_rotate_state():
    """The measured edge is negative after fees. No state may command a trade."""
    states = {cr.STRETCHED, cr.DRIFTING, cr.EQUILIBRIUM, cr.UNKNOWN}
    for s in states:
        assert "rotate" not in s.lower()
        assert "buy" not in s.lower() and "sell" not in s.lower()


# ------------------------------------------------------------ pair_row

def test_pair_row_computes_a_live_ratio():
    a = [10, 11, 12, 13, 14, 15, 16]
    b = [5, 5, 5, 5, 5, 5, 4]
    r = cr.pair_row("A", "B", a, b)
    assert r["ratio"] == pytest.approx(4.0)
    assert r["pair"] == "A/B"


def test_short_series_is_unknown_not_a_guess():
    r = cr.pair_row("A", "B", [1, 2], [1, 2])
    assert r["status"] == cr.UNKNOWN and r["z"] is None


def test_misaligned_series_are_truncated_to_the_overlap():
    r = cr.pair_row("A", "B", [1] * 40, [1] * 8)
    assert r["sessions"] == 8


def test_rich_leg_is_named_only_when_stretched():
    flat = cr.pair_row("A", "B", [10] * 20 + [10.01], [5] * 21)
    assert flat["rich"] is None
    spike = cr.pair_row("A", "B", [10, 10.1, 9.9, 10.05, 9.95, 10, 10.02, 40],
                        [5] * 8)
    assert spike["status"] == cr.STRETCHED and spike["rich"] == "A"


def test_detail_never_gives_an_instruction():
    spike = cr.pair_row("A", "B", [10, 10.1, 9.9, 10.05, 9.95, 10, 10.02, 40], [5] * 8)
    d = spike["detail"].lower()
    assert "rotate" not in d and "sell " not in d and "buy " not in d
    assert "has not paid" in d


# -------------------------------------------------------------- matrix

def _book():
    import random
    random.seed(3)
    return {k: [100 + random.gauss(0, 3) for _ in range(60)]
            for k in ("BTC", "ETH", "XRP", "ZEC")}


def test_matrix_covers_every_pair():
    m = cr.matrix(["BTC", "ETH", "XRP", "ZEC"], _book())
    assert m["pairs"] == 6            # 4 choose 2


def test_missing_assets_are_reported_not_dropped():
    book = _book()
    del book["ZEC"]
    m = cr.matrix(["BTC", "ETH", "XRP", "ZEC"], book)
    assert m["missing_assets"] == ["ZEC"]
    assert "no usable history" in m["headline"]


def test_rows_are_sorted_by_how_stretched_they_are():
    m = cr.matrix(["BTC", "ETH", "XRP", "ZEC"], _book())
    zs = [abs(r["z"]) for r in m["rows"] if r["z"] is not None]
    assert zs == sorted(zs, reverse=True)


def test_counts_add_up_to_the_pairs():
    m = cr.matrix(["BTC", "ETH", "XRP", "ZEC"], _book())
    assert sum(m["counts"].values()) == m["pairs"]


def test_matrix_always_carries_the_measured_edge():
    """The UI must not be able to show a z-score without its refutation."""
    m = cr.matrix(["BTC", "ETH"], _book())
    assert m["edge"]["tested"] is True
    assert m["edge"]["rows"]


def test_matrix_labels_itself_a_measurement():
    m = cr.matrix(["BTC", "ETH"], _book())
    assert m["is_a_measurement_not_a_signal"] is True


# ----------------------------------------------------------- the edge

def test_no_tested_setting_pays_after_fees():
    """If this ever fails, the desk's copy is wrong and must be rewritten."""
    e = cr.tested_edge()
    assert all(r["net_pct"] <= 0.01 for r in e["rows"])


def test_a_round_trip_is_two_legs():
    assert cr.ROUND_TRIP_FEE_PCT == pytest.approx(0.70)


def test_every_tested_row_reports_its_significance():
    for r in cr.tested_edge()["rows"]:
        assert "t_stat" in r and abs(r["t_stat"]) < 1.96, \
            "a significant result would change what this desk may say"


# ------------------------------------------------- structural guards

def test_module_issues_no_orders_and_fetches_nothing():
    tree = ast.parse(open("cross_rates.py").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None)]
            for n in names:
                assert not any(bad in (n or "") for bad in
                               ("aiohttp", "requests", "httpx", "urllib", "coinbase")), \
                    f"cross_rates imports {n}"


def test_no_status_constant_is_an_imperative():
    """Checked on the parsed tree so the docstring's prose cannot trip it."""
    tree = ast.parse(open("cross_rates.py").read())
    banned = ("GATE_UNLOCKED", "ROTATE", "UNLOCKED")
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in banned:
                    pytest.fail(f"module defines an imperative state: {t.id}")


# ------------------------------------------------ board composition

def _stretched(a, b, z):
    return {"pair": f"{a}/{b}", "a": a, "b": b, "z": z,
            "status": cr.STRETCHED if abs(z) >= 2 else cr.EQUILIBRIUM}


def _complete_matrix(assets, stretched_with=None):
    """Every pair present, as a real matrix always is. A coin's baseline
    share is what makes dominance meaningful, so a partial set would
    measure the wrong thing."""
    import itertools
    rows = []
    for a, b in itertools.combinations(assets, 2):
        hot = stretched_with and stretched_with in (a, b)
        rows.append(_stretched(a, b, -5.0 if hot else 0.1))
    return rows


def test_one_runaway_coin_is_named_as_one_finding():
    """QNT ran 65% against everything. That is one move, not ten signals."""
    rows = _complete_matrix(["QNT", "A", "B", "C", "D", "E"], stretched_with="QNT")
    d = cr.dominant_asset(rows)
    assert d and d["asset"] == "QNT" and d["pairs"] == 5
    assert "one coin that moved" in d["note"]


def test_a_coin_stretched_no_more_than_its_share_is_not_dominant():
    """The guard against crying concentration at ordinary participation."""
    rows = _complete_matrix(["A", "B", "C", "D", "E", "F"])
    for r in rows[:6]:
        r["z"] = -3.0
        r["status"] = cr.STRETCHED
    d = cr.dominant_asset(rows)
    assert d is None or d["observed_pct"] >= d["expected_pct"] * 2


def test_no_dominant_coin_when_the_stretch_is_spread_out():
    rows = [_stretched("A", "B", -3.0), _stretched("C", "D", -3.0),
            _stretched("E", "F", -3.0), _stretched("G", "H", -3.0)]
    assert cr.dominant_asset(rows) is None


def test_dominance_is_measured_against_the_matrix_baseline():
    """A flat threshold would never fire: in a 12-coin matrix every coin
    already sits in 17% of the pairs, so 46% has to count as concentrated."""
    rows = _complete_matrix([chr(65 + i) for i in range(12)], stretched_with="A")
    d = cr.dominant_asset(rows)
    assert d and d["asset"] == "A"
    assert d["observed_pct"] > d["expected_pct"] * 2


def test_too_few_stretched_pairs_claims_nothing():
    assert cr.dominant_asset([_stretched("A", "B", -3.0)]) is None
    assert cr.dominant_asset([]) is None


def test_board_is_thinned_but_counts_are_not():
    """Only the DISPLAY is capped. The counts must still cover every pair."""
    import random
    random.seed(11)
    book = {k: [100 + random.gauss(0, 3) for _ in range(60)]
            for k in ("A", "B", "C", "D", "E", "F")}
    m = cr.matrix(list(book), book, max_per_asset=2)
    assert m["pairs"] == 15                      # 6 choose 2
    assert sum(m["counts"].values()) == 15       # every pair counted
    assert m["rows_shown"] <= 15                 # display thinned


def test_no_asset_exceeds_its_display_cap():
    import random
    random.seed(12)
    book = {k: [100 + random.gauss(0, 3) for _ in range(60)]
            for k in ("A", "B", "C", "D", "E", "F")}
    m = cr.matrix(list(book), book, max_per_asset=2)
    seen = {}
    for r in m["rows"]:
        for k in ("a", "b"):
            seen[r[k]] = seen.get(r[k], 0) + 1
    assert all(v <= 2 for v in seen.values()), seen


def test_max_per_asset_none_shows_everything():
    import random
    random.seed(13)
    book = {k: [100 + random.gauss(0, 2) for _ in range(60)]
            for k in ("A", "B", "C", "D")}
    m = cr.matrix(list(book), book, max_per_asset=None)
    assert m["rows_shown"] == m["pairs"]
