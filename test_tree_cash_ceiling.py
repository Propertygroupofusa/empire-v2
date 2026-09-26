"""The family tree's cash ceiling, and the guard that keeps it complete.

WHY THIS FILE EXISTS

The tree and the grid fleet spend one Coinbase wallet. The ceiling was
first wired into a single buy site in crypto_family_tree_bot - and there
are five. The branch entry and the reinforcement buy, the two that spend
most of the money, were both uncapped, so the ceiling protected the fleet
from nothing while looking done.

Two things follow, and both are tested here:

  1. A COVERAGE GUARD. Every competitive buy must route through
     capped_market_buy(). A raw engine.place_market_buy() is allowed only
     inside that helper, or at a site carrying an explicit written
     exemption. Adding a new buy path without a cap fails this file.
  2. THE HELPER'S OWN ARITHMETIC. Its first version passed the REQUESTED
     amount where the wallet's free cash belonged, so a $50 ask came out
     as $10.50 - every tree buy silently strangled, no error anywhere.
     The "within-share ask passes through untouched" check below is the
     one that caught it, and is the reason this file tests behaviour
     rather than just structure.

Run: python3 test_tree_cash_ceiling.py
"""
import asyncio
import os
import re
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import crypto_cash_allocator as allocator  # noqa: E402

SRC = open(os.path.join(HERE, "crypto_family_tree_bot.py")).read()
checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


# --- 1. coverage guard -----------------------------------------------------
HELPER = re.search(
    r"async def capped_market_buy\(.*?\n    return await engine\.place_market_buy\("
    r"session, spend, product_id\)\n", SRC, re.S)
ok("the chokepoint exists", HELPER is not None)

raw_sites = [(m.start(), SRC[:m.start()].count("\n") + 1)
             for m in re.finditer(r"await engine\.place_market_buy\(", SRC)]
helper_span = (HELPER.start(), HELPER.end()) if HELPER else (0, 0)

inside, exempt, uncapped = [], [], []
for pos, line in raw_sites:
    if helper_span[0] <= pos < helper_span[1]:
        inside.append(line)
    elif "DELIBERATELY NOT capped_market_buy" in SRC[max(0, pos - 800):pos]:
        exempt.append(line)
    else:
        uncapped.append(line)

print(f"    raw engine.place_market_buy sites: {[l for _, l in raw_sites]}")
print(f"    inside chokepoint: {inside} | exempt: {exempt} | UNCAPPED: {uncapped}")
ok("no competitive buy bypasses the chokepoint", not uncapped)
ok("the chokepoint itself makes the only unguarded call", len(inside) == 1)
ok("exactly one documented exemption (the retirement conversion)", len(exempt) == 1)
ok("the exemption explains why it is exempt",
   "one-shot exit, not a competitive trade" in SRC)

for name, needle in [
    ("branch entry", "capped_market_buy(session, spend, branch.product_id"),
    ("reinforcement buy", "capped_market_buy(session, usd_amount, target_branch.product_id"),
    ("reversal buy", "capped_market_buy(session, spend, product_id"),
]:
    ok(f"the {name} routes through the chokepoint", needle in SRC)

ok("the share rule lives in exactly one place",
   SRC.count("allocator.spend_ceiling(allocator.TREE") == 1)


# --- 2. the helper's arithmetic -------------------------------------------
ns = {
    "engine": types.SimpleNamespace(),
    "log": types.SimpleNamespace(info=lambda *a, **k: None),
    "MIN_TRADE_USD": 10.0,
}
ceiling_fn = re.search(
    r"def tree_spend_ceiling\(spendable_usd\):.*?\n    return allocator\.spend_ceiling\("
    r"allocator\.TREE, spendable_usd\)\n", SRC, re.S)
exec(ceiling_fn.group(0) + "\n" + HELPER.group(0), ns)
capped_market_buy = ns["capped_market_buy"]

placed = []


async def fake_buy(session, usd, pid):
    placed.append(round(usd, 2))
    return (1.0, 100.0)


ns["engine"].place_market_buy = fake_buy

# The self-fetch path: callers that do not already know the wallet's free
# cash leave free_cash_usd None and the helper reads it. Stubbed so that
# path is exercised rather than skipped - it is the one the reinforcement
# buy actually takes.
BALANCE = {"value": 577.0, "err": None}


async def fake_balance(session):
    return BALANCE["value"], BALANCE["err"]


async def fake_locked():
    return 0.0


ns["engine"].get_usd_balance = fake_balance
ns["get_locked_usd"] = fake_locked
for var in ("CRYPTO_CASH_SHARE_TREE", "CRYPTO_CASH_SHARE_GRID",
            "CRYPTO_CASH_SHARE_COMPOUND", "CRYPTO_GLOBAL_CASH_RESERVE_USD"):
    os.environ.pop(var, None)

FREE = 577.0                      # the wallet after the BTC was sold
CEIL = allocator.spend_ceiling(allocator.TREE, FREE)[0]


def call(ask, free=FREE):
    placed.clear()
    return asyncio.run(capped_market_buy(None, ask, "SOL-USD", "b1", free_cash_usd=free))


res = call(50.0)
ok("an ask WITHIN the share passes through untouched", res is not None and placed == [50.0])
print(f"    -> free ${FREE:,.2f} | tree ceiling ${CEIL:,.2f} | $50 ask placed ${placed[0]:,.2f}")

res = call(300.0)
ok("an ask ABOVE the share is trimmed to the ceiling, not refused",
   res is not None and placed == [round(CEIL, 2)])
print(f"    -> $300 ask placed ${placed[0]:,.2f} (the ceiling, not a fraction of the ask)")

ok("the trim is bounded by the WALLET, never by the request",
   placed[0] == round(CEIL, 2) and placed[0] > 300.0 * 0.30)

res = call(5.0)
ok("an ask under the exchange minimum places no order", res is None and not placed)

res = call(50.0, free=0.29)
ok("a drained wallet places no order", res is None and not placed)

res = call(50.0, free=None)
ok("with no free cash passed, the helper reads the wallet itself",
   res is not None and placed == [50.0])

BALANCE["value"], BALANCE["err"] = None, "HTTP 401"
res = call(50.0, free=None)
ok("and places no order when that read fails", res is None and not placed)
BALANCE["value"], BALANCE["err"] = 577.0, None

BALANCE["value"] = 0.29
res = call(50.0, free=None)
ok("a drained wallet on the self-fetch path also places no order",
   res is None and not placed)
BALANCE["value"] = 577.0

# The property the whole ceiling exists for.
grid_ceiling = allocator.spend_ceiling(allocator.GRID, FREE)[0]
ok("after the tree spends its whole ceiling, the grid's share survives",
   allocator.spend_ceiling(allocator.GRID, FREE - CEIL)[0] > 0)
ok("and the two together never exceed the wallet", CEIL + grid_ceiling <= FREE)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
