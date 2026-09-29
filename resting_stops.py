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
              excluded=None, actively_traded=(), unstopped=None):
    """One resting stop, or why there will not be one.

    `stop_price` is the level holdings_watch already computed. Nothing is
    re-derived here: a second opinion about where the stop belongs is how
    the dashboard and the venue end up disagreeing about what is
    protected.

    `actively_traded` is the set of tickers a grid branch currently holds
    open slices on. Nothing rests on those - see the block below.

    `unstopped` is {TICKER: why} for branches with NO grid stop, from
    crypto_grid_bot.products_without_a_grid_stop(). It changes NO decision
    here: every refusal below refuses exactly the assets it always did, and
    this cannot cause a placement. What it changes is whether a refusal may
    claim the grid is covering the position, and whether the gap is counted.

    THE ASYMMETRY THAT MADE IT NECESSARY. The ACTIVELY_TRADED refusal said
    "The branch carries its own adaptive stop; this would be a second one the
    first cannot see." Right for most branches. For an adopted one - which
    names its own stop of 0 - it was the reassurance that hid the gap, while
    the branch pointed back here claiming its coin was "covered at the
    portfolio level by the resting stops". Live on 2026-09-29: eight
    branches, $3,712, and protects_usd 0.

    None (the default) means UNKNOWN, not "everything has a stop". The
    wording then falls back to what it always said and nothing is marked
    either way. An empty dict means the read succeeded and found no gaps.
    """
    _stops_unknown = unstopped is None
    _gap_why = (None if _stops_unknown
                else {str(k).upper(): v for k, v in unstopped.items()}.get(
                    str(asset).upper()))
    _no_grid_stop = _gap_why is not None

    def no(reason, detail):
        out = {"asset": asset, "ok": False, "reason": reason, "detail": detail}
        # ANY refusal on a branch with no grid stop is a coverage gap, not
        # only the ACTIVELY_TRADED one. That generality is load-bearing: ZEC,
        # the largest of them, is refused as TRIMMERS_COIN and never reaches
        # that block at all. The reason differs; the position is equally
        # naked, and a gap that only one refusal path can report is a gap
        # that hides behind the others.
        if _no_grid_stop:
            out["uncovered"] = True
            out["uncovered_detail"] = (
                f"NO STOP FROM EITHER LAYER: {_gap_why}, and this layer is not placing "
                f"one ({reason}). Nothing sells this position automatically at any "
                f"price.")
            u, p = _num(units_available), _num(price)
            out["position_usd"] = (round(u * p, 2)
                                   if u is not None and p is not None else None)
        elif _stops_unknown:
            # Said out loud rather than left absent. A caller that could not
            # read the grid's stops must not have this read as "the grid has
            # it" - the same rule that makes an unreadable balance never 0.
            out["uncovered"] = None
        return out

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
        # The three reasons above stand whatever the branch's stop is - they
        # are about held inventory, phantom units and not realising red, none
        # of which a stop elsewhere changes. So the REFUSAL is unconditional
        # and only its last sentence varies: the branch's own stop may be
        # cited as cover only when the branch actually has one.
        if _no_grid_stop:
            _second_stop = (f"AND THE BRANCH HAS NO STOP OF ITS OWN: {_gap_why}. So this "
                            f"position has no automatic exit from either layer - that is "
                            f"a gap to decide about, not a reason to rest an order here, "
                            f"because the three objections above are unchanged.")
        elif _stops_unknown:
            _second_stop = ("The branch normally carries its own adaptive stop; whether "
                            "THIS one does could not be read, so it is not being claimed.")
        else:
            _second_stop = ("The branch carries its own adaptive stop; this would be a "
                            "second one the first cannot see.")
        return no("ACTIVELY_TRADED",
                  f"{asset} has open grid slices on it. A resting sell would hold 75% "
                  f"of the units the grid trades with, and if it fired it would sell "
                  f"coin the branch still has on its books. {_second_stop}")

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

    # THE GAP AS A NUMBER, not as a reason buried in a refusal.
    #
    # protects_usd was the only coverage figure here, and it answers "how
    # much is this layer holding a stop under". It read 0 for a week while
    # every position it declined was being described as covered by a grid
    # stop that did not exist. A panel that reports what IS protected and
    # not what is protected by NOTHING lets the second number stay at zero
    # unnoticed, because zero is also what a healthy fleet reports.
    uncovered = [p for p in plans if p.get("uncovered") is True]
    unknown = [p for p in plans if p.get("uncovered") is None
               and "uncovered" in p]
    _sized = [p["position_usd"] for p in uncovered if p.get("position_usd") is not None]
    return {
        "mode": m,
        "armed": m == MODE_ARM,
        "would_place": len(ok),
        "protects_usd": round(sum(p["protects_usd"] for p in ok), 2),
        "worst_case_usd": round(sum(p["worst_case_usd"] for p in ok), 2),
        "coverage_pct": COVERAGE_PCT,
        "limit_band_pct": LIMIT_BAND_PCT,
        "excluded": sorted(excluded_assets()),
        # Assets with no stop from this layer AND none from the grid.
        "uncovered": sorted(p["asset"] for p in uncovered),
        "uncovered_count": len(uncovered),
        # None, never 0.0, when no uncovered position could be priced - a
        # $0.00 gap and an unpriced one are not the same claim.
        "uncovered_usd": round(sum(_sized), 2) if _sized else None,
        "uncovered_unpriced": sorted(p["asset"] for p in uncovered
                                     if p.get("position_usd") is None),
        # Assets whose grid stop could not be read at all. Not counted as
        # covered and not counted as a gap - reported as neither.
        "stop_coverage_unknown": sorted(p["asset"] for p in unknown),
        # EVERY asset unknown is a different statement from one asset unknown,
        # and it must not read the same. Live 2026-09-29 the dashboard forgot
        # to pass `unstopped` at all, plan_stop defaulted it to None, and all
        # 49 assets came back "unknown" - which looked like a legitimate read
        # failure rather than a call site missing an argument. The safety
        # default swallowed the wiring bug.
        "stop_coverage_unreadable": bool(plans) and len(unknown) == len(plans),
        "stop_coverage_unreadable_note": (
            "EVERY asset's grid stop read as unknown. One asset unknown is a gap in "
            "the data; ALL of them is the coverage map not arriving at all - check "
            "that the caller passes `unstopped` and that "
            "crypto_grid_bot.products_without_a_grid_stop() is not raising."
            if plans and len(unknown) == len(plans) else None),
        "uncovered_note": (
            "uncovered means NO automatic exit from either layer: the grid branch "
            "names a stop of 0 and this layer is not resting one either. It is a "
            "statement about coverage, not a recommendation - the reasons this layer "
            "declines (it would hold the grid's inventory, it could leave the branch "
            "tracking units the wallet no longer has, and it would schedule a loss on "
            "a position the fleet does not sell at a loss) all still apply."
            if uncovered else
            "Every position this layer declined has a grid stop of its own, or its "
            "stop could not be read - see stop_coverage_unknown."),
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
