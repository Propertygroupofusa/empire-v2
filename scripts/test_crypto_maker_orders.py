"""
Test maker-order routing for crypto_coinbase_bot.

WHY THIS EXISTS
---------------
Coinbase charges 0.40% taker and 0.25% maker on this tier, so routing
both legs as maker takes the round trip from 0.80% to 0.50%. The measured
mean-reversion edge is roughly 0.2-0.4%, so that 0.30% is not a
refinement - it is most of the difference between negative and marginally
positive.

The danger is that a maker order is a RESTING LIMIT that may never fill,
and the consequences of not filling are wildly asymmetric:

  BUY not filled          no position, no risk, free to retry
  SELL (profit) not filled  position is in profit, can wait a cycle
  SELL (stop) not filled    the stop did not happen

That last one is the whole safety question. A stop that might not fill is
not a stop, and resting one behind the market while price runs away is
exactly how a small loss becomes a large one.

The second hazard is silent phantom positions. Every caller treats
place_order()'s True as "the position changed" - recording an open
position or booking a realised P&L. A maker order that is merely ACCEPTED
has traded nothing, so returning True on acceptance would have the bot
record a position it does not own and later try to sell coins that were
never bought.

WHAT IS ASSERTED
----------------
  1. maker is OFF by default
  2. a STOP LOSS sell is NEVER routed as maker, in any mode
  3. profit-target and RSI sells, and all buys, may be maker when armed
  4. post_only is set on the limit, so the order can be rejected but can
     never silently become a taker
  5. BUY rests at the bid and SELL at the ask - resting at the wrong side
     of the book crosses the spread and pays taker anyway
  6. True is returned only on a CONFIRMED fill, never on acceptance
  7. an unfilled maker order is cancelled rather than left on the book
  8. an unfilled BUY does NOT chase with a taker order - paying 0.40% to
     force the entry undoes the entire reason for being here

Exercised against a fake Coinbase that records the requests, so the
assertions are about what would actually be sent over the wire.
"""
import asyncio
import os
import pathlib
import sys
import types

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("COINBASE_API_KEY_NAME", "test")
os.environ.setdefault("COINBASE_API_PRIVATE_KEY", "")

# The bot imports models, which imports database.Base. Stub both: the
# real ones need asyncpg and a live DATABASE_URL, and neither is involved
# in order routing.
from sqlalchemy.orm import declarative_base  # noqa: E402
_fake_db = types.ModuleType("database")
_fake_db.AsyncSessionLocal = None
_fake_db.Base = declarative_base()
sys.modules["database"] = _fake_db

import crypto_coinbase_bot as bot  # noqa: E402

bot._auth_headers = lambda m, p: {}
# Keep the poll interval positive. The fill loop advances its own clock by
# this value, so zero never reaches the timeout and the process hangs -
# which is how the missing clamp in the module was found. Do not set this
# to 0 to "speed the test up"; the module now clamps it anyway.
bot.MAKER_POLL_INTERVAL_S = 0.01
bot.MAKER_FILL_TIMEOUT_S = 0.03


class Resp:
    def __init__(self, payload, status=200):
        self._p, self.status = payload, status

    async def json(self):
        return self._p

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeCoinbase:
    """Records every request; `fill` decides whether the limit fills."""

    def __init__(self, fill=True, bid=100.0, ask=101.0):
        self.fill, self.bid, self.ask = fill, bid, ask
        self.posts, self.cancels = [], []

    def get(self, url, **kw):
        if "product_book" in url:
            return Resp({"pricebook": {"bids": [{"price": str(self.bid)}],
                                       "asks": [{"price": str(self.ask)}]}})
        if "orders/historical" in url:
            return Resp({"order": {"status": "FILLED" if self.fill else "OPEN",
                                   "filled_size": "1.0" if self.fill else "0"}})
        return Resp({}, 404)

    def post(self, url, **kw):
        body = kw.get("json") or {}
        if "batch_cancel" in url:
            self.cancels.append(body)
            return Resp({"results": [{"success": True}]})
        self.posts.append(body)
        return Resp({"success": True, "success_response": {"order_id": "ord-1"}})


def cfg(body):
    return body.get("order_configuration", {})


