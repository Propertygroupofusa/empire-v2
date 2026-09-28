"""Two enforcements of one 20% rule, measured against different books.

Live at 2026-09-28T10:0xZ the same two coins read:

                market value / whole account      cost basis / grid coin
    ZEC            $2,058.54 of $10,648.64          $2,341.45 of $7,551.94
                        19.33%                            31.00%
    XRP            $2,095.61 of $10,648.64          $2,270.78 of $7,551.94
                        19.68%                            30.07%

auto_trim asked the first question and said both were fine. The buy gate
asked the second and blocked both. One rule, one number, two answers
eleven points apart - the same bug class that let the trimmer sell ZEC
and XRP out from under live grid branches.

THE BOOK, chosen by the owner 2026-09-28: market value over the WHOLE
account, cash included. It is what the rule is for - a 50% drop in one
coin costing a fixed share of everything held - and it is the only one
of the two that can never cause a sale, because moving to it lowers
every reading rather than raising it.

THE ARITHMETIC CHANGES WITH THE BOOK, and this is the part that is easy
to get wrong. The old gate solved

    (basis + spend) / (fleet + spend) <= max_share

because on a coin-only book the money spent landed in both numerator and
denominator. On the account book it does NOT: buying coin with cash from
the same account moves dollars from the USD row to the coin row and
leaves the total where it was. So the denominator is fixed, and

    headroom = max_share * total - held

Carrying the old divisor over would have overstated the room by a fifth.
"""
import auto_trim
import concentration_gate as cg

# The live account, 46 assets, as /auto-trim reported it. The nine over
# $200 by name and the 37 smaller ones as one row.
#
# The first draft of this fixture listed only the seven largest, summing
# to $8,835.90, and then asserted shares computed against the real
# $10,648.64 - so ZEC read 23.3% here and 19.33% live, and the test
# failed the correct code. A book that claims to be the whole account
# has to BE the whole account; a truncated one measures every share
# against a denominator that does not exist.
BOOK = {"XRP": 2095.61, "ZEC": 2058.54, "USD": 1952.64, "BTC": 1461.97,
        "XLM": 634.37, "ETH": 344.89, "SHIB": 287.88, "NEAR": 261.76,
        "LTC": 202.03,
        # the 37 holdings under $200 each, which are just as much part of
        # the denominator as the nine above
        "TAIL": 1347.68}
TOTAL = round(sum(BOOK.values()), 2)


def test_the_fixture_is_the_whole_account_not_a_sample():
    """Guards the mistake that made this file fail correct code."""
    assert TOTAL == 10647.37
    assert abs(TOTAL - 10648.64) < 2.0, "within rounding of the live total"


# ── one book, one answer ─────────────────────────────────────────────────

def test_the_two_checks_now_agree_on_the_live_numbers():
    """The whole point. Both sides asked about the same coin, same book."""
    for asset, expect in (("ZEC", 19.33), ("XRP", 19.68)):
        gate = cg.share_pct(BOOK[asset], TOTAL)
        trim = auto_trim.share_pct(BOOK[asset], TOTAL)
        assert round(gate, 2) == round(trim, 2) == expect, (asset, gate, trim)


def test_auto_trim_does_not_carry_its_own_copy_of_the_formula():
    """Delegation, not duplication - a second implementation is how the
    two came to disagree in the first place."""
    assert auto_trim.excess_usd is cg.excess_usd
    assert auto_trim.share_pct is cg.share_pct


def test_there_is_exactly_one_ceiling_constant():
    from capital_velocity import MAX_SINGLE_COIN_SHARE
    assert cg.MAX_SINGLE_COIN_SHARE is MAX_SINGLE_COIN_SHARE
    assert auto_trim.LIMIT_PCT == MAX_SINGLE_COIN_SHARE * 100


# ── the arithmetic the book changes ──────────────────────────────────────

