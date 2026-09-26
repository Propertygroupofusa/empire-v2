"""Tests for the harness that is supposed to stop the next bad backtest.

The tests that matter are the ones proving it REFUSES: that a pure-noise
search cannot pass, that an in-sample number cannot be read as a result,
and that the cross-rate study - which really did produce +3.252% and
t=13.14 before dying out of sample - now comes back FAIL.
"""
import ast
import math
import random

import pytest

import walk_forward as wf


# --------------------------------------------------------------- split

def test_split_is_chronological():
    assert wf.split_index(100, 0.70) == 70


def test_split_never_empties_either_side():
    assert wf.split_index(2, 0.99) == 1
    assert wf.split_index(2, 0.01) == 1


@pytest.mark.parametrize("bad", [0, 1, None, -5])
def test_split_refuses_impossible_lengths(bad):
    with pytest.raises(ValueError):
        wf.split_index(bad)


@pytest.mark.parametrize("bad", [0, 1, 1.5, -0.2])
def test_split_refuses_impossible_fractions(bad):
    with pytest.raises(ValueError):
        wf.split_index(100, bad)


# ----------------------------------------------------- search threshold

def test_one_test_keeps_the_textbook_bar():
    assert wf.search_threshold(1) == wf.NAIVE_T


def test_the_bar_rises_with_the_width_of_the_search():
    assert wf.search_threshold(96) > wf.search_threshold(10) > wf.search_threshold(1)


def test_the_bar_matches_the_expected_maximum_of_n_normals():
    assert wf.search_threshold(96) == pytest.approx(math.sqrt(2 * math.log(96)))


def test_the_real_search_bar_exceeds_the_surviving_t():
    """96 configs -> 3.02. The cross-rate survivor's 2.61 is BELOW noise."""
    assert wf.search_threshold(96) > 2.61


# ---------------------------------------------------------- overlap

def test_overlap_correction_divides_by_root_hold():
    r = [1.0, 2.0, 0.5, 1.5, 3.0, -0.5, 2.0, 1.0]
    raw, adj = wf.corrected_t(r, 42)
    assert adj == pytest.approx(raw / math.sqrt(42))


def test_the_real_inflation_is_reproduced():
    """13.14 raw on a 42-day hold was really about 2.03."""
    assert 13.14 / math.sqrt(42) == pytest.approx(2.028, abs=0.01)


def test_independent_rounds_account_for_the_hold():
    assert wf.independent_rounds(6694, 42) == pytest.approx(159.4, abs=0.5)


def test_flat_returns_give_no_t_rather_than_infinity():
    assert wf.corrected_t([2.0] * 10, 5) == (None, None)


def test_too_few_points_give_no_t():
    assert wf.corrected_t([1.0], 5) == (None, None)


# -------------------------------------------------------------- verdict

def _stats(**kw):
    base = {"observations": 500, "independent_rounds": 25.0, "net_pct": 1.0,
            "t_overlap_corrected": 5.0, "t_threshold": 3.02, "fee_pct": 0.70}
    base.update(kw)
    return base


def test_a_losing_test_window_fails():
    v, why = wf.verdict(_stats(net_pct=-0.137), 96)
    assert v == wf.FAIL and "loses" in why


def test_exactly_zero_is_not_a_pass():
    v, _ = wf.verdict(_stats(net_pct=0.0), 96)
    assert v == wf.FAIL


def test_profitable_but_under_the_search_bar_fails():
    """The lone cross-rate survivor: +1.759% at t=2.61 against a 3.02 bar."""
    v, why = wf.verdict(_stats(net_pct=1.759, t_overlap_corrected=2.61), 96)
    assert v == wf.FAIL and "noise alone" in why


def test_the_same_result_passes_when_it_was_the_only_test():
    v, _ = wf.verdict(_stats(net_pct=1.759, t_overlap_corrected=2.61,
                             t_threshold=wf.NAIVE_T), 1)
    assert v == wf.PASS


def test_too_few_independent_rounds_is_inconclusive_not_a_pass():
    """1.5 rounds is what the survivor actually had out of sample."""
    v, why = wf.verdict(_stats(independent_rounds=1.5, net_pct=9.0), 96)
    assert v == wf.INCONCLUSIVE and "independent rounds" in why


def test_too_few_observations_is_inconclusive():
    v, _ = wf.verdict(_stats(observations=10), 96)
    assert v == wf.INCONCLUSIVE


def test_inconclusive_is_never_rounded_up_to_pass():
    for s in (_stats(observations=5), _stats(independent_rounds=1.0),
              _stats(net_pct=None), _stats(t_overlap_corrected=None)):
        assert wf.verdict(s, 96)[0] != wf.PASS


# ------------------------------------------------------------- the run

def _noise_runner(seed=0):
    rnd = random.Random(seed)
    table = {}

    def run_fn(cfg, lo, hi):
        key = (cfg["id"], lo, hi)
        if key not in table:
            table[key] = [rnd.gauss(0, 8) for _ in range(400)]
        return table[key]
    return run_fn


