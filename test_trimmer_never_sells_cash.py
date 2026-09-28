"""The trimmer planned to sell dollars.

Live at 2026-09-28T09:24Z, AUTO_TRIM_MODE=arm:

    {"asset": "USD", "usd": 2139.97, "share_pct": 20.08, "act": true,
     "trim_usd": 61.62, "reason": "OVER_LIMIT", "kind": "TRIM",
     "detail": "20.08% of the account against a 20% rule;
                selling $61.62 brings it to about 19.5%"}

account_census.census() returns USD in `holdings` - correctly, it is
part of the account - and plan_trims iterates that list applying the
concentration ceiling to every row in it. Nothing told it that one of
those rows is the money itself.

Two things wrong with acting on that row:

  1. There is no order to place. auto_trim_worker builds the product id
     as f"{asset}-USD", so this becomes "USD-USD", which is not a
     product at any venue.

  2. The rule does not apply. The ceiling exists because "one bad week
     in a position this size moves the whole account". Dollars do not
     have bad weeks, and the proceeds of selling dollars are dollars -
     the share would not move.

The same holds for every cash equivalent: USDC, USDT, DAI, PYUSD, USDS.
account_census.STABLE is the one list that says which those are, and it
is imported rather than restated, because a second copy is how two
subsystems come to disagree.

A skipped row is still returned with its reason, like every other
refusal here, so the ceiling rule stays auditable for the times it
should have acted and did not.
"""
from datetime import datetime, timezone

import account_census
import auto_trim

NOW = datetime(2026, 9, 28, 9, 24, tzinfo=timezone.utc)


def H(asset, usd):
    return {"asset": asset, "usd": usd, "units": usd, "price": 1.0}


# The live book, rounded: USD over the ceiling, one coin under it.
BOOK = [H("USD", 2139.97), {"asset": "ZEC", "usd": 2066.66, "units": 1.33, "price": 1553.9}]
TOTAL = 10658.22


def test_the_ceiling_rule_does_not_plan_a_sale_of_dollars():
    rows = auto_trim.plan_trims(BOOK, TOTAL, now=NOW)
    usd = next(r for r in rows if r["asset"] == "USD")
    assert usd["act"] is False, (
        f"planned to sell ${usd.get('trim_usd')} of USD - there is no USD-USD product, "
        f"and the proceeds of selling dollars are dollars")
    assert usd["trim_usd"] == 0.0


def test_the_refusal_is_returned_with_a_reason_not_dropped():
    rows = auto_trim.plan_trims(BOOK, TOTAL, now=NOW)
    assert [r["asset"] for r in rows].count("USD") == 1, "the cash row vanished from the audit"
    usd = next(r for r in rows if r["asset"] == "USD")
    assert usd["reason"] == "NOT_A_POSITION"
    assert usd.get("detail")


def test_every_cash_equivalent_is_covered_not_just_the_dollar():
    for sym in sorted(account_census.STABLE):
        book = [H(sym, 5000.0), {"asset": "ZEC", "usd": 100.0, "units": 1.0, "price": 100.0}]
        rows = auto_trim.plan_trims(book, 10000.0, now=NOW)
        row = next(r for r in rows if r["asset"] == sym)
        assert row["act"] is False, f"{sym} is a cash equivalent and was planned for sale"
        assert row["reason"] == "NOT_A_POSITION"


def test_the_list_of_cash_equivalents_is_not_restated_here_or_there():
    """One source. A second copy is how two subsystems come to disagree."""
    src = open("auto_trim.py").read()
    assert "account_census" in src, "auto_trim must read STABLE from account_census"
    for sym in ("USDC", "USDT", "PYUSD", "USDS"):
        assert f'"{sym}"' not in src and f"'{sym}'" not in src, (
            f"{sym} is restated in auto_trim.py - import account_census.STABLE instead")


def test_a_real_coin_over_the_ceiling_is_still_trimmed():
    """One-directional: this can only ever cancel a sale, never cause one."""
    book = [H("USD", 100.0), {"asset": "ZEC", "usd": 5000.0, "units": 3.2, "price": 1562.5}]
    rows = auto_trim.plan_trims(book, 10000.0, now=NOW)
    zec = next(r for r in rows if r["asset"] == "ZEC")
    assert zec["act"] is True and zec["trim_usd"] > 0
    assert zec["reason"] == "OVER_LIMIT"


def test_cash_still_counts_toward_the_account_total_it_is_measured_against():
    """Excluded from the RULE, never from the TOTAL - a coin's share is a
    share of the whole account, cash included, or every share inflates."""
    book = [H("USD", 8000.0), {"asset": "ZEC", "usd": 2000.0, "units": 1.0, "price": 2000.0}]
    rows = auto_trim.plan_trims(book, 10000.0, now=NOW)
    zec = next(r for r in rows if r["asset"] == "ZEC")
    assert zec["share_pct"] == 20.0, (
        "ZEC is 20% of a $10,000 account that is mostly cash; if cash were dropped "
        "from the denominator it would read 100%")
    assert zec["act"] is False and zec["reason"] == "WITHIN_LIMIT"


def test_position_rules_shares_the_one_list_rather_than_copying_it():
    """It carried a copy under a comment claiming it was "kept identical to
    account_census.STABLE so the two cannot drift apart" - which nothing
    enforced. Three files now read one definition."""
    import position_rules
    assert position_rules.STABLE is account_census.STABLE
    src = open("position_rules.py").read()
    for sym in ("USDC", "USDT", "PYUSD", "USDS"):
        assert f'"{sym}"' not in src, f"{sym} is still restated in position_rules.py"
