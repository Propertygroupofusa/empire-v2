#!/usr/bin/env python3
"""The account owner's own standing rule, made enforceable: no single coin
may hold more than 20% of the fleet's real capital.

Why this file exists
--------------------
The rule was stated, and the number 0.20 was even written down (see
capital_velocity.MAX_SINGLE_COIN_SHARE), but nothing in the live buy path
ever consulted it. Measured 2026-09-28 against the real fleet:

    ZEC-USD   $2,341.45 of $7,193.46 real cost basis   32.6%
    XRP-USD   $2,240.54 of $7,193.46 real cost basis   31.1%

Two coins, 63.7% of every dollar the fleet has deployed, and between them
ZERO completed round trips in 26 days of trading. Over the same window the
whole fleet realized $32.53, essentially all of it on coins holding under
$200 each. That is what an unenforced ceiling costs.

A second number disagreed with it in the same repo -
fleet_capital_allocator.MAX_BRANCH_SHARE = 0.35 - which is the exact bug
class that keeps recurring here: two numbers that must agree, written in
two places, with nothing forcing them to match. This module imports the
one in capital_velocity rather than restating it, so there is only ever
one ceiling to change.

What it does and does not do
----------------------------
ONE-DIRECTIONAL, on purpose. This can only ever refuse a NEW buy into a
coin that is already over the ceiling. It never sells, never trims, never
resizes an existing position, and never relaxes any other threshold - so
it can never realize a loss on the owner's behalf, and it cannot deepen a
concentration it was built to stop.

FAILS OPEN, on purpose. If the fleet's cost basis cannot be read, the
verdict is ALLOW with a reason saying so. A gate that halts trading
whenever a database read hiccups is a worse failure than the one it
guards against - and silence must never be mistaken for a measurement.

THE BOOK
--------
ONE book, for the buy gate and the trimmer alike: MARKET VALUE over the
WHOLE ACCOUNT, cash included.

They used to disagree. auto_trim measured market value against the whole
account; this file measured cost basis against grid coin only. On
2026-09-28 the same two coins read:

    ZEC   $2,058.54 of $10,648.64 = 19.33%   |  $2,341.45 of $7,551.94 = 31.00%
    XRP   $2,095.61 of $10,648.64 = 19.68%   |  $2,270.78 of $7,551.94 = 30.07%

One rule, one number, two answers eleven points apart - the trimmer
saying fine while the gate blocked. That is the same bug class that let
the trimmer sell ZEC and XRP out from under live grid branches: two
subsystems enforcing one ceiling against different books.

The account book is what the rule is for. "One bad week in a position
this size moves the whole account" is a statement about market value as
a share of everything held, cash included - cash does not have bad
weeks, but it is unquestionably part of what a drop is measured against.
It is also the only one of the two that can never cause a sale, since
moving to it lowers every reading rather than raising it.

THE ARITHMETIC CHANGES WITH THE BOOK
------------------------------------
The old gate solved

    (basis + spend) / (fleet + spend) <= max_share

because on a coin-only book the money spent landed in BOTH numerator and
denominator. On the account book it does not: buying coin with cash from
the same account moves dollars from the USD row to the coin row and
leaves the total exactly where it was. The denominator is fixed, so

    headroom = max_share * total - held

Carrying the old divisor across would have overstated the room by a
fifth at a 20% ceiling.
"""

from capital_velocity import MAX_SINGLE_COIN_SHARE

__all__ = ["MAX_SINGLE_COIN_SHARE", "share_pct", "excess_usd", "headroom_usd",
           "coin_share", "coin_headroom_usd", "concentration_verdict",
           "book_from_holdings"]

# Assets that ARE money. A share of the account is measured against a
# total that includes them, and nothing is ever bought into or trimmed
# out of one. Imported, never restated - see account_census.STABLE.
from account_census import STABLE as CASH_EQUIVALENTS


def _num(v):
    """A FINITE float, or None. None means UNREADABLE and never 0.0.

    inf is rejected alongside nan. Caught by test_auto_trim's
    test_unreadable_holding_is_none[inf] when a first draft of this
    let infinity through: excess_usd(inf, 10_000) returned inf, and a
    caller sizing a trim from that would have asked the venue to sell
    an infinite position. An unreadable figure is not a huge one.
    """
    try:
        if v is None:
            return None
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _asset(key):
    return str(key or "").split("-")[0].upper()