def main():
    failures = []
    print("Crypto maker-order routing\n")

    # ── 1. off by default ──────────────────────────────────────────────
    src = (REPO / "crypto_coinbase_bot.py").read_text()
    if 'os.getenv("CRYPTO_ORDER_MODE", "taker")' not in src:
        failures.append("CRYPTO_ORDER_MODE does not default to taker")
    else:
        print("  CRYPTO_ORDER_MODE defaults to 'taker'")

    # ── 2 & 3. routing policy ──────────────────────────────────────────
    bot.ORDER_MODE = "taker"
    if bot.maker_allowed("buy") or bot.maker_allowed("sell", "PROFIT TARGET"):
        failures.append("maker used while ORDER_MODE=taker")

    bot.ORDER_MODE = "maker"
    policy = {
        ("buy", ""): True,
        ("sell", "PROFIT TARGET"): True,
        ("sell", "RSI"): True,
        ("sell", "STOP LOSS"): False,     # the one that must never rest
    }
    for (side, reason), expected in policy.items():
        got = bot.maker_allowed(side, reason)
        if got != expected:
            failures.append(f"maker_allowed({side!r}, {reason!r}) = {got}, "
                            f"expected {expected}")
    if not failures:
        print("  STOP LOSS never rests as maker; buys and profit exits may")

    # ── 4, 5, 6. a filling maker order ─────────────────────────────────
    fake = FakeCoinbase(fill=True)
    ok = asyncio.run(bot.place_order(fake, "BTC/USD", "buy", 1.0, 100.0))
    if ok is not True:
        failures.append(f"filled maker buy returned {ok!r}, expected True")
    if len(fake.posts) != 1:
        failures.append(f"expected 1 order, sent {len(fake.posts)}")
    else:
        c = cfg(fake.posts[0]).get("limit_limit_gtc")
        if not c:
            failures.append(f"maker order is not a limit: {cfg(fake.posts[0])}")
        else:
            if c.get("post_only") is not True:
                failures.append("post_only not set - the order could silently "
                                "become a taker and pay 0.40%")
            if float(c["limit_price"]) != fake.bid:
                failures.append(f"BUY rested at {c['limit_price']}, not the bid "
                                f"{fake.bid} - crossing pays taker anyway")
            else:
                print(f"  BUY rests at the bid ({fake.bid}), post_only=True")

    fake = FakeCoinbase(fill=True)
    asyncio.run(bot.place_order(fake, "BTC/USD", "sell", 1.0, 100.0,
                                reason="PROFIT TARGET"))
    c = cfg(fake.posts[0]).get("limit_limit_gtc", {})
    if float(c.get("limit_price", 0)) != fake.ask:
        failures.append(f"SELL rested at {c.get('limit_price')}, not the ask "
                        f"{fake.ask}")
    else:
        print(f"  SELL rests at the ask ({fake.ask})")

    # ── 7 & 8. an unfilled maker order ─────────────────────────────────
    fake = FakeCoinbase(fill=False)
    ok = asyncio.run(bot.place_order(fake, "BTC/USD", "buy", 1.0, 100.0))
    if ok is not False:
        failures.append(f"unfilled maker buy returned {ok!r} - a merely ACCEPTED "
                        f"order must never report a fill, or the bot records a "
                        f"position it does not own")
    if not fake.cancels:
        failures.append("unfilled maker order was not cancelled - it would sit "
                        "on the book unmanaged")
    else:
        print("  unfilled maker order is cancelled and reports no fill")
    market_orders = [p for p in fake.posts if "market_market_ioc" in cfg(p)]
    if market_orders:
        failures.append(f"unfilled maker BUY chased with {len(market_orders)} "
                        f"taker order(s) - paying 0.40% to force the entry "
                        f"undoes the reason for using maker at all")
    else:
        print("  unfilled BUY does not chase with a taker order")

    # ── stop loss really does go taker, end to end ─────────────────────
    fake = FakeCoinbase(fill=True)
    asyncio.run(bot.place_order(fake, "BTC/USD", "sell", 1.0, 100.0,
                                reason="STOP LOSS"))
    if not fake.posts or "market_market_ioc" not in cfg(fake.posts[0]):
        failures.append(f"STOP LOSS was not sent as a market order: "
                        f"{cfg(fake.posts[0]) if fake.posts else 'nothing sent'}")
    else:
        print("  STOP LOSS is sent as market_market_ioc (crosses, always fills)")

    # ── no book => fall back to taker rather than skipping the order ───
    fake = FakeCoinbase(fill=True, bid=None, ask=None)
    fake.get = lambda url, **kw: Resp({"pricebook": {"bids": [], "asks": []}})
    ok = asyncio.run(bot.place_order(fake, "BTC/USD", "sell", 1.0, 100.0,
                                     reason="PROFIT TARGET"))
    if not fake.posts or "market_market_ioc" not in cfg(fake.posts[0]):
        failures.append("with no book the order was not placed as taker - an "
                        "exit must not be silently skipped")
    else:
        print("  unreadable book falls back to taker instead of skipping")

    print()
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  PASS  maker routing is off by default, never risks a stop, and "
          "only reports confirmed fills")
    return 0


if __name__ == "__main__":
    sys.exit(main())
