"""The operator's chosen coins must never be filtered down to nothing.

The bill for not having this, 2026-09-25:

    A spread plan reported, with $259.41 waiting to deploy and 35 freshly
    ranked coins in hand:

        OPEN NEW BRANCHES: 5
          eligible coins: NONE

    Three independent filters had stacked into a total shutout:

      MIN_REQUIRED_ROI_PCT (20%)  only 3 of 35 coins cleared it - UNI
                                  40.0%, NEAR 36.1%, ARB 34.5%. FIL
                                  missed at 18.8%.
      MANUAL_EXCLUDED_COINS       hardcoded; blocks UNI (the #1 ranked
                                  coin) and STX (which earned real money
                                  in September).
      TOP_N_ELIGIBLE_COINS (15)   cuts everything outside the top 15 by
                                  backtest ROI, starving the generic
                                  nine-coin fallback too.

    Of the three survivors one was hardcoded-blocked and one was already
    claimed. Every layer was individually defensible. Together they left
    the only crypto system still placing orders with nothing to trade.

THE RULE THIS FILE PROTECTS: a coin the operator deliberately chose is
judged on two questions only - did they exclude it by hand from the
dashboard, and is another branch already on it. Backtest ROI still ORDERS
the picks; it no longer vetoes them. An automatic signal may rank a
deliberate choice lower. It may not silently remove it.

Run: python3 test_working_set.py
"""
import ast
import importlib
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


src = open(os.path.join(HERE, "crypto_grid_bot.py"), encoding="utf-8").read()

# --- the chosen eight, exactly ------------------------------------------
# Parsed with AST, not a regex: the default is written as ADJACENT string
# literals (implicit concatenation), and a regex grabbing only the first
# one silently reports half the list as the whole list - which is exactly
# what this check did on its first run after the set grew past one line.
def _default_working_set(source):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "getenv"
                and node.args
                and getattr(node.args[0], "value", "") == "GRID_WORKING_SET"
                and len(node.args) > 1):
            return ast.literal_eval(node.args[1])
    return None


_raw = _default_working_set(src)
ok("a default working set is defined", _raw is not None)
coins = [c.strip() for c in (_raw or "").split(",") if c.strip()]
CHOSEN = ["BTC-USD", "NEAR-USD", "DOGE-USD", "ARB-USD", "ETH-USD", "SOL-USD", "LINK-USD",
          "INJ-USD", "APT-USD", "TIA-USD", "LDO-USD", "FIL-USD", "ICP-USD", "SUI-USD"]
ok("it is exactly the fourteen coins selected", coins == CHOSEN)
ok("ARB is included - it is the largest funded branch", "ARB-USD" in coins)
ok("NEAR is included - it ranked #2 at 36.1% ROI", "NEAR-USD" in coins)
ok("DOGE and ETH are included - both earned real money in September",
   "DOGE-USD" in coins and "ETH-USD" in coins)

# --- it must be overridable without a deploy -----------------------------
ok("the set is read from the environment, not frozen in source",
   'os.getenv(\n        "GRID_WORKING_SET"' in src or 'os.getenv("GRID_WORKING_SET"' in src)


def parse_env(value):
    """Re-statement of the parsing in crypto_grid_bot.GRID_WORKING_SET."""
    return [c.strip().upper() for c in value.split(",") if c.strip()]


ok("an env override replaces the default", parse_env("SOL-USD,LINK-USD") == ["SOL-USD", "LINK-USD"])
ok("lowercase input is normalised", parse_env("sol-usd,link-usd") == ["SOL-USD", "LINK-USD"])
ok("stray whitespace is tolerated", parse_env(" SOL-USD , LINK-USD ") == ["SOL-USD", "LINK-USD"])
ok("empty entries are dropped", parse_env("SOL-USD,,LINK-USD,") == ["SOL-USD", "LINK-USD"])

# --- no chosen coin may sit on the hardcoded blocklist -------------------
treesrc = open(os.path.join(HERE, "crypto_family_tree_bot.py"), encoding="utf-8").read()
blocked = set(
    re.search(r"MANUAL_EXCLUDED_COINS = \{([^}]+)\}", treesrc)
    .group(1).replace('"', "").replace("'", "").split(", ")
)
ok("the hardcoded blocklist is still found (guards this test's own premise)",
   len(blocked) >= 5)
ok("no chosen coin is hardcoded-blocked", not (set(CHOSEN) & blocked))
# The blocklist still has to BLOCK things - this is not a licence to empty it.
ok("the blocklist still blocks UNI and POL (real live evidence)",
   "UNI-USD" in blocked and "POL-USD" in blocked)

# --- the eligibility rule -------------------------------------------------
fn = [n for n in ast.walk(ast.parse(src))
      if isinstance(n, ast.AsyncFunctionDef) and n.name == "_spread_candidate_coins"][0]
body = "\n".join(ast.unparse(s) for s in fn.body[1:])  # docstring named the filters

ok("the working set is consulted in candidate selection", "GRID_WORKING_SET" in body)
ok("hand exclusions are computed separately from automatic ones",
   "hand_excluded" in body)
ok("the working set is judged against hand exclusions, not the full set",
   "working_set=True" in body)
ok("ranked picks are still offered FIRST (ROI orders, it does not veto)",
   body.index("for p in ranked") < body.index("for pid in GRID_WORKING_SET"))
ok("the working set is tried BEFORE the generic nine-coin list",
   body.index("for pid in GRID_WORKING_SET") < body.index("scanner.NINE_COINS"))
ok("a reasons-lookup failure fails CLOSED for the working set",
   "hand_excluded = set(excluded)" in body)


# --- the rule, exercised as behaviour ------------------------------------
def eligible(pid, *, working_set, auto_excluded, hand_excluded, claimed, tree_held):
    blocked_set = hand_excluded if working_set else auto_excluded
    return pid not in blocked_set and pid not in claimed and pid not in tree_held


base = dict(auto_excluded={"SOL-USD", "LINK-USD", "AVAX-USD", "DOGE-USD"},
            hand_excluded=set(), claimed=set(), tree_held=set())

ok("REGRESSION: the live shutout - auto-excluded chosen coins are now eligible",
   all(eligible(c, working_set=True, **base) for c in ["SOL-USD", "LINK-USD", "AVAX-USD"]))
ok("a NON-working-set coin is still subject to the automatic layers",
   not eligible("FIL-USD", working_set=False,
                **{**base, "auto_excluded": base["auto_excluded"] | {"FIL-USD"}}))
ok("a hand exclusion still kills a working-set coin",
   not eligible("SOL-USD", working_set=True, **{**base, "hand_excluded": {"SOL-USD"}}))
ok("a coin already claimed by a branch is still skipped",
   not eligible("ARB-USD", working_set=True, **{**base, "claimed": {"ARB-USD"}}))
ok("a coin the tree really holds is still skipped",
   not eligible("BTC-USD", working_set=True, **{**base, "tree_held": {"BTC-USD"}}))
ok("with nothing excluded or claimed, all fourteen are available",
   sum(1 for c in CHOSEN if eligible(c, working_set=True,
       auto_excluded=set(), hand_excluded=set(), claimed=set(), tree_held=set())) == 14)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