def test_a_search_over_pure_noise_does_not_pass():
    """The single most important test in this file."""
    cfgs = [{"id": i, "hold": 10} for i in range(96)]
    r = wf.run(cfgs, _noise_runner(1), train_hi=200, test_lo=200, test_hi=350,
               hold_of=lambda c: c["hold"], label="pure noise")
    assert r["verdict"] != wf.PASS, r["reason"]


@pytest.mark.parametrize("seed", range(8))
def test_noise_does_not_pass_on_any_seed(seed):
    cfgs = [{"id": i, "hold": 21} for i in range(50)]
    r = wf.run(cfgs, _noise_runner(seed), train_hi=200, test_lo=200, test_hi=350,
               hold_of=lambda c: c["hold"], label="noise")
    assert r["verdict"] != wf.PASS


def test_a_real_edge_survives():
    """The harness must not be so strict that nothing can ever pass."""
    rnd = random.Random(4)

    def run_fn(cfg, lo, hi):
        # a genuine +2% per round, present in BOTH windows
        return [2.0 + 0.70 + rnd.gauss(0, 3) for _ in range(600)]
    cfgs = [{"id": i, "hold": 1} for i in range(4)]
    r = wf.run(cfgs, run_fn, train_hi=200, test_lo=200, test_hi=350,
               hold_of=lambda c: c["hold"], label="real edge")
    assert r["verdict"] == wf.PASS, r["reason"]


def test_the_result_is_the_test_window_not_the_training_one():
    def run_fn(cfg, lo, hi):
        # spectacular in training, flat in test
        return [9.0] * 300 if lo == 0 else [0.70 + (0.01 if i % 2 else -0.01)
                                            for i in range(300)]
    cfgs = [{"id": 0, "hold": 1}]
    r = wf.run(cfgs, run_fn, train_hi=200, test_lo=200, test_hi=350,
               hold_of=lambda c: c["hold"], label="decays")
    assert r["net_pct"] == pytest.approx(0.0, abs=0.01)
    assert r["selection_only"]["net_pct"] == pytest.approx(8.3, abs=0.01)


def test_the_training_number_is_labelled_a_selection():
    def run_fn(cfg, lo, hi):
        return [3.0] * 300
    r = wf.run([{"id": 0, "hold": 1}], run_fn, train_hi=200, test_lo=200,
               test_hi=350, hold_of=lambda c: c["hold"], label="x")
    assert "selection_only" in r and "in_sample" not in r
    assert "not evidence" in r["selection_only"]["warning"]


def test_decay_is_reported():
    def run_fn(cfg, lo, hi):
        return [5.0] * 300 if lo == 0 else [1.0] * 300
    r = wf.run([{"id": 0, "hold": 1}], run_fn, train_hi=200, test_lo=200,
               test_hi=350, hold_of=lambda c: c["hold"], label="x")
    assert r["decay_pct"] == pytest.approx(-4.0, abs=0.01)
    assert "fall of" in r["decay_note"]


def test_a_config_that_raises_does_not_kill_the_search():
    def run_fn(cfg, lo, hi):
        if cfg["id"] == 1:
            raise RuntimeError("bad config")
        return [3.0] * 300
    r = wf.run([{"id": 0, "hold": 1}, {"id": 1, "hold": 1}], run_fn,
               train_hi=200, test_lo=200, test_hi=350,
               hold_of=lambda c: c["hold"], label="x")
    assert r["verdict"] in (wf.PASS, wf.FAIL, wf.INCONCLUSIVE)


def test_an_empty_search_is_refused():
    with pytest.raises(ValueError):
        wf.run([], lambda c, a, b: [], train_hi=10, test_lo=10, test_hi=20,
               hold_of=lambda c: 1)


def test_the_search_width_is_always_reported():
    r = wf.run([{"id": i, "hold": 1} for i in range(7)],
               lambda c, a, b: [1.0] * 100, train_hi=50, test_lo=50, test_hi=100,
               hold_of=lambda c: c["hold"], label="x")
    assert r["configurations_tested"] == 7


# --------------------------------------------------- structural guards

def test_fees_are_a_round_trip():
    assert wf.DEFAULT_FEE_PCT == pytest.approx(0.70)


def test_evaluate_always_subtracts_the_fee():
    s = wf.evaluate([1.0] * 50, hold_days=1, fee_pct=0.70)
    assert s["net_pct"] == pytest.approx(0.30)


def test_the_module_reaches_no_network_and_no_venue():
    tree = ast.parse(open("walk_forward.py").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None)]
            for n in names:
                assert not any(bad in (n or "") for bad in
                               ("aiohttp", "requests", "httpx", "urllib", "coinbase")), \
                    f"harness imports {n}"


def test_no_key_named_in_sample_exists_anywhere():
    """A key called in_sample invites being quoted as a result."""
    def run_fn(cfg, lo, hi):
        return [2.0] * 200
    r = wf.run([{"id": 0, "hold": 1}], run_fn, train_hi=100, test_lo=100,
               test_hi=200, hold_of=lambda c: c["hold"], label="x")

    def walk(o, path=""):
        if isinstance(o, dict):
            for k, v in o.items():
                assert k != "in_sample", f"found in_sample at {path}"
                walk(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")
    walk(r)
