"""The floor at the chokepoint, and who called.

THE INCIDENT THIS PINS

2026-10-04, between two hourly guard passes: the FLOKI-USD branch
(crypto_grid_5) closed its last slice at 21:29:45 for +$0.10 on a clean
profit_target, went flat, and its row was gone before the next read.
It took $63.73 of claim with it. The money was fully accounted for -
allocation_backed passed, the cash landed in the wallet as unallocated -
but $43.48 of FLOKI was left in the account owned by NO branch, 75% of
it under a resting order with nothing managing it.

What deleted it could not be established from outside:
  - profit_harvest keeps $15 and never takes more than earned
  - branch_rightsize floors a coinless branch at $15
  - auto_rotate read fully_off, and its apply is balanced anyway
  - the write guard recorded ZERO HTTP write attempts, across a process
    uptime that started hours BEFORE the deletion
Nothing recorded who called, so the answer is still unknown.

TWO MODULES HAD ALREADY FOUND THIS HAZARD AND EACH PATCHED ITS OWN SIDE:
rotation_task.KEEP_BRANCH_ALIVE_USD and profit_harvest.KEEP_BRANCH_ALIVE_USD
are both 15.0, both added after a near-miss. Neither floor was at the
function that does the deleting, so the third caller had none at all.

Run: python3 test_branch_keep_alive_floor.py
"""
import asyncio
import os
import sys
from unittest.mock import patch

import crypto_grid_bot as grid
import profit_harvest
import rotation_task

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


class FakeBranch:
    def __init__(self, name, alloc, product="FLOKI-USD"):
        self.bot_name, self.allocated_usd, self.product_id = name, alloc, product
        self.locked = False
        self.num_levels = 3
        self.peak_equity = alloc


class FakeResult:
    def __init__(self, v):
        self._v = v

    def scalar_one_or_none(self):
        return self._v

    def scalars(self):
        return self

    def first(self):
        return self._v


class FakeDB:
    def __init__(self, branch, slices=None):
        self.branch, self.slices = branch, slices
        self.deleted = []
        self.committed = 0

    async def execute(self, stmt):
        # First call fetches the branch, later ones the slice rows.
        txt = str(stmt).lower()
        if "slice" in txt:
            return FakeResult(self.slices)
        return FakeResult(self.branch)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.committed += 1

    async def refresh(self, obj):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def run_withdraw(alloc, amount, **kw):
    """Returns (result_or_exception, db)."""
    branch = FakeBranch("crypto_grid_5", alloc)
    db = FakeDB(branch)
    with patch.object(grid, "get_session_factory", lambda: (lambda: db)), \
         patch.object(grid, "_effective_num_levels",
                      lambda *_a, **_k: _coro(3)):
        try:
            return asyncio.run(
                grid.withdraw_from_grid_branch("crypto_grid_5", amount, **kw)), db
        except Exception as e:
            return e, db


async def _coro(v):
    return v


# --- the floor ------------------------------------------------------------
r, db = run_withdraw(63.73, 63.73)
ok("a full drain is REFUSED by default", isinstance(r, ValueError))
ok("and the row survives", db.deleted == [])
ok("the refusal names the floor", isinstance(r, ValueError) and "15.00" in str(r))
ok("the refusal names what it would orphan",
   isinstance(r, ValueError) and "out of the fleet" in str(r))
ok("the refusal names who asked",
   isinstance(r, ValueError) and "Asked for by:" in str(r))
ok("the refusal says how much IS withdrawable",
   isinstance(r, ValueError) and "48.73" in str(r))

r, db = run_withdraw(63.73, 50.0)
ok("a withdrawal leaving under $15 is refused too", isinstance(r, ValueError))
ok("and that row survives as well", db.deleted == [])

r, db = run_withdraw(63.73, 48.73)
ok("a withdrawal leaving EXACTLY $15.00 goes through",
   not isinstance(r, Exception))
ok("and deletes nothing", db.deleted == [])

r, db = run_withdraw(63.73, 10.0)
ok("an ordinary partial withdrawal is untouched", not isinstance(r, Exception))

