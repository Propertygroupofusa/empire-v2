"""Stop orders that actually rest at the venue, and what that costs.

WHY THIS IS NOT JUST "PLACE THE LEVEL WE ALREADY COMPUTE"

holdings_watch has produced stop LEVELS for every coin since it was
written, and its own disclaimer is exact: "These are ALERT LEVELS, not
stop-loss orders. Nothing here rests at the exchange and nothing here
will sell." Turning a level into a resting order changes three things,
and each one can cost money in a way the alert version cannot.

1. A RESTING SELL PUTS THE COINS ON HOLD.

   Coinbase reserves the base currency of an open sell order. Those units
   stop being available - and auto_trim, sell_amount and every manual
   sale size against AVAILABLE, deliberately, because sizing against held
   units produces an order the venue rejects. So covering 100% of a
   position with a resting stop silently disables every other way of
   selling it, including the concentration trimmer that just fired.

   That is why COVERAGE_PCT exists and defaults below 100. The uncovered
   remainder is not an oversight; it is the part the rest of the system
   can still act on.

2. COINBASE ADVANCED TRADE HAS NO STOP-MARKET, ONLY STOP-LIMIT.

   A stop-limit has two prices: the stop that wakes it and the limit it
   then works. If price gaps straight through the limit, the order sits
   unfilled and the protection did not happen - in exactly the fast fall
   it was bought for. ZEC fell 61.5% in five days this May; a limit one
   tick under the stop would very likely have been jumped.

   So the limit is set a deliberate band BELOW the stop. That band is the
   worst fill accepted in exchange for the order actually working, and
   widening it is not "worse pricing", it is buying a higher chance of
   being filled at all. Neither end of that trade is free and the numbers
   are reported rather than buried.

3. A RESTING ORDER IS STATIC. THE LEVELS ARE TRAILING.

   holdings_watch recomputes each level from the running peak on every
   call, so it rises as the coin rises. An order at the venue does not.
   Keeping them in step means cancel-and-replace, and the ratchet rule is
   absolute: a stop may move UP and never down. Volatility rising would
   otherwise widen the computed stop and quietly lower a protection level
   that was already in place, which is the one direction that must never
   happen automatically.

WHAT THIS MODULE DOES NOT DO

It does not place, cancel or amend anything. It returns plans and
refusals. Everything that touches the venue lives in the worker, so the
arithmetic here can be tested without an account.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN

# How much of a position a resting stop may cover. The remainder stays
# available so the trimmer and any manual sale still have something to
# work with.
COVERAGE_PCT = 75.0

# How far under the stop the limit sits. 1.5% is wider than one candle on
# every coin in this account and is the price of the order filling at all
# in a gap.
LIMIT_BAND_PCT = 1.5

# A stop this close to the market is noise, not protection - it fires on
# an ordinary tick and pays a round trip for it.
MIN_DISTANCE_PCT = 2.0

# Below this the venue rejects the order or the fee eats the protection.
MIN_STOP_USD = 25.0

# Replace an existing stop only when the new level is meaningfully higher.
# Cancel/replace is two requests and a window where nothing is resting.
REPLACE_MOVE_PCT = 1.0

MODE_ENV = "RESTING_STOPS_MODE"
MODE_OBSERVE, MODE_ARM = "observe", "arm"

# Coins the concentration trimmer owns. A resting stop holds the units it
# covers, and the trimmer sizes against what is AVAILABLE - so resting a
# stop on a coin the trimmer is managing takes away the very units it
# needs. Two protections, one pile of coins; the trimmer gets first claim
# because a coin over the ceiling is the one that can end the account, and
# a stop that cannot be acted on is not protection.
#
# ZEC is here by the account owner's decision: the trimmer has it.
EXCLUDE_ENV = "RESTING_STOPS_EXCLUDE"
DEFAULT_EXCLUDED = ("ZEC",)

# And the same idea as a rule rather than a list, so a coin that drifts up
# into the trimmer's territory is excluded without anyone remembering to
# add it. Anything within this many points of the concentration limit is
# the trimmer's.
NEAR_LIMIT_BAND_PCT = 3.0


def excluded_assets(raw=None):
    """The explicit exclusion list. Unset means the default.

    An empty string means "exclude nothing" and is honoured, because the
    alternative - treating empty as unset - makes it impossible to turn
    the default off.
    """
    import os
    if raw is None:
        raw = os.getenv(EXCLUDE_ENV)
    if raw is None:
        return set(DEFAULT_EXCLUDED)
    return {a.strip().upper() for a in str(raw).split(",") if a.strip()}


def is_trimmers(asset, share_pct, limit_pct, *, excluded=None,
                band_pct=NEAR_LIMIT_BAND_PCT):
    """Should the trimmer keep these units? (asset, reason) or (False, None)."""
    a = (asset or "").upper()
    ex = excluded_assets() if excluded is None else excluded
    if a in ex:
        return True, f"{a} is managed by the concentration trimmer, which needs its units free"
    sp = _num(share_pct)
    lp = _num(limit_pct)
    if sp is not None and lp is not None and sp >= lp - (_num(band_pct) or 0):
        return True, (f"{a} is {sp:.2f}% of the account against a {lp:.0f}% limit - "
                      f"close enough that the trimmer will want these units")
    return False, None


def normalise_mode(value) -> str:
    """Only "arm" arms it, ignoring case and surrounding whitespace.

    " Arm " arms it; "true", "yes", "1", "armed" and None do not. The
    whitespace and case exception is deliberate - a trailing space on a
    Railway variable is an accident, not a different intention, and a
    guard that silently fails to arm on an invisible character leaves the
    operator believing protection is running when it is not.
    """
    if not isinstance(value, str):
        return MODE_OBSERVE
    return MODE_ARM if value.strip().lower() == MODE_ARM else MODE_OBSERVE


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def round_down(value, increment):
    inc = Decimal(str(increment))
    if inc <= 0:
        raise ValueError(f"increment must be positive, got {increment}")
    return (Decimal(str(value)) / inc).to_integral_value(rounding=ROUND_DOWN) * inc


def round_price(value, increment):
    """Prices round DOWN too, for a sell stop.

    Rounding a sell's trigger up moves protection closer to the market and
    can put it above it. Down is always the safe direction here.
    """
    return round_down(value, increment)


def plan_stop(asset, *, units_available, price, stop_price,
              base_increment="0.00000001", quote_increment="0.01",
              base_min_size=None, coverage_pct=COVERAGE_PCT,
              limit_band_pct=LIMIT_BAND_PCT, min_distance_pct=MIN_DISTANCE_PCT,
              min_stop_usd=MIN_STOP_USD, share_pct=None, limit_pct=None,
              excluded=None, actively_traded=()):
    """One resting stop, or why there will not be one.

    `stop_price` is the level holdings_watch already computed. Nothing is
    re-derived here: a second opinion about where the stop belongs is how
    the dashboard and the venue end up disagreeing about what is
    protected.

    `actively_traded` is the set of tickers a grid branch currently holds
    open slices on. Nothing rests on those - see the block below.
    """
    def no(reason, detail):
        return {"asset": asset, "ok": False, "reason": reason, "detail": detail}

    mine, why = is_trimmers(asset, share_pct, limit_pct, excluded=excluded)
    if mine:
        return no("TRIMMERS_COIN", why)

    # SECOND SELLER, SAME MISTAKE.
    #
    # The concentration trimmer sold $885.43 of ZEC and $244.78 of XRP out
    # from under live grid branches before plan_trims was given this rule.
    # This file is the other seller and never had it: on 2026-09-28 it was
    # placing stops on XLM, NEAR, LINK, ALGO, SOL and ACH, five of which
    # were live branches, with ZEC excluded only because it belonged to
    # the trimmer. It knew about the trimmer and not about the grid.
    #
    # A resting sell holds 75% of the position, so the grid cannot trade
    # its own inventory; and if it fires it sells that 75% in one order
    # while the branch goes on tracking it slice by slice, leaving the
    # branch claiming units the wallet no longer holds. That is how ETH
    # came to be short 0.0316 units.
    #
    # It also cannot be squared with the standing rule that nothing red is
    # realised - a stop under an underwater grid position is a scheduled
    # loss on a position that is not to be sold at a loss.
    #
    # Checked before any reason that could let it through. ONE-DIRECTIONAL:
    # it can only ever cancel a placement, never cause one. And it FAILS
    # OPEN - an empty or unreadable set leaves behaviour exactly as it was,
    # because a protection that disappears on a read error is worse than
    # one that was never there.
    if str(asset).upper() in {str(a).upper() for a in (actively_traded or ())}:
        return no("ACTIVELY_TRADED",
                  f"{asset} has open grid slices on it. A resting sell would hold 75% "
                  f"of the units the grid trades with, and if it fired it would sell "
                  f"coin the branch still has on its books. The branch carries its own "
                  f"adaptive stop; this would be a second one the first cannot see.")

    u = _num(units_available)
    p = _num(price)
    sp = _num(stop_price)
    if u is None or u <= 0:
        return no("NOTHING_AVAILABLE",
                  "no unheld units - an existing resting order may already hold them")
    if p is None or p <= 0:
        return no("UNPRICED", "no live price, so the distance to the stop is unknown")
    if sp is None or sp <= 0:
        return no("NO_LEVEL", "holdings_watch has no stop level for this asset")

    # THE ONE THAT MUST NEVER SLIP THROUGH.
    #
    # A sell stop at or above the market triggers the instant it is
    # accepted, which turns "protect this position" into "liquidate this
    # position now, at a limit price below the market". It is the single
    # most expensive mistake available in this file.
    if sp >= p:
        return no("STOP_ABOVE_MARKET",
                  f"stop {sp:,.8g} is at or above the market {p:,.8g} - this would "
                  f"fire immediately and sell the position, not protect it")

    distance = (p - sp) / p * 100.0
    if distance < (_num(min_distance_pct) or 0):
        return no("TOO_CLOSE",
                  f"{distance:.2f}% below the market is inside ordinary noise; it would "
                  f"fire on a normal tick and pay a round trip for nothing")

    cov = max(0.0, min(_num(coverage_pct) or 0.0, 100.0))
    size = round_down(u * cov / 100.0, base_increment)
    if size <= 0:
        return no("SIZE_ROUNDS_TO_ZERO",
                  f"{cov:.0f}% of {u} rounds to zero on a {base_increment} increment")
    if base_min_size is not None and size < Decimal(str(base_min_size)):
        return no("BELOW_MIN_SIZE",
                  f"{size} is under the product's {base_min_size} minimum")

    value = float(size) * sp
    if value < (_num(min_stop_usd) or 0):
        return no("TOO_SMALL",
                  f"${value:,.2f} protected is under the ${min_stop_usd:,.2f} floor - "
                  f"the fee would be a meaningful share of what is being saved")

    band = max(_num(limit_band_pct) or 0.0, 0.0)
    limit = round_price(sp * (1.0 - band / 100.0), quote_increment)
    if float(limit) <= 0:
        return no("LIMIT_ROUNDS_TO_ZERO",
                  f"a {band}% band under {sp:,.8g} rounds to zero on {quote_increment}")

    return {
        "asset": asset,
        "ok": True,
        "base_size": format(size, "f"),
        "units": float(size),
        "stop_price": format(round_price(sp, quote_increment), "f"),
        "limit_price": format(limit, "f"),
        "market_price": p,
        "distance_pct": round(distance, 2),
        "limit_band_pct": band,
        "coverage_pct": cov,
        "units_left_available": round(u - float(size), 10),
        "protects_usd": round(value, 2),
        "worst_case_usd": round(float(size) * float(limit), 2),
        "order_configuration": {
            "stop_limit_stop_limit_gtc": {
                "base_size": format(size, "f"),
                "limit_price": format(limit, "f"),
                "stop_price": format(round_price(sp, quote_increment), "f"),
                "stop_direction": "STOP_DIRECTION_STOP_DOWN",
            }
        },
        "note": (f"Rests at the venue. Wakes if {asset} trades at or below "
                 f"{sp:,.8g} ({distance:.2f}% below the market), then works a limit at "
                 f"{float(limit):,.8g}. A gap straight through that limit leaves it "
                 f"unfilled - this reduces the loss, it does not cap it."),
    }


def needs_replacement(existing_stop_price, new_stop_price, move_pct=REPLACE_MOVE_PCT):
    """Should a resting stop be cancelled and re-placed higher?

    The ratchet is the point. A trailing level recomputed from a running
    peak can FALL when volatility rises, and silently lowering protection
    that is already at the venue is the one move that must never happen
    on its own. So this returns True only for a meaningful move UP.
    """
    old = _num(existing_stop_price)
    new = _num(new_stop_price)
    if old is None or new is None or old <= 0 or new <= 0:
        return False, "one of the levels is unreadable"
    if new <= old:
        return False, ("the new level is not higher - a resting stop ratchets up and "
                       "never down, so this is left where it is")
    move = (new - old) / old * 100.0
    if move < (_num(move_pct) or 0):
        return False, (f"only {move:.2f}% higher; replacing costs two requests and a "
                       f"window with nothing resting")
    return True, f"{move:.2f}% higher, worth the replace"


def summarise(plans, mode):
    ok = [p for p in plans if p.get("ok")]
    m = normalise_mode(mode)
    return {
        "mode": m,
        "armed": m == MODE_ARM,
        "would_place": len(ok),
        "protects_usd": round(sum(p["protects_usd"] for p in ok), 2),
        "worst_case_usd": round(sum(p["worst_case_usd"] for p in ok), 2),
        "coverage_pct": COVERAGE_PCT,
        "limit_band_pct": LIMIT_BAND_PCT,
        "excluded": sorted(excluded_assets()),
        "plans": plans,
        "what_it_does_not_do": (
            "A stop-limit is not a guarantee. If price gaps through the limit the "
            "order does not fill and the position is still held. Coinbase Advanced "
            "Trade has no stop-market, so this is the strongest available form."),
        "the_cost": (
            f"Resting sells hold the coins. {COVERAGE_PCT:.0f}% of each position "
            f"becomes unavailable to the trimmer and to you until the order is "
            f"cancelled."),
    }
