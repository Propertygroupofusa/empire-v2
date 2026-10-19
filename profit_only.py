"""THE OWNER'S PROFIT-ONLY RULE: nothing sells below what it cost plus Coinbase's fees.

The owner, 2026-10-19: "Only buy and sell at a gain and profit. Make sure that is
stamped in all of the bots ... the fees for Coinbase and everything, as you know
already, need to be put in there. And once that happens, then you know that you have
profit after that." Before that, in their words: "Do not sell anything that I have
as a loss" and "we're not selling nothing at no loss."

There is no switch for it. A setting that could turn it off is how the stop-loss came
back after GRID_SELL_ONLY was switched off: on 2026-10-07/08 nine stop-loss sales booked
-$50.50. So the rule is code, and every Coinbase seller asks it before an order:

  * the grid's sale step - the stop-loss sale is off for good, and the slice picked
    must still net above zero, both fees paid, at the price it is sold at;
  * the grid's forced close (close-branch, rotation) - every slice in it must clear;
  * the family tree, the mean-reversion bot and the legacy bot - their positions
    carry an entry price, so a sale that would not clear it is refused;
  * resting stops - a stop is a sale placed under the price with no cost on record
    to clear, so an armed pass places none (STOP_ORDERS_ALLOWED);
  * auto-trim already refuses any trim that would not clear its cost and fees
    (auto_trim.REQUIRE_PROFIT), and an unknown cost refuses there too.

A price or entry that cannot be read is a refusal, never a guessed yes. Alpaca is a
different venue with its own code and never reads this.
"""

# The grid prices each slice with the account's real fee rates. Sellers that have no
# measured rate use this one: the grid's own conservative round trip (1.2% a side).
# Erring high can only refuse a sale that was barely profitable; erring low can sell
# at a loss, which is the one thing this file exists to stop.
DEFAULT_ROUND_TRIP_FEE_RATE = 0.024


def net_after_fees(qty, entry_price, price, round_trip_fee_rate=None):
    """What selling qty bought at entry_price would net at price, both fee legs paid -
    the same arithmetic as crypto_grid_bot._grid_slice_net_pnl."""
    rate = DEFAULT_ROUND_TRIP_FEE_RATE if round_trip_fee_rate is None else round_trip_fee_rate
    return qty * (price - entry_price) - qty * (entry_price + price) * (rate / 2)


def verdict(qty, entry_price, price, round_trip_fee_rate=None):
    """(ok, reason). ok only when every number is readable and the sale nets above zero."""
    try:
        qty, entry_price, price = float(qty), float(entry_price), float(price)
    except (TypeError, ValueError):
        return False, "profit-only rule: quantity, cost or price unreadable - not sold"
    if not (qty > 0 and entry_price > 0 and price > 0):
        return False, "profit-only rule: quantity, cost or price unreadable - not sold"
    net = net_after_fees(qty, entry_price, price, round_trip_fee_rate)
    if net > 0:
        return True, f"nets +${net:,.2f} after fees"
    return False, (f"profit-only rule: selling at ${price:,.6g} against a ${entry_price:,.6g} "
                   f"cost would net ${net:,.2f} after fees - held, not sold")


def net_verdict(net):
    """(ok, reason) for a net already computed with the caller's own real fee rates."""
    try:
        net = float(net)
    except (TypeError, ValueError):
        return False, "profit-only rule: net after fees unreadable - not sold"
    if net > 0:
        return True, f"nets +${net:,.2f} after fees"
    return False, f"profit-only rule: this sale would net ${net:,.2f} after fees - held, not sold"


NO_COST_ON_RECORD = ("profit-only rule: no cost is on record for this sale, so it cannot be "
                     "shown to clear its fees - not sold")

# Read by resting_stops_worker. A constant, not a variable: the rule has no switch.
STOP_ORDERS_ALLOWED = False
NO_STOP_SALES = ("profit-only rule: a stop is a sale placed under the price with no cost on "
                 "record to clear, so none is placed. Nothing already resting is cancelled.")
