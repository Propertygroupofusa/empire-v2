"""R-13: the three paths that WRITE branch.num_levels must honour the same
GRID_LEVEL_CAP_EXEMPT rule the live cycle already honours.

THE BUG THIS LOCKS DOWN. run_grid_branch_cycle checks
branch_is_level_cap_exempt(branch.product_id) before applying a promoted
override's level ceiling. _effective_num_levels could not: it took only an
allocation, so create / add-cash / withdraw applied the ceiling to every
branch, exempt or not.

Measured live 2026-10-09, override capping at 3, five exempt branches on 10
levels: a $1 add-cash to HBAR-USD would have rewritten num_levels 10 -> 3
while it held NINE open slices. A branch whose open slices exceed its levels
cannot buy at all, so the funding would have frozen it. The five exempt
branches hold $2,664.38 - 55% of the fleet's claim.
"""
import asyncio, os, sys

sys.modules.setdefault("_pytest_shim", None)
os.environ.setdefault("GRID_LEVEL_CAP_EXEMPT", "XRP-USD,XLM-USD,HBAR-USD,BCH-USD,LINK-USD")

import crypto_grid_bot as g

FAILED = []
def ok(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILED.append(label)
def section(t): print("\n" + t)

run = asyncio.get_event_loop_policy().new_event_loop().run_until_complete

class Override:
    """Pin the promoted override without touching the database."""
    def __init__(self, label): self.label = label
    def __enter__(self):
        self._real = g.get_live_grid_spacing_override
        async def fake(): return self.label
        g.get_live_grid_spacing_override = fake
        return self
    def __exit__(self, *a):
        g.get_live_grid_spacing_override = self._real

CAPPED = next((k for k, v in g.GRID_LEVEL_SPACING_CANDIDATES.items()
               if v.get("num_levels", 99) <= 3 and k != "live_default"), None)

section("[1] the exemption is read from the environment, as the live cycle reads it")
ok("the five live exempt coins are exempt",
   all(g.branch_is_level_cap_exempt(p) for p in
       ("XRP-USD", "XLM-USD", "HBAR-USD", "BCH-USD", "LINK-USD")))
ok("a coin that is NOT named stays capped - ZEC above all",
   not g.branch_is_level_cap_exempt("ZEC-USD"))
ok("a promoted candidate capping at 3 or fewer exists to test against",
   CAPPED is not None)

section("[2] with NO override, nothing is capped and the exemption is moot")
with Override("live_default"):
    ok("HBAR's $357.70 supports its 10 levels",
       run(g._effective_num_levels(357.70, "HBAR-USD")) == 10)
    ok("and a non-exempt branch gets the same answer - the exemption is a "
       "CEILING exemption, never a floor override",
       run(g._effective_num_levels(357.70, "TIA-USD")) == 10)

if CAPPED:
    cap = g.GRID_LEVEL_SPACING_CANDIDATES[CAPPED]["num_levels"]
    section(f"[3] with the override '{CAPPED}' live (caps at {cap})")
    with Override(CAPPED):
        ok("a NON-exempt branch is still capped - the override keeps working",
           run(g._effective_num_levels(357.70, "TIA-USD")) == cap)
        ok("an EXEMPT branch keeps its allocation-derived count",
           run(g._effective_num_levels(357.70, "HBAR-USD")) == 10)
        ok("THE LIVE CASE: adding $1 to HBAR no longer rewrites 10 -> 3 "
           "under a branch holding 9 slices",
           run(g._effective_num_levels(358.70, "HBAR-USD")) == 10)
        ok("BCH, 6 slices against 10 levels, is the same shape and also safe",
           run(g._effective_num_levels(192.00, "BCH-USD")) == 10)
        ok("every one of the five exempt branches survives a funding at its "
           "live allocation",
           all(run(g._effective_num_levels(a, p)) == 10 for p, a in
               (("XRP-USD", 1268.48), ("XLM-USD", 710.71), ("HBAR-USD", 357.70),
                ("BCH-USD", 191.00), ("LINK-USD", 136.49))))

        section("[4] the floor still binds an exempt branch - it is a ceiling "
                "exemption, not a licence")
        ok("a $12 exempt branch gets 2 levels, not 10: $5 MIN_TRADE_USD still rules",
           run(g._effective_num_levels(12.00, "LINK-USD")) == 2)
        ok("and a $4 exempt branch floors at 1, never 0",
           run(g._effective_num_levels(4.00, "LINK-USD")) == 1)

        section("[5] omitting product_id keeps the OLD behaviour exactly, so a "
                "what-if caller with no branch in hand is unchanged")
        ok("no product_id -> still capped",
           run(g._effective_num_levels(357.70)) == cap)
        ok("None is explicitly not exempt", not g.branch_is_level_cap_exempt(None))
        ok("and an empty string is not exempt either",
           not g.branch_is_level_cap_exempt(""))

section("[6] the three paths that WRITE num_levels all pass the real product_id")
src = open("crypto_grid_bot.py").read()
import re
calls = re.findall(r"_effective_num_levels\(\s*([^)]*?)\)", src, re.S)
writes = [c for c in calls if "branch.allocated_usd" in c or "num_levels = " in c]
ok("create_grid_branch passes product_id",
   "_effective_num_levels(allocated_usd, product_id)" in src)
ok("add-cash and withdraw both pass branch.product_id",
   src.count("branch.allocated_usd, branch.product_id") == 2)
ok("no write site is left calling it on the allocation alone",
   not re.search(r"num_levels = await _effective_num_levels\(\s*branch\.allocated_usd\s*\)", src))

print("\n" + ("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: " + "; ".join(FAILED)))
sys.exit(1 if FAILED else 0)
