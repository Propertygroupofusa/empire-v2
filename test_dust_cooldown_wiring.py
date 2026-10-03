"""Does the cooldown actually SAVE the calls, and never hide a real sale?

test_dust_cooldown.py proves the decision logic. This proves the wiring:
that an armed cooldown short-circuits place_maker_sell BEFORE the balance,
rules and book are fetched, and that an unarmed one does not.

The balance fetch is replaced with one that RAISES. A test that merely
counted calls would pass even if the call still happened and was ignored;
raising makes the saving unforgeable.
"""
import asyncio
import sys

import crypto_btc_compound_bot as bot
import dust_cooldown as dc

FAILS = []


def ok(label, cond, got=None):
    if cond:
        print(f"  PASS  {label}")
    else:
        FAILS.append(label)
        print(f"  FAIL  {label}" + (f"   got: {got!r}" if got is not None else ""))


class Tripwire(Exception):
    pass


async def _exploding_balance(session, asset):
    raise Tripwire("the balance was fetched - the cooldown did not short-circuit")


_real_balance_fn = bot.get_asset_balance

print("\n[1] an armed cooldown returns None without touching the venue")
dc._armed.clear()
bot.get_asset_balance = _exploding_balance
dc.note_dust("QNT-USD", "DUST", available_units=0.00097323,
             reason="BELOW_BASE_INCREMENT")
try:
    out = asyncio.run(bot.place_maker_sell(None, 0.337991, "QNT-USD"))
    ok("it returned None", out is None, out)
    ok("no balance call was made", True)
except Tripwire as e:
    ok("no balance call was made", False, str(e))

print("\n[2] the skip is recorded so the dashboard does not read it as a fill")
blk = bot._last_order_block.get("QNT-USD") or {}
ok("the block names DUST", blk.get("decision") == dc.DUST, blk)
ok("the reason is the cooldown, distinguishable from a fresh refusal",
   blk.get("reason") == "DUST_COOLDOWN", blk)
ok("nothing rested", bot._last_order_rested.get("QNT-USD") is False,
   bot._last_order_rested.get("QNT-USD"))
ok("available_units is ABSENT, not zero - nothing was read this pass",
   "available_units" not in blk, blk)
err = bot._last_order_error.get("QNT-USD") or ""
ok("the error text says cooldown", "dust cooldown" in err, err)
ok("and does not claim a refusal by the venue this pass",
   "cooldown on the QUESTION" in err, err)

print("\n[3] WITHOUT a cooldown the balance IS fetched - the guard is not a mute")
dc._armed.clear()
try:
    asyncio.run(bot.place_maker_sell(None, 0.337991, "QNT-USD"))
    ok("the balance was fetched when nothing is armed", False,
       "place_maker_sell returned without reaching the balance fetch")
except Tripwire:
    ok("the balance was fetched when nothing is armed", True)

print("\n[4] a cooldown on one product does not silence another")
dc._armed.clear()
dc.note_dust("QNT-USD", "DUST", available_units=0.0009)
try:
    asyncio.run(bot.place_maker_sell(None, 100.0, "XRP-USD"))
    ok("XRP still reaches the venue", False, "returned early")
except Tripwire:
    ok("XRP still reaches the venue", True)

print("\n[5] an expired cooldown lets the attempt through again")
dc._armed.clear()
dc.note_dust("QNT-USD", "DUST", available_units=0.0009, now=0.0)
dc._armed["QNT-USD"]["at"] = -10_000.0          # far past the window
try:
    asyncio.run(bot.place_maker_sell(None, 0.337991, "QNT-USD"))
    ok("the expired cooldown did not block", False, "returned early")
except Tripwire:
    ok("the expired cooldown did not block", True)

bot.get_asset_balance = _real_balance_fn
dc._armed.clear()

print("\n[6] the module under test did not disable maker-only or any floor")
src = open("crypto_btc_compound_bot.py").read()
ok("post_only is still set on the sell", '"post_only": True' in src)
ok("base_min_size is still read from the venue", 'rules["base_min_size"]' in src)
ok("quote_min_size is still read from the venue", 'rules["quote_min_size"]' in src)
ok("the guard is actually installed", "_dust.skip_reason(product_id)" in src)
# The strongest statement available: the cooldown module itself has no way
# to reach the venue, so no edit to it can ever place or cancel an order.
cd_src = open("dust_cooldown.py").read()
# NOT the bare word "order": it appears in this module's own prose ("an
# order", "orders"), and a test that matches English in a comment is the
# same mistake as reading control flow out of a docstring. Only tokens that
# would have to be present for a call to actually go out.
for verb in ("aiohttp", "requests", "urllib", "session", "post(",
             "_place_maker", "client_order_id", "order_configuration"):
    ok(f"dust_cooldown.py contains no '{verb}'", verb not in cd_src.lower()
       if verb.islower() else verb not in cd_src)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
