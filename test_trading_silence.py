"""Quiet is not broken, and broken is not quiet.

2026-09-10 to 09-25: the grid closed ZERO round trips for sixteen days.
Not the strategy, not the market - the Coinbase JWT was signed wrong, the
venue answered 401, and with maker-only on "no maker order was ever
placed" (the fixing commit, 09-25 20:20). The repo's own history shows an
URGENT "resume trading immediately" on 09-10 and credential commits still
landing on 09-22. Eleven days of not knowing, worth roughly $4,353/year
at the fleet's measured best rate.

Nothing watched for it. This is that watch, and the only thing it has to
get right is the difference between a grid waiting for a move and a grid
being refused - because an alarm that fires on every calm night gets
muted, and then the real one is missed too.
"""
import trading_silence as ts


def B(pid, price, ref, step=0.03, slices=1):
    return {"product_id": pid, "current_price": price, "reference_price": ref,
            "grid_pct": step, "slices": [{"qty": 1, "entry_price": ref}] * slices}


# --- quiet must NOT cry wolf ---------------------------------------------

def test_a_long_quiet_night_with_nothing_refused_is_not_an_alarm():
    r = ts.assess(hours_since_last_fill=10.0, refusals_by_product={},
                  branches=[B("XLM-USD", 0.223, 0.2245)])   # below trigger
    assert r["verdict"] == "QUIET" and r["alarm"] is False, r
    assert r["quiet_is_not_broken"] is True


def test_a_recent_fill_is_simply_trading():
    r = ts.assess(hours_since_last_fill=0.5, refusals_by_product={})
    assert r["verdict"] == "TRADING" and r["alarm"] is False


def test_one_stray_refusal_is_not_an_outage():
    # Dust and rounding edges refuse single orders all the time.
    r = ts.assess(hours_since_last_fill=9.0, refusals_by_product={"PEPE-USD": 3})
    assert r["verdict"] == "QUIET" and r["alarm"] is False, r


# --- broken must fire -----------------------------------------------------

def test_repeated_refusals_are_the_alarm():
    # The live QNT case: 182 refusals in 24h, same order into the same wall.
    r = ts.assess(hours_since_last_fill=10.0,
                  refusals_by_product={"QNT-USD": 182, "PEPE-USD": 18})
    assert r["verdict"] == "BROKEN" and r["alarm"] is True
    assert "QNT-USD x182" in r["reason"], r["reason"]
    assert "2026-09-10" in r["why_this_matters"]


def test_a_branch_past_its_trigger_that_has_not_sold_is_the_alarm():
    # QNT sat 39% PAST its sell trigger for hours. The decision was made;
    # the order did not happen. That is the sharpest signal there is.
    r = ts.assess(hours_since_last_fill=10.0, refusals_by_product={},
                  branches=[B("QNT-USD", 291.84, 145.0)])
    assert r["verdict"] == "BROKEN" and r["alarm"] is True
    assert r["past_trigger"][0]["product_id"] == "QNT-USD"
    assert r["past_trigger"][0]["pct_past_trigger"] > 90


def test_dying_cycles_are_the_alarm():
    r = ts.assess(hours_since_last_fill=8.0, cycle_errors=40)
    assert r["verdict"] == "BROKEN" and "cycles died" in r["reason"]


def test_broken_fires_even_when_a_fill_was_recent():
    # The JWT outage did not announce itself with silence first - orders
    # were refused while the last fill was still fresh. Waiting for the
    # quiet threshold before looking would have cost days.
    r = ts.assess(hours_since_last_fill=0.2, refusals_by_product={"QNT-USD": 50})
    assert r["verdict"] == "BROKEN" and r["alarm"] is True


# --- UNKNOWN is a third verdict ------------------------------------------

def test_unreadable_inputs_are_unknown_not_healthy():
    for kw in ({"readable": False, "hours_since_last_fill": 1},
               {"hours_since_last_fill": None}):
        r = ts.assess(**kw)
        assert r["verdict"] == "UNKNOWN", r
        assert r["alarm"] is False
        assert r["this_is_unknown_not_healthy"] is True


def test_a_branch_below_its_trigger_is_never_counted_as_past_it():
    r = ts.assess(hours_since_last_fill=10.0,
                  branches=[B("ZEC-USD", 1420.4, 1586.44)])
    assert r["verdict"] == "QUIET", r
    assert r["past_trigger"] == []


def test_an_empty_branch_cannot_be_past_a_sell_trigger():
    b = B("JASMY-USD", 0.0060, 0.0050); b["slices"] = []
    r = ts.assess(hours_since_last_fill=10.0, branches=[b])
    assert r["verdict"] == "QUIET", "a branch holding nothing has nothing to sell"


