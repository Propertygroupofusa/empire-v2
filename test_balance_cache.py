"""Does one account pull now answer every currency - without going stale?

The bug: get_asset_balance reads the WHOLE account listing and scans it for
ONE currency, so reading USD and USDC was two complete pulls for two numbers
in the same response. 38 call sites; place_maker_sell runs per branch per
30s cycle. That volume is what produced the 429s that made the account book
unreadable, which (until it was made fail-closed) switched the 20%
concentration ceiling off.

The risk the cache introduces is the opposite one: an order sized against a
balance a fill has already changed. Most of what follows guards that.
"""
import asyncio
import sys

import crypto_btc_compound_bot as bot

FAILS = []


def ok(label, cond, got=None):
    if cond:
        print(f"  PASS  {label}")
    else:
        FAILS.append(label)
        print(f"  FAIL  {label}" + (f"   got: {got!r}" if got is not None else ""))


# There are no Coinbase credentials in a test container, so _auth_headers
# raises ValueError and get_asset_balance returns "Auth header build failed"
# before it ever reaches the venue. The first run of this file showed PULLS
# at 0 across the board, which is what that looks like: not a cache that
# failed to work, a function that never got started.
bot._auth_headers = lambda method, path, body="": {"Authorization": "test"}

PULLS = []


class Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status, self._p, self._t = status, payload or {}, text
    async def json(self): return self._p
    async def text(self): return self._t
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class Session:
    """Counts full account-listing pulls."""
    def __init__(self, status=200, pages=None):
        self.status = status
        self.pages = pages or [{"accounts": [
            {"currency": "USD",  "available_balance": {"value": "1276.40"}},
            {"currency": "USDC", "available_balance": {"value": "0.080937"}},
            {"currency": "ZEC",  "available_balance": {"value": "1.58"}},
            {"currency": "BROKE", "available_balance": {"value": "not-a-number"}},
        ], "has_next": False}]
    def get(self, url, **k):
        PULLS.append(url)
        if self.status != 200:
            return Resp(self.status, text="rate limited")
        return Resp(200, self.pages[min(len(PULLS) - 1, len(self.pages) - 1)])


def reset(**kw):
    bot.invalidate_balance_cache()
    PULLS.clear()
    return Session(**kw)


print("\n[1] THE FIX: many currencies, one account pull")
s = reset()
usd, e1 = asyncio.run(bot.get_asset_balance(s, "USD"))
usdc, e2 = asyncio.run(bot.get_asset_balance(s, "USDC"))
zec, e3 = asyncio.run(bot.get_asset_balance(s, "ZEC"))
ok("USD is right", usd == 1276.40, usd)
ok("USDC is right", usdc == 0.080937, usdc)
ok("ZEC is right", zec == 1.58, zec)
ok("no errors", (e1, e2, e3) == (None, None, None), (e1, e2, e3))
ok("THREE reads cost ONE account pull", len(PULLS) == 1, len(PULLS))

print("\n[2] a currency genuinely absent reads as absent, not as zero")
bal, err = asyncio.run(bot.get_asset_balance(s, "DOGE"))
ok("absent currency returns None", bal is None, bal)
ok("...with a reason, not a 0.0", "no DOGE account" in (err or ""), err)
ok("and it did not re-pull", len(PULLS) == 1, len(PULLS))

print("\n[3] an unreadable ROW is skipped, never read as zero")
bal, err = asyncio.run(bot.get_asset_balance(s, "BROKE"))
ok("a row whose value will not parse is absent, not 0.0", bal is None, bal)

print("\n[4] A FAILED READ IS NEVER CACHED AND NEVER SERVED")
s = reset(status=429)
b1, e1 = asyncio.run(bot.get_asset_balance(s, "USD"))
ok("a 429 returns no balance", b1 is None, b1)
ok("...and says so", "429" in (e1 or ""), e1)
b2, e2 = asyncio.run(bot.get_asset_balance(s, "USD"))
ok("a second call RETRIES rather than serving a cached failure", len(PULLS) == 2, len(PULLS))
ok("still no balance", b2 is None, b2)
ok("the cache was not poisoned", bot._BALANCES_CACHE["by_currency"] is None,
   bot._BALANCES_CACHE["by_currency"])

print("\n[5] A STALE BALANCE MUST NOT SIZE AN ORDER - the cache expires")
s = reset()
asyncio.run(bot.get_asset_balance(s, "USD"))
ok("one pull so far", len(PULLS) == 1, len(PULLS))
bot._BALANCES_CACHE["at"] -= (bot._BALANCE_TTL_SECONDS + 1)
asyncio.run(bot.get_asset_balance(s, "USD"))
ok("past the TTL it pulls again", len(PULLS) == 2, len(PULLS))
ok("the TTL is short - 10s, inside one 30s grid cycle",
   bot._BALANCE_TTL_SECONDS <= 15, bot._BALANCE_TTL_SECONDS)

print("\n[6] a fill drops the cache immediately, without waiting for the TTL")
s = reset()
asyncio.run(bot.get_asset_balance(s, "USD"))
ok("cached", bot._BALANCES_CACHE["by_currency"] is not None)
bot.invalidate_balance_cache("a sell filled")
ok("invalidate clears it", bot._BALANCES_CACHE["by_currency"] is None)
asyncio.run(bot.get_asset_balance(s, "USD"))
ok("the next read goes to the venue", len(PULLS) == 2, len(PULLS))

print("\n[7] a PARTIAL listing is never cached")
# has_next true but no cursor -> the loop breaks with the walk incomplete.
s = reset()
s.pages = [{"accounts": [{"currency": "USD", "available_balance": {"value": "5.00"}}],
            "has_next": True}]          # no cursor
bal, _ = asyncio.run(bot.get_asset_balance(s, "USD"))
ok("the value it did see is returned", bal == 5.00, bal)
ok("an INCOMPLETE listing is not cached",
   bot._BALANCES_CACHE["by_currency"] is None, bot._BALANCES_CACHE["by_currency"])
# The assertion this replaced was `ok(..., True)` - a test that passes no
# matter what the code does. Writing it properly is what surfaced that the
# code cached partial listings while its own comment said it did not.
bal, err = asyncio.run(bot.get_asset_balance(s, "DOGE"))
ok("a currency missing from an INCOMPLETE listing reads as UNKNOWN",
   bal is None and "incomplete" in (err or ""), err)
ok("...and that is worded differently from a genuine absence",
   "no DOGE account found" not in (err or ""), err)
n_before = len(PULLS)
asyncio.run(bot.get_asset_balance(s, "USD"))
ok("an incomplete listing is re-fetched, not served from cache",
   len(PULLS) > n_before, (n_before, len(PULLS)))

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
