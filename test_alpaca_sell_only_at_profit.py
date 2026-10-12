"""sell_only_at_profit: no mean-reversion exit rule may close a position
below the fee floor.

Added 2026-10-12 at the account owner's instruction: "Is it going to stop
selling at a loss? ... Make sure that this doesn't continue to happen. We
need to sell at a win."

Pure function tests - no broker, no database, no network. Runnable as a
script (python3 test_alpaca_sell_only_at_profit.py) or under pytest.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from alpaca_mean_reversion import should_exit_position, ALPACA_ROUND_TRIP_PCT  # noqa: E402

FLOOR = ALPACA_ROUND_TRIP_PCT + 0.002   # the fallback the function uses when fee_floor is importable it reads the same figure


def _exit(price, rsi=None, age=60, entry=100.0, peak=0.0, gate=True):
    return should_exit_position(
        symbol="TEST", entry_price=entry, current_price=price,
        current_rsi=rsi, position_age_seconds=age, direction="long",
        max_hold_seconds=7200, stop_loss_pct=0.015,
        min_profit_target_pct=0.03, rsi_profit_threshold_long=60,
        peak_pnl_pct=peak, breakeven_trigger_pct=0.01, max_giveback_pct=0.015,
        sell_only_at_profit=gate,
    )


# ── the four loss-taking rules are held below the floor ────────────────

def test_the_hard_stop_does_not_sell_at_a_loss():
    should_exit, reason, exit_type, _ = _exit(97.0)          # -3.0%, past the -1.5% stop
    assert should_exit is False
    assert exit_type == "hold"
    assert "stop" in reason and "loss" in reason


def test_the_max_hold_does_not_sell_at_a_loss():
    should_exit, reason, exit_type, _ = _exit(99.5, age=99999)   # -0.5%, past 7,200s
    assert should_exit is False
    assert exit_type == "hold"
    assert "max hold" in reason


def test_the_breakeven_stop_does_not_sell_below_the_floor():
    # peak was +1.2% (ratchet armed), now exactly at entry: the old rule
    # closed here at 0.00% gross, which is a loss after both legs' fees.
    should_exit, reason, exit_type, _ = _exit(100.0, peak=0.012)
    assert should_exit is False
    assert exit_type == "hold"
    assert "breakeven stop" in reason


def test_the_peak_giveback_does_not_sell_below_the_floor():
    # peak +0.5% (above the arming threshold), now -1.2%: a 1.7% giveback.
    should_exit, reason, exit_type, _ = _exit(98.8, peak=0.005)
    assert should_exit is False
    assert exit_type == "hold"
    assert "peak giveback" in reason


def test_a_thin_rsi_exit_below_the_floor_is_held():
    # +0.05% gross with a hot RSI: the old RSI rule sold on any positive
    # number, which is a loss once 0.12% of fees come off.
    should_exit, reason, exit_type, _ = _exit(100.05, rsi=75.0)
    assert should_exit is False
    assert exit_type == "hold"


# ── above the floor every rule works exactly as before ─────────────────

def test_the_profit_target_still_sells():
    should_exit, reason, exit_type, _ = _exit(103.5)
    assert should_exit is True
    assert exit_type == "profit"


def test_the_max_hold_still_sells_when_it_is_a_win():
    should_exit, reason, exit_type, _ = _exit(101.0, age=99999)   # +1.0%, above the floor
    assert should_exit is True
    assert exit_type == "timeout"


def test_the_rsi_exit_still_sells_a_real_win():
    should_exit, reason, exit_type, _ = _exit(101.5, rsi=75.0)
    assert should_exit is True
    assert exit_type == "rsi_exit"


def test_the_giveback_still_sells_when_what_is_left_is_a_win():
    # peak +4.0%, now +2.0%: gave back 2.0%, still a net win - locks it in.
    should_exit, reason, exit_type, _ = _exit(102.0, peak=0.04)
    assert should_exit is True
    assert exit_type == "trail"


def test_the_peak_is_still_tracked_while_held():
    _, _, _, new_peak = _exit(99.0, peak=0.0)
    assert new_peak == 0.0
    _, _, _, new_peak = _exit(100.2, peak=0.0)
    assert abs(new_peak - 0.002) < 1e-9


def test_the_reason_names_the_floor():
    _, reason, _, _ = _exit(97.0)
    assert "+%.2f%%" % (FLOOR * 100) in reason


# ── the default is byte-for-byte the old behaviour ─────────────────────

def test_default_off_keeps_the_stop_loss():
    should_exit, reason, exit_type, _ = _exit(97.0, gate=False)
    assert should_exit is True
    assert exit_type == "stop_loss"


def test_default_off_keeps_the_timeout_at_a_loss():
    should_exit, reason, exit_type, _ = _exit(99.5, age=99999, gate=False)
    assert should_exit is True
    assert exit_type == "timeout"


def test_default_off_keeps_the_breakeven_stop():
    should_exit, reason, exit_type, _ = _exit(100.0, peak=0.012, gate=False)
    assert should_exit is True
    assert exit_type == "stop_loss"


def test_the_parameter_defaults_to_off():
    import inspect
    sig = inspect.signature(should_exit_position)
    assert sig.parameters["sell_only_at_profit"].default is False


# ── the live wiring in prop_bot ────────────────────────────────────────

def test_prop_bot_switch_is_off_unless_explicitly_armed():
    """Unset, empty, "0", "false", a typo, a quoted value - all OFF. Only
    an explicit affirmative arms it, so a deploy carrying the code changes
    nothing until the owner sets the variable."""
    import prop_bot
    f = prop_bot._sell_only_at_profit_from_env
    saved = os.environ.pop("ALPACA_SELL_ONLY_AT_PROFIT", None)
    try:
        assert f() is False
        for v in ("", "0", "false", "no", "off", "tru", "armed?"):
            os.environ["ALPACA_SELL_ONLY_AT_PROFIT"] = v
            assert f() is False, v
        for v in ("true", " True ", '"yes"', "1", "on"):
            os.environ["ALPACA_SELL_ONLY_AT_PROFIT"] = v
            assert f() is True, v
    finally:
        os.environ.pop("ALPACA_SELL_ONLY_AT_PROFIT", None)
        if saved is not None:
            os.environ["ALPACA_SELL_ONLY_AT_PROFIT"] = saved


def test_prop_bot_passes_the_switch_at_both_live_call_sites():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "prop_bot.py")).read()
    assert src.count("sell_only_at_profit=MEAN_REVERSION_SELL_ONLY_AT_PROFIT") == 2


if __name__ == "__main__":
    names = [n for n in sorted(globals()) if n.startswith("test_")]
    failed = 0
    for n in names:
        try:
            globals()[n]()
            print("ok   ", n)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("FAIL ", n, "-", type(e).__name__, e)
    print(f"{len(names) - failed}/{len(names)} passed")
    sys.exit(1 if failed else 0)