def test_garbage_does_not_crash_or_fabricate():
    r = ts.assess(hours_since_last_fill=10.0,
                  branches=[{"product_id": "X", "current_price": "oops",
                             "reference_price": None, "slices": [{"qty": 1}]}])
    assert r["verdict"] == "QUIET" and r["past_trigger"] == []


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


# --- past the trigger is only a fault if something could have been SOLD ---
#
# The trigger is branch-level (price vs the branch's re-anchoring
# reference); profitability is slice-level (price vs that slice's entry).
# On a branch that fell and re-anchored they come apart, and this check
# used to fault the engine for a refusal that was correct.

def Bs(pid, price, ref, slices, step=0.03):
    """slices: list of dicts, so a test can set entry and net per slice."""
    return {"product_id": pid, "current_price": price, "reference_price": ref,
            "grid_pct": step, "slices": slices}


def test_the_real_ondo_case_is_not_an_alarm():
    """Live 2026-10-04. ONDO read +1.70% past its trigger and the fleet
    reported BROKEN. Its reference had re-anchored to 0.47586 while its
    only slice was bought at 0.53524, so the sale it was faulted for not
    making would have LOST $0.98."""
    r = ts.assess(hours_since_last_fill=19.3, refusals_by_product={},
                  branches=[Bs("ONDO-USD", 0.49845, 0.47586,
                               [{"qty": 26.68, "entry_price": 0.53524,
                                 "unrealized_net_usd": -1.08}])])
    assert r["past_trigger"] == [], r
    assert r["verdict"] != "BROKEN", r
    assert r["alarm"] is False


def test_a_genuinely_sellable_slice_past_the_trigger_still_alarms():
    """The failure this check exists for must still fire."""
    r = ts.assess(hours_since_last_fill=10.0, refusals_by_product={},
                  branches=[Bs("QNT-USD", 291.84, 145.0,
                               [{"qty": 1, "entry_price": 145.0,
                                 "unrealized_net_usd": 140.0}])])
    assert r["verdict"] == "BROKEN" and r["alarm"] is True
    assert r["past_trigger"][0]["product_id"] == "QNT-USD"


def test_one_sellable_slice_among_losers_is_enough_to_alarm():
    r = ts.assess(hours_since_last_fill=10.0,
                  branches=[Bs("X-USD", 110.0, 100.0,
                               [{"qty": 1, "entry_price": 200.0,
                                 "unrealized_net_usd": -90.0},
                                {"qty": 1, "entry_price": 50.0,
                                 "unrealized_net_usd": 59.0}])])
    assert r["past_trigger"][0]["product_id"] == "X-USD"
    assert r["alarm"] is True


def test_a_slice_net_negative_despite_being_gross_positive_is_not_sellable(self=None):
    """Fees decide it, not the raw price. The engine's own net figure is
    what gets read, so a slice up on price but down after both fee legs
    does not count as a missed sale."""
    r = ts.assess(hours_since_last_fill=10.0,
                  branches=[Bs("X-USD", 110.0, 100.0,
                               [{"qty": 1, "entry_price": 109.9,
                                 "unrealized_net_usd": -0.4}])])
    assert r["past_trigger"] == []


def test_it_falls_back_to_gross_when_net_is_missing():
    below = ts.assess(hours_since_last_fill=10.0,
                      branches=[Bs("X-USD", 110.0, 100.0,
                                   [{"qty": 1, "entry_price": 200.0}])])
    assert below["past_trigger"] == [], "price under entry: nothing to sell"
    above = ts.assess(hours_since_last_fill=10.0,
                      branches=[Bs("X-USD", 110.0, 100.0,
                                   [{"qty": 1, "entry_price": 50.0}])])
    assert above["past_trigger"][0]["product_id"] == "X-USD"


def test_an_unreadable_slice_still_counts_because_it_fails_toward_alarming():
    """An unreadable slice is not evidence of health."""
    r = ts.assess(hours_since_last_fill=10.0,
                  branches=[Bs("X-USD", 110.0, 100.0, [{"qty": 1}])])
    assert r["past_trigger"][0]["product_id"] == "X-USD"
    assert r["alarm"] is True


def test_six_underwater_parked_branches_do_not_pin_the_alarm_on():
    """The state the fleet is actually in. None of these can sell at a
    profit, so none of them is a missed sale."""
    under = [Bs(p, 110.0, 100.0,
                [{"qty": 1, "entry_price": 500.0,
                  "unrealized_net_usd": -30.0}] * 3)
             for p in ("ZEC-USD", "XRP-USD", "XLM-USD",
                       "HBAR-USD", "BCH-USD", "LINK-USD")]
    r = ts.assess(hours_since_last_fill=19.3, refusals_by_product={},
                  branches=under)
    assert r["past_trigger"] == []
    assert r["verdict"] != "BROKEN", r