# ── the one formula, in dollars, shared by both callers ──────────────────

def share_pct(holding_usd, total_usd):
    """This holding as a percentage of the account. None when unreadable.

    None is load-bearing: a total that failed to read is not the same as
    a position that is within its limit, and a caller that cannot tell
    them apart will report 0.00% on an unmeasured position.
    """
    u, t = _num(holding_usd), _num(total_usd)
    if u is None or t is None or t <= 0 or u < 0:
        return None
    return (u / t) * 100.0


def excess_usd(holding_usd, total_usd, limit_pct=MAX_SINGLE_COIN_SHARE * 100,
               buffer_pct=0.5):
    """Dollars to sell so the holding lands at (limit - buffer).

    0.0 when already at or under the limit; None when the inputs cannot
    be trusted. A caller must treat None as "do nothing", never as zero.

    Selling coin for cash on this book leaves the total where it is -
    the dollars move from the coin row to the USD row - so the target is
    a plain share of a fixed total.
    """
    share = share_pct(holding_usd, total_usd)
    lim = _num(limit_pct)
    if share is None or lim is None or lim <= 0 or lim > 100:
        return None
    if share <= lim:
        return 0.0
    buf = _num(buffer_pct) or 0.0
    target = max(lim - buf, 0.0)
    return max(_num(holding_usd) - _num(total_usd) * target / 100.0, 0.0)


def headroom_usd(holding_usd, total_usd, limit_pct=MAX_SINGLE_COIN_SHARE * 100):
    """The most that may be added without breaching the ceiling.

    0.0 when already at or over; None when unreadable. See the note in
    the module docstring on why this is a subtraction and not the
    coin-only divisor it replaced.
    """
    u, t = _num(holding_usd), _num(total_usd)
    lim = _num(limit_pct)
    if u is None or t is None or t <= 0 or lim is None or not (0 < lim <= 100):
        return None
    return max(0.0, t * lim / 100.0 - u)


# ── the book ─────────────────────────────────────────────────────────────

def book_from_holdings(holdings):
    """{ASSET: market value usd} from a census holdings list, or None.

    None on an unreadable reading, never {} - an empty book and an
    unreadable one lead to opposite decisions.
    """
    if holdings is None:
        return None
    out = {}
    for row in holdings:
        a = _asset((row or {}).get("asset"))
        usd = _num((row or {}).get("usd"))
        if a and usd is not None:
            out[a] = out.get(a, 0.0) + usd
    return out


def coin_share(product_id, book, spend_usd=0.0):
    """This coin's share (0-1) of the account AFTER a proposed spend.

    `book` is {ASSET: market value usd} INCLUDING cash - see the module
    docstring. Keys may be tickers or product ids; both resolve.

    Returns None when it cannot be computed. None means UNKNOWN, never
    0.0: a coin whose share could not be measured has not been measured
    at zero.

    The spend is added to the coin and NOT to the total, because the
    dollars come from the account's own cash, which the total already
    counts.
    """
    held = _book(book)
    if held is None:
        return None
    total = sum(held.values())
    if total <= 0:
        return None
    spend = max(0.0, _num(spend_usd) or 0.0)
    pct = share_pct(held.get(_asset(product_id), 0.0) + spend, total)
    return None if pct is None else pct / 100.0


def _book(book):
    """{ASSET: usd} from a book or a census holdings list. None if unreadable."""
    if book is None:
        return None
    if isinstance(book, (list, tuple)):
        return book_from_holdings(book)
    try:
        out = {}
        for k, v in dict(book).items():
            usd = _num(v)
            if usd is not None:
                a = _asset(k)
                out[a] = out.get(a, 0.0) + usd
        return out
    except (TypeError, ValueError):
        return None


