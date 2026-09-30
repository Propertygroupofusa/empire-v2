"""Which coins can actually fill as maker, and which only think they can.

THE BUG THIS EXISTS FOR

expected_leg_fee_rate() returns the MAKER rate whenever maker-only is on
- for every coin, with no check on whether that coin's book can actually
give a maker fill. universe_scan already knows better, and says so on
the dashboard:

    ACH    $52,080/day    FAILS the $750,000 depth floor
    FLOKI  $114,810/day   FAILS
    TIA    $395,209/day   FAILS
    SHIB   $471,080/day   FAILS
    BONK   $744,989/day   FAILS - by eleven thousand dollars

An order on a book that thin crosses the spread and pays TAKER. So five
live branches have every sell priced at 0.35% a leg while paying 0.75%,
and that number is not cosmetic: _pick_profitable_slice_to_sell uses it
to decide whether a sale would net a profit. Understating the exit leg by
0.40 points green-lights sales that net between -0.40% and zero - real
losses booked as wins, which is the precise failure that function was
written to prevent.

HOW THIS ERRS

Only upward. A coin known to be below the floor is priced at taker; a
coin known to be above keeps the maker rate; a coin nothing is known
about keeps today's behaviour and is REPORTED as unknown rather than
quietly assumed good. The rate this produces is never lower than the one
without it, so the worst case is a bot slightly too reluctant to sell -
and the codebase's own rule is that a number gating real money must
never fail optimistic.

WHAT IT IS NOT

Not a liquidity model. Depth changes through the day and a coin that
clears the floor on average can still be thin at 3am. This answers one
narrow question - is this book routinely deep enough that a resting
order gets filled rather than crossing - and answers it from the same
figure and the same floor universe_scan and coin_rotation already use.
"""
from __future__ import annotations

# The same floor universe_scan and coin_rotation use. Below this, a
# resting order does not get the maker fill it is priced for.
MIN_24H_NOTIONAL_USD = 750_000.0

# Cached verdicts, keyed by the coin symbol. Populated by whatever has
# just measured depth; never fetched from here, because this module is
# called from the trading loop and must not reach a venue.
_KNOWN: dict = {}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def _coin(product_id):
    return str(product_id or "").split("-")[0].upper() or None


def can_fill_as_maker(notional_24h_usd, floor=MIN_24H_NOTIONAL_USD):
    """True, False, or None when the depth is unknown.

    None is a distinct answer on purpose. "We have not measured this
    book" and "this book is fine" are different facts, and collapsing
    them is how the maker rate got applied to a $52,080 order book.
    """
    n = _num(notional_24h_usd)
    if n is None or n < 0:
        return None
    return n >= float(floor)


def remember(product_id, notional_24h_usd, floor=MIN_24H_NOTIONAL_USD):
    """Record what a depth measurement found, for the trading loop to read."""
    c = _coin(product_id)
    if not c:
        return None
    verdict = can_fill_as_maker(notional_24h_usd, floor)
    _KNOWN[c] = {"maker_ok": verdict, "notional_24h_usd": _num(notional_24h_usd)}
    return verdict


def remember_many(rows, floor=MIN_24H_NOTIONAL_USD):
    """Record a whole scan. Rows carry a product_id/coin and a notional."""
    n = 0
    for r in rows or ():
        if not hasattr(r, "get"):
            continue
        pid = r.get("product_id") or r.get("coin") or r.get("asset")
        notional = (r.get("notional_24h_usd") if r.get("notional_24h_usd") is not None
                    else r.get("notional") if r.get("notional") is not None
                    else r.get("depth_usd"))
        if pid is not None and remember(pid, notional, floor) is not None:
            n += 1
    return n


def known(product_id):
    """What is known about this coin's book, or None."""
    return _KNOWN.get(_coin(product_id))


def maker_ok(product_id):
    """True / False / None - never a guess."""
    rec = known(product_id)
    return rec["maker_ok"] if rec else None


def thin_coins():
    """Every coin measured and found below the floor."""
    return sorted(c for c, r in _KNOWN.items() if r.get("maker_ok") is False)


def leg_fee_rate(product_id, maker_rate, taker_rate, *, maker_only_active=True):
    """The rate this coin's next leg should be priced at.

    Never returns less than the caller's own maker rate, so wiring this
    in can only raise a fee, never lower one. A coin nothing is known
    about keeps today's behaviour exactly.
    """
    m, t = _num(maker_rate), _num(taker_rate)
    if m is None:
        return t
    if t is None or not maker_only_active:
        return m
    return t if maker_ok(product_id) is False else m


