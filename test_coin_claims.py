"""Cross-system coin claims: the grid and the tree must not share a coin.

Both systems hold positions in one Coinbase account where a coin's balance
is POOLED. Two branches on the same coin each track their own qty against
those same tokens, and the accounting stops meaning anything - the gap
behind this repo's phantom-position self-heal, its DB-vs-Coinbase
SHORTFALLs, and the consolidate-branches feature built after 15 branches
piled onto POL-USD.

Each system guarded only itself. crypto_family_tree_bot never referenced
CryptoGridBranch at all, so the tree could pick a coin the fleet was
actively gridding. Harmless only while one of the two was not running -
which stopped being true on 2026-09-24.

Run: python3 test_coin_claims.py
"""
import os
import re
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "crypto_coin_claims.py")).read()
GRID = open(os.path.join(HERE, "crypto_grid_bot.py")).read()
TREE = open(os.path.join(HERE, "crypto_family_tree_bot.py")).read()

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


# normalize_product is pure; run it for real rather than asserting on source.
ns = {"log": types.SimpleNamespace(warning=lambda *a, **k: None)}
exec(re.search(r"def normalize_product\(symbol\):.*?\n    return s\n", SRC, re.S).group(0), ns)
normalize = ns["normalize_product"]

for raw, want in [("BTC/USD", "BTC-USD"), ("BTC-USD", "BTC-USD"), ("btc-usd", "BTC-USD"),
                  ("BTC", "BTC-USD"), ("  sol/usd ", "SOL-USD"), ("", ""), (None, "")]:
    ok(f"normalize({raw!r}) -> {want!r}", normalize(raw) == want)

# The separator mismatch is not hypothetical: tree positions were found
# stored as "BTC/USD" while product ids sent to Coinbase are "BTC-USD".
# Comparing raw made every claim check silently miss - the same bug that
# 404'd the reconciliation panel's Reconcile link.
ok("both separators collapse to one spelling", normalize("BTC/USD") == normalize("BTC-USD"))

tree_holds = {normalize(x) for x in ("ETH-USD", "BTC/USD")}
ok("a tree-held coin reads as claimed", normalize("ETH-USD") in tree_holds)
ok("BTC/USD held by the tree blocks BTC-USD for the grid", normalize("BTC-USD") in tree_holds)
ok("a coin nobody holds stays available", normalize("SOL-USD") not in tree_holds)

ok("the tree filters out grid-owned coins",
   "claims.claimed_by_other(claims.TREE)" in TREE and "not in grid_claimed" in TREE)
ok("and logs which coins it withheld, and why",
   "cannot be tracked by two systems" in TREE)
ok("the grid refuses a coin the tree holds",
   "claims.claimed_by_other(claims.GRID)" in GRID
   and "already held by a family-tree branch" in GRID)
ok("and its error says what to do about it", "Pick another coin" in GRID)
ok("both sides normalize before comparing",
   "claims.normalize_product(" in GRID and "claims.normalize_product(" in TREE)

ok("the lookup fails OPEN, with the reasoning written down",
   "fails OPEN deliberately" in SRC)
ok("an inactive grid branch still holding slices keeps its claim",
   "b.bot_name in holding" in SRC)
ok("the tree claims its configured coin AND what it really holds",
   "CryptoTreeBranch.product_id" in SRC and "BotPosition.symbol" in SRC)
ok("an overlap is reported rather than hidden", '"grid+tree"' in SRC)
ok("the module imports neither bot, so neither import cycles",
   "import crypto_grid_bot" not in SRC and "import crypto_family_tree_bot" not in SRC)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