def concentration_verdict(product_id, book, spend_usd,
                          max_share=MAX_SINGLE_COIN_SHARE):
    """(allow, reason) for buying spend_usd more of product_id.

    Judged on the share the coin would hold AFTER the buy, as market
    value over the whole account - the same book and the same arithmetic
    auto_trim uses to decide what is over the line. Before 2026-09-28
    these two asked different questions and gave answers eleven points
    apart on the same coin.
    """
    asset = _asset(product_id)
    cap = max_share * 100

    held = _book(book)
    if held is None:
        # FAIL CLOSED. This used to return True with the reasoning "unknown is
        # not a breach", and that reasoning is wrong for a BUY ceiling.
        #
        # The book goes unreadable exactly when Coinbase rate-limits, which is
        # exactly when the bot is busiest. Measured 2026-10-01 05:21Z:
        #   HTTP 429 fetching USD / HTTP 429 fetching USDC
        #   [GRID] account book unreadable - concentration not checked this cycle
        # Every buy in that window had NO ceiling at all. A 20% rule that
        # switches itself off under load is not a 20% rule.
        #
        # The two errors are not symmetric. Refusing a buy costs one cycle of
        # opportunity, and the grid buys dips - there is always another dip.
        # Allowing an unmeasured buy is unbounded, and unbounded is how 20%
        # became 26.6% on two coins carrying 89% of the account's losses.
        #
        # This also makes the gate agree with the rest of the repo, which
        # already refuses on an unreadable read rather than guessing:
        # coin_deploy refuses on unreadable cash, place_maker_sell refuses on
        # an unreadable balance, risk_governor's trading_approved requires
        # every field to be exactly True.
        #
        # Blast radius is bounded by the caller's cache: crypto_grid_bot holds
        # the book for max_age_seconds, so a transient 429 is served from
        # cache and only a SUSTAINED failure to read the account stops buying.
        # If that happens, buying should stop.
        return False, ("account book unreadable - REFUSING the buy. The "
                       "concentration ceiling cannot be measured, and an "
                       "unmeasured ceiling is not a ceiling. One missed buy "
                       "costs a cycle; an unbounded one is how a 20% rule "
                       "becomes 26%.")

    # Cash is the thing coin is bought WITH, not a position to hold a
    # share of. auto_trim refuses to trim it for the same reason.
    if asset in CASH_EQUIVALENTS:
        return False, (f"{asset} is the account's cash, not a position - there is "
                       f"nothing to buy here")

    total = sum(held.values())
    if total <= 0:
        # Nothing held at all: a ceiling is a share OF something, and
        # refusing here would mean the account could never place its
        # first order.
        return True, "the account holds nothing yet - no share to be over"

    now = share_pct(held.get(asset, 0.0), total)
    after = share_pct(held.get(asset, 0.0) + max(0.0, _num(spend_usd) or 0.0), total)
    if now is None or after is None:
        # Same rule, same reason as above: a share that cannot be computed is
        # not a share within the ceiling.
        return False, ("share could not be computed from this book - REFUSING "
                       "the buy rather than spending against a number that "
                       "could not be worked out")

    if after <= cap:
        return True, (f"{product_id} would be {after:.1f}% of the account after this "
                      f"buy, within the {cap:.0f}% ceiling")

    if now > cap:
        return False, (f"{product_id} already holds {now:.1f}% of the account, over the "
                       f"{cap:.0f}% ceiling - no new dollars go into it while it is "
                       f"over. Nothing is sold; it comes back under as the rest of the "
                       f"account grows or as its own slices sell at a profit.")
    return False, (f"this buy would take {product_id} from {now:.1f}% to {after:.1f}% "
                   f"of the account, through the {cap:.0f}% ceiling")


def coin_headroom_usd(product_id, book, max_share=MAX_SINGLE_COIN_SHARE):
    """The most that may be added to this coin. None when unreadable.

    Sizes a top-up to what is allowed rather than proposing a number the
    gate will refuse. concentration_verdict remains the thing that
    decides; this only says how much would pass.
    """
    held = _book(book)
    if held is None:
        return None
    total = sum(held.values())
    if total <= 0:
        return None
    room = headroom_usd(held.get(_asset(product_id), 0.0), total, max_share * 100)
    return None if room is None else round(room, 2)
