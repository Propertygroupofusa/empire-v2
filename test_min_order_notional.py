"""Does the bot stop sending orders the venue will always refuse?

The live failure: a $980 account sized LONG SH at 0.020988 shares of a
$32.40 ETF - $0.68 - and Alpaca refused it for being under its $1 minimum,
every ~30 seconds for hours.

The guard must do three things and no more: refuse the buy, never silently
enlarge it to clear the minimum, and never block an exit.
"""
import asyncio
import sys

import prop_bot as P

FAILS = []


def ok(label, cond, got=None):
    if cond:
        print(f"  PASS  {label}")
    else:
        FAILS.append(label)
        print(f"  FAIL  {label}" + (f"   got: {got!r}" if got is not None else "")) 


class Tripwire(Exception):
    pass


# RECORD the attempt, do not rely on the exception escaping.
#
# The first version of this test raised from .post() and asserted the
# exception propagated. It never does: execute_futures_trade wraps the send
# in `except Exception`, logs "Futures trade error", and returns False. So
# an order that DID reach the venue looked identical to one that was
# refused before sending, and three correct behaviours read as failures.
# The list is the evidence; the raise only stops execution going further.
SENT = []


class Boom:
    def post(self, *a, **k):
        SENT.append(True)
        raise Tripwire("an order was sent to the venue")


print("\n[1] the exact live case is refused, and never reaches the venue")
P._below_min_logged.clear(); SENT.clear()
out = asyncio.run(P.execute_futures_trade(
    Boom(), "SH", "BUY", 0.020988, 32.40, 83.8, "bullish"))
ok("it returned False", out is False, out)
ok("no order was sent", SENT == [], SENT)

print("\n[2] IT DOES NOT ROUND THE SIZE UP TO CLEAR THE MINIMUM")
src = open("prop_bot.py").read()
seg = src[src.index("THE VENUE'S MINIMUM ORDER VALUE"):src.index("_seen = _below_min_logged")]
for bad in ("qty =", "qty=", "MIN_ORDER_NOTIONAL_USD /", "max(qty"):
    ok(f"the guard never reassigns the quantity ({bad!r})", bad not in seg)
ok("the declared minimum is the venue's own $1", P.MIN_ORDER_NOTIONAL_USD == 1.0,
   P.MIN_ORDER_NOTIONAL_USD)

print("\n[3] an order AT or ABOVE the minimum is not blocked by this guard")
P._below_min_logged.clear(); SENT.clear()
asyncio.run(P.execute_futures_trade(Boom(), "SH", "BUY", 0.5, 32.40, 50, "bullish"))
ok("a $16.20 buy reaches the venue", SENT == [True], SENT)
P._below_min_logged.clear(); SENT.clear()
asyncio.run(P.execute_futures_trade(Boom(), "SH", "BUY", 1.0 / 32.40, 32.40, 50, "bullish"))
ok("a buy at exactly $1.00 reaches the venue", SENT == [True], SENT)

print("\n[4] AN EXIT IS NEVER BLOCKED - a position must always be able to leave")
P._below_min_logged.clear(); SENT.clear()
asyncio.run(P.execute_futures_trade(Boom(), "SH", "SELL", 0.020988, 32.40, 20, "bearish"))
ok("a sub-minimum SELL still reaches the venue", SENT == [True], SENT)

print("\n[5] an unreadable price is refused, not treated as fine")
P._below_min_logged.clear(); SENT.clear()
out = asyncio.run(P.execute_futures_trade(Boom(), "SH", "BUY", 0.5, None, 50, "bullish"))
ok("an unreadable notional refuses the buy", out is False, out)
ok("...and nothing was sent", SENT == [], SENT)

print("\n[6] the condition is logged once per size, not once per cycle")
P._below_min_logged.clear()
seen = []
_real = P.log.warning
P.log.warning = lambda m, *a, **k: seen.append(str(m))
for _ in range(5):
    asyncio.run(P.execute_futures_trade(Boom(), "SH", "BUY", 0.020988, 32.40, 50, "bullish"))
ok("five identical attempts logged once", len(seen) == 1, len(seen))
asyncio.run(P.execute_futures_trade(Boom(), "SH", "BUY", 0.010, 32.40, 50, "bullish"))
ok("a DIFFERENT size logs again", len(seen) == 2, len(seen))
ok("the message names the shortfall", "$0.68" in seen[0], seen[0][:160])
ok("and does not claim the order failed at the venue",
   "REJECTED" not in seen[0], seen[0][:160])
P.log.warning = _real

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
