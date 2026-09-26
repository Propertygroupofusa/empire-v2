"""Sell a DOLLAR AMOUNT of a holding the bots do not track.

The account owner asked to sell $400 of ZEC. Nothing in this codebase
could do that, 2026-09-26:

  * POST /coinbase/sell   requires the symbol to be in
    crypto_coinbase_bot.open_crypto_positions - a position a bot opened.
    ZEC belongs to no branch, so it 404s.
  * POST /crypto/withdraw reads the FULL available balance and market-sells
    all of it. Fired on ZEC-USD it would have sold 1.82953266 ZEC -
    $2,822 - when $400 was asked for. Seven times the instruction.

So the sizing rules live here, as pure functions, because the failure
mode of this operation is selling someone's money and it must be
testable without placing an order.

Everything rounds DOWN, always. A partial sell that overshoots cannot be
undone except by buying back at a worse price and paying two more fees.
Selling $399.94 when $400 was asked is a rounding artifact; selling
$400.06 is selling more of someone's position than they authorised.
"""

from decimal import Decimal, ROUND_DOWN

# Sold with a market IOC order, so the fill price is not the quote price.
# Sizing against a quote that is already stale by the time the order lands
# is how an overshoot happens, so the base size is cut by this much first.
# 0.5% is wider than the spread on any pair this account holds and costs
# the seller a couple of dollars on a $400 sale - the right side to err on.
SLIPPAGE_HAIRCUT = 0.005


def _dec(v):
    return Decimal(str(v))


def round_down_to_increment(size, increment):
    """Truncate a base size onto the product's own grid, never up.

    Coinbase rejects a size off the increment grid outright, so a value
    that is merely rounded to 8dp can be refused for a pair quoted in
    whole units. Truncating also keeps the direction honest: the result
    is always <= what was asked.
    """
    inc = _dec(increment)
    if inc <= 0:
        raise ValueError(f"base_increment must be positive, got {increment}")
    steps = (_dec(size) / inc).to_integral_value(rounding=ROUND_DOWN)
    return steps * inc


def plan_sale(usd_target, price, units_held, *, base_increment="0.00000001",
              base_min_size=None, haircut=SLIPPAGE_HAIRCUT):
    """What to actually place, or why not to.

    Returns a dict carrying `ok`. When False, `reason` says what stopped
    it and NOTHING should be sent. When True, `base_size` is the string
    to put in the order and `est_usd` is what it is expected to raise.

    `units_held` is a hard ceiling: this never sells more of an asset than
    the account holds, whatever was asked for.
    """
    for name, v in (("usd_target", usd_target), ("price", price), ("units_held", units_held)):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return {"ok": False, "reason": f"{name} is not a number: {v!r}"}
        if v != v or v in (float("inf"), float("-inf")):
            return {"ok": False, "reason": f"{name} is not finite: {v!r}"}
        if v <= 0:
            return {"ok": False, "reason": f"{name} must be positive, got {v}"}

    usd_target = float(usd_target)
    price = float(price)
    units_held = float(units_held)
    held_usd = units_held * price

    capped_to_holding = False
    if usd_target > held_usd:
        capped_to_holding = True
        usd_target = held_usd

    # Haircut FIRST, then truncate onto the increment grid. Doing it the
    # other way lets the haircut push the size back off the grid.
    raw = (usd_target / price) * (1.0 - float(haircut))
    size = round_down_to_increment(raw, base_increment)

    if size <= 0:
        return {"ok": False,
                "reason": (f"${usd_target:,.2f} at ${price:,.8g} is {raw:.10f} units, which "
                           f"rounds to zero on a {base_increment} increment")}

    if size > _dec(units_held):
        # Cannot happen given the cap above, but this is a sell of real
        # money and the assertion is cheaper than the incident.
        return {"ok": False,
                "reason": f"computed size {size} exceeds the {units_held} held"}

    if base_min_size is not None and size < _dec(base_min_size):
        return {"ok": False,
                "reason": (f"{size} is below this product's {base_min_size} minimum size - "
                           f"raise the amount or sell the position another way")}

    est_usd = float(size) * price
    return {
        "ok": True,
        "base_size": format(size, "f"),
        "units": float(size),
        "est_usd": round(est_usd, 2),
        "price_used": price,
        "units_held": units_held,
        "held_usd": round(held_usd, 2),
        "pct_of_holding": round(float(size) / units_held * 100, 2),
        "capped_to_holding": capped_to_holding,
        "haircut_pct": round(float(haircut) * 100, 3),
        "note": (f"Sells {size} of {units_held} held ({float(size) / units_held * 100:.1f}%), "
                 f"raising about ${est_usd:,.2f}. Market IOC, so the fill can differ; the "
                 f"size is cut {float(haircut) * 100:.1f}% below the quote and truncated to "
                 f"the product increment, so it lands under the target rather than over."),
    }
