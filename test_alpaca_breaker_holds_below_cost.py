"""The stock bot's forced exits sell only at a win, like its own exits.

2026-10-14 14:06:51-14:07:39Z: the daily-loss circuit breaker (equity down
$10 on the day, paper losses included) closed every position at once -
SLV -$6.41 and -$1.74 (-5.09%), AMZN -$1.84 (-1.44%), META -$1.41
(-1.17%). ALPACA_SELL_ONLY_AT_PROFIT was on, but it only ever reached
should_exit_position; the breaker and the make-room rotation never asked
it. The breaker also left entries open below the 2% limit, so SLV was
bought and closed again five times in twenty minutes.

The owner the same day: "make sure it sells as a profit and not a loss...
Let only profit happen."

Pinned here: the pure rule (behavioural) and its wiring (structural - the
cycle needs the broker to run).
"""
import os

import prop_bot as pb

SRC = open(pb.__file__, encoding="utf-8").read()
CYCLE = SRC[SRC.index("async def run_prop_cycle("):]


def test_this_mornings_forced_sales_are_all_held():
    # entry, price at 14:06Z
    for name, entry, px in (("SLV", 53.535, 50.81), ("AMZN", 261.038, 257.276),
                            ("META", 756.356, 747.514)):
        ok, why = pb.forced_exit_allowed({"entry": entry, "side": "long"}, px, True)
        assert ok is False and "held at" in why, (name, why)


def test_a_position_above_the_win_floor_may_still_be_sold():
    ok, why = pb.forced_exit_allowed({"entry": 745.612, "side": "long"}, 760.0, True)
    assert ok is True, why                       # META +1.93% at the open


def test_just_above_cost_but_under_the_fee_floor_is_held():
    ok, _ = pb.forced_exit_allowed({"entry": 100.0, "side": "long"}, 100.10, True)
    assert ok is False                           # +0.10% does not cover the round trip


def test_unreadable_inputs_hold():
    assert pb.forced_exit_allowed({"entry": None}, 50.0, True)[0] is False
    assert pb.forced_exit_allowed({"entry": 50.0}, None, True)[0] is False
    assert pb.forced_exit_allowed(None, 50.0, True)[0] is False


def test_switched_off_the_old_behaviour_returns():
    ok, why = pb.forced_exit_allowed({"entry": 53.535, "side": "long"}, 50.81, False)
    assert ok is True and "off" in why


def test_the_breaker_asks_before_it_sells():
    i = CYCLE.index("if daily_circuit_breaker_tripped:")
    block = CYCLE[i:CYCLE.index("if equity_floor_breached:", i)]
    assert block.index("forced_exit_allowed(") < block.index("await close_position(")


def test_the_breaker_also_stops_new_entries():
    i = CYCLE.index("for _, contract, config, side, price, rsi, trend in candidates:")
    entry_loop = CYCLE[i:i + 8000]
    j = entry_loop.index("if daily_circuit_breaker_tripped:")
    assert entry_loop.index("continue", j) < entry_loop.index("try_open(")


def test_rotation_never_sells_a_losing_position_under_profit_only():
    i = CYCLE.index('"ROTATED OUT"')
    before = CYCLE[max(0, i - 2500):i]
    k = before.rindex("and MEAN_REVERSION_SELL_ONLY_AT_PROFIT")
    assert "elif weakest_contract is not None" in before[k:]


# --- "No losses. Only gains." (the owner, 2026-10-14, the same afternoon) -----
# Every other forced or rule-driven sale path gets the same floor.

def test_the_equity_floor_breach_asks_before_it_sells():
    i = CYCLE.index("if equity_floor_breached:")
    block = CYCLE[i:i + 3500]
    assert block.index("forced_exit_allowed(") < block.index('"EQUITY FLOOR BREACH")')


def test_momentum_exits_are_held_below_the_win_floor_on_both_paths():
    n = SRC.count("should_exit_position_momentum(")
    calls = [k for k in range(len(SRC)) if SRC.startswith("should_exit_position_momentum(", k)]
    guarded = 0
    for k in calls:
        after = SRC[k:k + 900]
        if "if should_exit and not forced_exit_allowed(" in after:
            guarded += 1
    assert guarded == 2, (guarded, n)


def test_the_opening_bar_exit_asks_before_it_sells():
    fn = SRC[SRC.index("async def run_opening_bar_symbol_cycle("):]
    fn = fn[:fn.index("\nasync def ", 10)]
    assert fn.index("forced_exit_allowed(") < fn.index('"OPENING_BAR_EXIT"')


def test_every_sell_order_in_the_stock_bot_is_a_known_path():
    """A new SELL call site must be looked at against the floor, not
    discovered after it has sold something at a loss."""
    sites = SRC.count('execute_futures_trade(session, contract, "SELL"') + \
        SRC.count("execute_futures_trade(session, contract, close_action")
    assert sites == 3, sites   # close_position, branch_exit, opening_bar_exit