# --- the deliberate exception --------------------------------------------
r, db = run_withdraw(63.73, 63.73, allow_delete=True)
ok("allow_delete=True still closes a branch on purpose",
   isinstance(r, dict) and r.get("branch_deleted") is True)
ok("and really deletes the row", len(db.deleted) == 1)
ok("the owner's stated capability survives",
   isinstance(r, dict) and r.get("amount") == 63.73)

# --- attribution ----------------------------------------------------------
r, _ = run_withdraw(63.73, 63.73, allow_delete=True)
ok("a delete result carries the caller", isinstance(r, dict) and r.get("caller"))
ok("the caller is never 'unattributed' from a real stack",
   isinstance(r, dict) and r.get("caller") != "unattributed")
ok("auto-attribution skips the event loop",
   isinstance(r, dict) and "asyncio" not in str(r.get("caller")))


# The FUNCTION name is the part that matters - "something in rotation_task"
# is a worse answer than "rotation_task.apply". Awaiting directly keeps the
# frame chain intact, which is what the real callers look like; going
# through asyncio.run (as run_withdraw does) leaves the loop between us and
# the caller and the walk can only reach module level.
async def _a_named_exit_path(branch, db):
    with patch.object(grid, "get_session_factory", lambda: (lambda: db)), \
         patch.object(grid, "_effective_num_levels", lambda *_a, **_k: _coro(3)):
        return await grid.withdraw_from_grid_branch(
            branch.bot_name, branch.allocated_usd, allow_delete=True)


_b = FakeBranch("crypto_grid_5", 63.73)
_r = asyncio.run(_a_named_exit_path(_b, FakeDB(_b)))
ok("auto-attribution names the calling FUNCTION, not just the module",
   "_a_named_exit_path" in str(_r.get("caller")))
ok("and it names the module it lives in",
   "__main__" in str(_r.get("caller")))

r, _ = run_withdraw(63.73, 63.73, allow_delete=True, caller="a named caller")
ok("an explicit caller= wins over the frame walk",
   isinstance(r, dict) and r.get("caller") == "a named caller")

ok("the helper never raises, whatever the stack",
   grid._attributed_caller(skip=99) == "unattributed")

# --- the floors must agree across the three modules -----------------------
ok("crypto_grid_bot's floor is the $15 viability floor",
   grid.GRID_KEEP_BRANCH_ALIVE_USD == 15.0)
ok("it is MIN_TRADE_USD x 3, not a magic number",
   grid.GRID_KEEP_BRANCH_ALIVE_USD == grid.MIN_TRADE_USD * 3)
ok("rotation_task agrees",
   rotation_task.KEEP_BRANCH_ALIVE_USD == grid.GRID_KEEP_BRANCH_ALIVE_USD)
ok("profit_harvest agrees",
   profit_harvest.KEEP_BRANCH_ALIVE_USD == grid.GRID_KEEP_BRANCH_ALIVE_USD)

# --- the endpoint keeps the owner's deliberate close ----------------------
ROUTER = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "routers", "trading_dashboard.py")).read()
RCODE = "\n".join(l for l in ROUTER.splitlines() if not l.lstrip().startswith("#"))
ok("the withdraw endpoint can still request a close",
   "allow_delete: bool = False" in RCODE)
ok("and it defaults to NOT deleting", "allow_delete: bool = True" not in RCODE)
ok("the endpoint names itself as the caller",
   'caller="dashboard POST' in RCODE)

SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "crypto_grid_bot.py")).read()
CODE = "\n".join(l for l in SRC.splitlines() if not l.lstrip().startswith("#"))
ok("there is still exactly ONE place that deletes a branch",
   CODE.count("await db.delete(branch)") == 1)
ok("the floor is checked before the subtraction",
   CODE.index("GRID_KEEP_BRANCH_ALIVE_USD and not allow_delete")
   < CODE.index("branch.allocated_usd -= amount"))
ok("add_cash logs its caller too", "called by: {_who}" in CODE)

failed = [l for l, c in checks if not c]
for l, c in checks:
    print(f"  {'PASS' if c else 'FAIL'}  {l}")
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)