def test_a_buy_does_not_grow_the_denominator_on_this_book():
    """Cash is in the total, so cash becoming coin leaves the total put."""
    before = cg.share_pct(BOOK["XLM"], TOTAL)
    after = cg.share_pct(BOOK["XLM"] + 100.0, TOTAL)
    assert round(after - before, 6) == round(100.0 / TOTAL * 100.0, 6)


def test_headroom_is_a_plain_subtraction_not_the_coin_only_divisor():
    room = cg.headroom_usd(BOOK["XRP"], TOTAL)
    # The fixture's own arithmetic. NOT the $34.12 the live account read
    # a minute earlier - that figure belongs to a different total, and
    # asserting it here would make this test pass or fail on the market
    # rather than on the code.
    assert round(room, 2) == round(0.20 * TOTAL - BOOK["XRP"], 2) == 33.86
    # The coin-only formula would have said $42.33 - a fifth too much.
    assert round(room / 0.80, 2) == 42.33
    assert room < room / 0.80


def test_spending_the_headroom_lands_exactly_on_the_ceiling():
    room = cg.headroom_usd(BOOK["XRP"], TOTAL)
    assert round(cg.share_pct(BOOK["XRP"] + room, TOTAL), 6) == 20.0


def test_a_coin_over_the_line_has_no_headroom_and_never_negative():
    assert cg.headroom_usd(0.30 * TOTAL, TOTAL) == 0.0


# ── the verdict, on the account book ─────────────────────────────────────

def test_zec_and_xrp_are_allowed_on_this_book():
    """They were blocked at 31% and 30% on the coin-only book. Under 20%
    of the account, they are inside the owner's stated rule."""
    for asset in ("ZEC", "XRP"):
        ok, why = cg.concentration_verdict(asset + "-USD", BOOK, 10.0)
        assert ok is True, (asset, why)


def test_a_buy_that_would_cross_the_line_is_still_refused():
    ok, why = cg.concentration_verdict("XRP-USD", BOOK, 500.0)
    assert ok is False
    assert "20%" in why


def test_a_coin_already_over_takes_no_new_dollars():
    book = dict(BOOK, XRP=0.25 * TOTAL)
    ok, why = cg.concentration_verdict("XRP-USD", book, 5.0)
    assert ok is False
    assert "already" in why.lower()
    assert "Nothing is sold" in why


def test_the_gate_reads_product_ids_and_the_trimmer_reads_tickers():
    """Two callers, two key shapes, one book."""
    a, _ = cg.concentration_verdict("ZEC-USD", BOOK, 10.0)
    b, _ = cg.concentration_verdict("ZEC", BOOK, 10.0)
    assert a == b is True


# ── cash is in the denominator, and is never a position ──────────────────

def test_cash_counts_toward_the_total_every_share_is_measured_against():
    assert round(cg.share_pct(BOOK["USD"], TOTAL), 2) == 18.34
    without_cash = TOTAL - BOOK["USD"]
    assert cg.share_pct(BOOK["ZEC"], TOTAL) < cg.share_pct(BOOK["ZEC"], without_cash)


def test_nothing_may_be_bought_into_cash_itself():
    ok, why = cg.concentration_verdict("USD", BOOK, 100.0)
    assert ok is False
    assert "not a position" in why.lower()


# ── unknown is never a breach and never a zero ───────────────────────────

def test_an_unreadable_book_allows_rather_than_halting_trading():
    ok, why = cg.concentration_verdict("ZEC-USD", None, 10.0)
    assert ok is True
    assert "unreadable" in why.lower() or "not checked" in why.lower()


def test_an_unreadable_book_offers_no_headroom_figure():
    assert cg.headroom_usd(None, TOTAL) is None
    assert cg.headroom_usd(100.0, None) is None
    assert cg.share_pct(100.0, 0) is None


def test_an_empty_account_does_not_refuse_the_first_order():
    ok, why = cg.concentration_verdict("BTC-USD", {}, 50.0)
    assert ok is True
