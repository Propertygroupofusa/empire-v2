"""A coin subtotal nobody could price is UNKNOWN, not $0.00.

Measured on the live account 2026-09-30 03:22 local, /live-ops:

    Coinbase USD                                   $1,691.20
    Coinbase coins                                     $0.00
      excludes APE, TON, ONDO, ... BCH, BTC - could not be priced
    Verified cash across readable venues           $2,180.29
      every venue read successfully

All 55 holdings failed to price, the subtotal rendered as money, and the
line under it said every venue read successfully. At that moment the
fleet held $7,397.67 of coin. The page's own comment already stated the
rule it was breaking two rows further up: "It is never rendered as $0: a
reading that failed and a balance of zero are different facts."

capital_census had the rule too - "an unpriced holding is an unknown, and
quietly calling it $0 ... is how a balance stops meaning anything" - and
applied it per holding, then summed the refusals into a float and
returned it as a total.

Three things this pins:
  1. coin_usd is None when NOTHING could be priced, so no caller can
     print it as a dollar figure. A real zero (no coins held) stays 0.0.
  2. _spot_price says WHY it failed. Fifty-five identical silent Nones is
     what hid a rate limit; "HTTP 429 x55" names it.
  3. The gate tally counts CYCLE_ERROR. It read "errors 0" above a feed
     of TimeoutError, and a branch cycle that dies never reaches its buy
     or its sell - the throughput this page exists to show.
"""
import capital_census as cc


def _fake_prices(mapping):
    """Patch _spot_price: mapping product_id -> price, or a reason string."""
    def fake(pid):
        v = mapping.get(pid)
        return (None, v) if isinstance(v, str) else (float(v), None)
    return fake


def _holdings(mapping, USD=0.0, **coins):
    """price_holdings with _spot_price stubbed. USD is accepted and ignored:
    the cash leg is read by the caller, not by the function under test."""
    orig = cc._spot_price
    cc._spot_price = _fake_prices(mapping)
    try:
        return cc.price_holdings({c: float(a) for c, a in coins.items()})
    finally:
        cc._spot_price = orig


# --- 1. nothing priced -> UNKNOWN, never 0.00 ----------------------------

def test_no_holding_priced_returns_none_not_zero():
    v = _holdings({"ZEC-USD": "HTTP 429", "XRP-USD": "HTTP 429"},
                  USD=1691.20, ZEC=1.5, XRP=1500.0)
    assert v["coin_usd"] is None, f"coin_usd was {v['coin_usd']!r}, must be None"
    assert v["coin_usd_readable"] is False
    assert v["coin_priced_count"] == 0
    assert v["coin_holdings_count"] == 2
    assert v["unpriced"] == ["ZEC", "XRP"], v["unpriced"]


def test_the_reason_survives_so_a_rate_limit_is_nameable():
    v = _holdings({"ZEC-USD": "HTTP 429", "XRP-USD": "HTTP 429",
                   "BTC-USD": "TimeoutError"},
                  USD=1.0, ZEC=1.0, XRP=1.0, BTC=1.0)
    assert v["unpriced_reasons"] == {"HTTP 429": 2, "TimeoutError": 1}, \
        v["unpriced_reasons"]
    assert "HTTP 429" in v["note"] and "UNKNOWN" in v["note"]


def test_spot_price_returns_a_reason_not_a_bare_none():
    got = cc._spot_price("DEFINITELY-NOT-A-REAL-PRODUCT-XYZ")
    assert isinstance(got, tuple) and len(got) == 2, f"got {got!r}"
    px, why = got
    assert px is None and why, "a failure must carry its reason"


# --- 2. a REAL zero is still a zero --------------------------------------

def test_no_coins_at_all_is_a_readable_zero():
    v = _holdings({}, USD=500.0)
    assert v["coin_usd"] == 0.0, "no coins held is a genuine $0.00"
    assert v["coin_usd_readable"] is True
    assert v["coin_holdings_count"] == 0


# --- 3. partial coverage keeps the number AND states the gap -------------

def test_partial_pricing_keeps_the_total_and_names_the_shortfall():
    v = _holdings({"ZEC-USD": 100.0, "XRP-USD": "HTTP 429"},
                  USD=0.0, ZEC=2.0, XRP=1000.0)
    assert v["coin_usd"] == 200.0, v["coin_usd"]
    assert v["coin_usd_readable"] is True
    assert v["coin_priced_count"] == 1 and v["coin_holdings_count"] == 2
    assert "1 of 2" in v["note"], v["note"]


# --- 4. the text report must not print a fabricated dollar figure --------

def test_build_report_prints_unknown_not_a_dollar_total():
    v = dict(_holdings({"ZEC-USD": "HTTP 429"}, ZEC=1.5),
             venue="Coinbase", status="OK", usd_cash=1691.20)
    txt = cc.build_report({"venues": [v], "phantom_capital": {},
                           "verified_usd_cash": 1691.20, "venues_unknown": []})
    coins = [l for l in txt.splitlines() if "coins held" in l]
    total = [l for l in txt.splitlines() if "venue total" in l]
    assert coins and "UNKNOWN" in coins[0] and "$0.00" not in coins[0], coins
    assert total and "UNKNOWN" in total[0], total


# --- 5. the page must not render it as money either ----------------------

_HTML = open("live_ops_dashboard.html").read()


def test_page_branches_on_readability_before_formatting_money():
    assert "v.coin_usd === null || v.coin_usd_readable === false" in _HTML, \
        "the coin row no longer checks whether the subtotal is readable"
    assert "none of ' + (held || 0) + ' holdings could be priced" in _HTML


def test_page_does_not_call_a_cash_only_total_a_clean_bill_of_health():
    assert "this is NOT the account total" in _HTML, \
        "the verified-cash note can still read as if coin were covered"
    assert "every venue read successfully'" not in _HTML.replace(
        "cash only — every venue read successfully'", ""), \
        "an unqualified success note survives somewhere"


# --- 6. the tally that read 'errors 0' during a timeout storm ------------

def test_error_chip_counts_cycle_errors():
    assert "(t.GATE_ERROR||0) + (t.CYCLE_ERROR||0)" in _HTML, \
        "CYCLE_ERROR is collected into this feed and counted by nothing"


def test_a_stuck_exit_gets_its_own_visible_number():
    assert "t.PARKED_SELL_NOFILL||0" in _HTML
    assert "exit didn't fill" in _HTML


def test_cycle_error_is_actually_in_the_feed_the_chip_reads():
    src = open("routers/trading_dashboard.py").read()
    i = src.index("LIVE_OPS_GATE_EVENTS = (")
    block = src[i:src.index(")", src.index("PARKED_SELL_NOFILL", i))]
    for ev in ("CYCLE_ERROR", "PARKED_SELL_NOFILL"):
        assert f'"{ev}"' in block, f"{ev} left the feed; the chip would read 0 forever"


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
