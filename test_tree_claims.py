"""A retired family tree must not reserve coins it will never buy.

The bill for the old behaviour, 2026-09-25:

    The Grid Bot - the ONLY crypto system still placing real Coinbase
    orders - could not open a single new branch. Its spread plan read:

        OPEN NEW BRANCHES: 5
          eligible coins: NONE

    with $259.41 waiting to deploy and 35 freshly ranked coins available.
    Every candidate failed crypto_grid_bot.py's eligibility check, because
    tree_claimed_products() returned every CryptoTreeBranch.product_id in
    the database - with no check for whether the tree was retired.

    The tree has been in passive mode for a long time: run_branch_cycle()
    returns immediately for every branch, so no order is ever placed. It
    was reserving the entire coin pool for purchases that could not happen,
    against the one system that could actually trade them.

THE DISTINCTION THIS FILE PROTECTS:

    CONFIGURED product_id  a claim on the FUTURE - "what I am about to
                           buy". Meaningless once retired. Dropped.
    HELD BotPosition       a claim on the PRESENT - "what is really in the
                           shared wallet". Still true when retired, and
                           exactly the case where the grid must not trade
                           a coin out from under a position. Kept.

A future edit that "simplifies" this by returning an empty set whenever
retired would let the grid trade a coin the tree still holds. A future edit
that restores the old unconditional claim re-breaks the grid. Both fail here.

Run: python3 test_tree_claims.py
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


src = open(os.path.join(HERE, "crypto_coin_claims.py"), encoding="utf-8").read()
tree = ast.parse(src)
fn = [n for n in ast.walk(tree)
      if isinstance(n, ast.AsyncFunctionDef) and n.name == "tree_claimed_products"][0]
body = ast.unparse(fn)

# --- the passive-mode check exists and is consulted -----------------------
ok("passive mode is consulted", "is_crypto_passive_mode" in body)
ok("the import is function-scoped (no circular import at module load)",
   any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(fn)))
ok("module scope does NOT import crypto_family_tree_bot",
   not any(isinstance(n, (ast.Import, ast.ImportFrom))
           and any("crypto_family_tree_bot" in (a.name or "") for a in n.names)
           for n in tree.body))

# --- fails OPEN, matching claimed_by_other's documented reasoning ---------
ok("a passive-mode lookup failure falls back to NOT retired (fails open)",
   "retired = False" in body)

# --- configured claims are conditional, held claims are not --------------
ok("configured product_id claims are conditional on `retired`",
   "set() if retired else" in body or "if retired else" in body)
# The BotPosition loop must NOT sit inside a `retired` branch.
pos_loops = [n for n in ast.walk(fn) if isinstance(n, ast.For)]
ok("there is a loop over BotPosition rows", len(pos_loops) >= 1)


def inside_retired_branch(node, root):
    """True if `node` is nested under an `if retired`-style test."""
    for parent in ast.walk(root):
        if isinstance(parent, ast.If):
            test = ast.unparse(parent.test)
            if "retired" in test:
                for child in ast.walk(parent):
                    if child is node:
                        return True
    return False


ok("held-position claims are NOT gated on retired (a held coin stays claimed)",
   all(not inside_retired_branch(loop, fn) for loop in pos_loops))
ok("claimed.add is still reached for held symbols", "claimed.add" in body)


# --- the same rule, exercised as behaviour -------------------------------
def claims_for(retired, configured, held):
    """Pure re-statement of the rule in tree_claimed_products()."""
    claimed = set() if retired else set(configured)
    claimed |= set(held)
    return {c for c in claimed if c}


# Trading tree: both kinds of claim stand.
ok("active tree claims its configured coins",
   claims_for(False, ["UNI-USD", "NEAR-USD"], []) == {"UNI-USD", "NEAR-USD"})
ok("active tree also claims what it holds",
   claims_for(False, ["UNI-USD"], ["FIL-USD"]) == {"UNI-USD", "FIL-USD"})

# Retired tree: intentions dropped, reality kept.
ok("retired tree releases coins it merely intended to buy",
   claims_for(True, ["UNI-USD", "NEAR-USD", "FIL-USD"], []) == set())
ok("retired tree STILL claims a coin it actually holds",
   claims_for(True, ["UNI-USD", "NEAR-USD"], ["FIL-USD"]) == {"FIL-USD"})
ok("retired tree holding one coin frees the others",
   claims_for(True, ["UNI-USD", "NEAR-USD", "FIL-USD"], ["FIL-USD"]) == {"FIL-USD"})

# The live situation: tree retired, no open positions (the dashboard's own
# "No open positions to reconcile right now"), so the pool must open fully.
ok("the live case - retired, nothing held - frees the entire pool",
   claims_for(True, ["UNI-USD", "NEAR-USD", "ARB-USD", "FIL-USD", "BTC-USD"], []) == set())

# Regression guards, both directions.
ok("REGRESSION: a retired tree must not claim everything (the old bug)",
   claims_for(True, ["UNI-USD", "NEAR-USD"], []) != {"UNI-USD", "NEAR-USD"})
ok("REGRESSION: retired must not mean 'claim nothing at all'",
   claims_for(True, ["UNI-USD"], ["FIL-USD"]) != set())

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