# MEASURED FILL BEHAVIOUR, WHICH BEATS THE DEPTH PROXY WHEREVER IT EXISTS.
#
# The docstring above asserts "an order on a book that thin crosses the
# spread and pays TAKER" and names ACH, FLOKI, TIA and SHIB as examples.
# /grid-status/fee-reality has now measured the actual fills on those exact
# coins, and the assertion does not survive contact with them:
#
#   FLOKI   19 maker /  0 taker = 100.0% maker   at $44,999/day of depth
#   PRIME   18 maker /  0 taker = 100.0% maker
#   ALGO     7 maker /  0 taker = 100.0% maker
#   APE      7 maker /  0 taker = 100.0% maker
#   JASMY    3 maker /  0 taker = 100.0% maker
#   TIA     18 maker /  4 taker =  81.8% maker
#   SHIB    12 maker /  5 taker =  70.6% maker
#   ACH      2 maker /  1 taker =  66.7% maker
#
# Across the whole fleet the two groups are indistinguishable:
#   below the floor  88 maker / 12 taker = 88.0% maker
#   above the floor 119 maker / 15 taker = 88.8% maker
#
# And the screen is ANTI-CORRELATED with real taker exposure: the worst
# maker rates in the fleet - XYO 0%, ZEC 20%, XRP 28.6% - all CLEAR the
# floor. Those are the trimmer crossing the book, which depth cannot see.
#
# The cost is not hypothetical. expected_leg_fee_rate() feeds
# _pick_profitable_slice_to_sell; pricing FLOKI's exit at 0.75% when it
# really pays 0.35% overstates the cost of selling by 0.40 points and makes
# the fleet refuse exits that are genuinely profitable. The docstring's
# "worst case is a bot slightly too reluctant to sell" IS the live cost,
# and it lands on the branches that fill best.
#
# This records the measurement beside the verdict. It does NOT change
# pricing - repricing exits is a live trading decision and is the owner's.
_MEASURED_FILLS = {}


def record_measured_fills(product_id: str, maker: int, taker: int):
    """Store observed maker/taker counts for a coin. Names only, no rates."""
    if maker is None or taker is None or (maker + taker) <= 0:
        return
    _MEASURED_FILLS[product_id.split("-")[0].upper()] = {
        "maker": int(maker), "taker": int(taker),
        "maker_pct": round(100.0 * maker / (maker + taker), 1),
        "fills": int(maker + taker),
    }


def contradicted_by_fills():
    """Coins the depth screen calls taker-payers that measurably fill maker.

    A coin is only listed when it has BOTH a thin verdict and real fills, so
    an unmeasured coin never appears - absence here is not evidence either way.
    """
    out = {}
    for coin in thin_coins():
        obs = _MEASURED_FILLS.get(coin)
        if obs and obs["maker_pct"] >= 50.0:
            out[coin] = obs
    return out


def summarise():
    """What is known, for a panel that must not overstate its own coverage."""
    thin = thin_coins()
    contradicted = contradicted_by_fills()
    deep = sorted(c for c, r in _KNOWN.items() if r.get("maker_ok") is True)
    return {
        "measured": len(_KNOWN),
        "deep_enough_for_maker": deep,
        "too_thin_pays_taker": thin,
        "floor_usd": MIN_24H_NOTIONAL_USD,
        "detail": (
            (f"{len(thin)} coin(s) sit below the ${MIN_24H_NOTIONAL_USD:,.0f} depth floor and "
             f"pay TAKER on every leg: {', '.join(thin)}. Their sells were being priced at "
             f"the maker rate, which understates the exit leg and green-lights sales that "
             f"net between -0.40% and zero."
             if thin else
             f"Every measured coin clears the ${MIN_24H_NOTIONAL_USD:,.0f} floor.")
            + (f" {len(_KNOWN)} coin(s) measured; anything unmeasured keeps today's rate and "
               f"is reported as unknown rather than assumed fine." if _KNOWN else
               " Nothing measured yet, so nothing is being repriced.")),
        "contradicted_by_measured_fills": contradicted,
        "depth_is_a_proxy_not_a_measurement": (
            (f"{len(contradicted)} coin(s) called taker-payers by the depth floor "
             f"actually fill as maker most of the time: "
             + "; ".join(f"{c} {o['maker_pct']}% maker over {o['fills']} fills"
                         for c, o in sorted(contradicted.items()))
             + ". Depth predicts whether a resting order SHOULD fill; the fills "
               "say whether it DID. Where they disagree the fills win, and "
               "pricing these exits at taker makes the fleet refuse sales that "
               "are genuinely profitable."
             ) if contradicted else
            ("No thin coin has measured fills contradicting it. That is not a "
             "clean bill of health - it may simply mean nothing has been "
             "measured yet.")),
        "this_does_not_change_pricing": (
            "recorded beside the verdict, not applied to it. Repricing exits is "
            "a live trading decision and is not taken here."),
    }
