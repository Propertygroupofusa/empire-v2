""""Did not fill" and "was never placed" are different faults.

grid_sell returns None for both, so the caller said "did not fill" either
way - and that one word cost this session an hour of wrong diagnosis. ZEC
logged, on consecutive lines:

    NO MAKER SELL PLACED - available_quantity=0.0
    requested_quantity=0.0735  decision=DUST  reason=BELOW_BASE_INCREMENT
    real grid sell of ZEC-USD did not fill - will retry next cycle

The second line is the one a reader believes, and it points at the maker
rest time. Nothing rested. The wallet had 0.0 available against a slice
claiming 0.37816665, and resting longer cannot fix a claim that outruns the
wallet.
"""
import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

PASS, FAIL = [], []


def ok(label, cond):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label)


SRC = (REPO / "crypto_grid_bot.py").read_text()
i = SRC.index('"DID NOT FILL" AND "WAS NEVER PLACED" ARE DIFFERENT FAULTS')
j = SRC.index("_persist_exc", i)
BLOCK = SRC[i:j]

ok("the old unconditional 'did not fill' wording is gone",
   "did not fill - will retry next cycle" not in SRC)
ok("the two cases are named apart",
   "NO ORDER WAS PLACED" in BLOCK and "really did go unfilled" in BLOCK)
ok("the missing-order case says resting longer cannot help",
   "resting longer cannot help it" in BLOCK)
ok("and points at the fix that can", "reconciling" in BLOCK)
ok("both the claimed and the available figure are printed, so the gap is "
   "readable rather than asserted",
   "available against a slice claiming" in BLOCK)

# THE READ MUST NOT BE ABLE TO AFFECT THE SALE. It runs after a real venue
# call has already happened.
ok("the wallet read is wrapped", "except Exception as _exc" in BLOCK)
ok("and an unreadable wallet says so rather than guessing which case it is",
   "could not read available units" in BLOCK)
ok("it reads AVAILABLE units, which is the right question for 'could this "
   "order have been placed'",
   "available_units_map" in BLOCK and "owned_units_map" not in BLOCK)

# The gate-feed row must carry the same distinction, or the dashboard keeps
# showing the misleading version.
k = SRC.index("PARKED_SELL_NOFILL", i)
feed = SRC[k:k + 700]
ok("the gate-feed event carries the same note",
   "_avail_note" in feed)
ok("and its wording no longer claims a fill was attempted",
   "did not fill" not in feed)

# Nothing about the trading decision may have moved.
tree = ast.parse(SRC)
fn = next(n for n in ast.walk(tree)
          if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_grid_branch_cycle")
body = ast.unparse(fn)
ok("the sell call itself is unchanged",
   "fill = await grid_sell(session, oldest.qty, branch.product_id, branch.bot_name)"
   in body)
ok("the diagnosis runs only when there was no fill",
   body.index("if not fill:") < body.index("NO ORDER WAS PLACED"))

if __name__ == "__main__":
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1 if FAIL else 0)
