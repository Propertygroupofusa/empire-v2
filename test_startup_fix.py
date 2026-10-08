"""The one-shot boot task: it must run once, write only the two things it
is for, and never report a write it did not make.

These tests drive the REAL functions in startup_fix.py against a fake
session. They do not re-implement the apply loop - re-implementing the
code under test is how this session once declared a broken page healthy.
The fake session answers `select(X).where(X.col == v)` by introspecting
the statement, so the module's own queries run unmodified.

WHAT IS PINNED HERE
  1. inert without a ticket, and silent - not one query
  2. STOP_TRADING stops it
  3. the happy path really writes: num_levels on the branch row, qty on
     the slice rows, and the one-shot marker
  4. it cannot run twice on the same ticket
  5. a level below the branch's open slices is never written
  6. an unreadable wallet writes nothing (a gap is not a zero)
  7. NO dust-filtered fallback: without the unfiltered owned-units map it
     writes nothing, because holdings cannot answer "is this owned"
  8. OWNED, not AVAILABLE - staked and locked coin survives, and the test
     proves the opposite choice would have deleted it
  9. a write-off over the ceiling is refused
 10. a plan whose rows cannot be found reports NOTHING WAS CHANGED
 11. attempts are bounded, so a crash loop cannot re-run forever
 12. no order is placed, and nothing but num_levels and slice qty is written
 13. a cold fleet is waited for, not acted on
 14. a fleet that never arrives is UNKNOWN and never settles
 15. an unsettled step leaves the ticket unspent, and the next boot writes
     what the first one lost - the exact fault that dropped the levels on
     2026-10-03
 16. both level outcomes reach the durable activity log, so "why it wrote
     nothing" survives the restart that erased the first run's report
"""
import asyncio
import sys

sys.path.insert(0, "/home/user/empire-v2")
from sqlalchemy import select  # noqa: E402

import slice_reconcile  # noqa: E402
import startup_fix  # noqa: E402
from models import CryptoGridBranch, CryptoGridSlice, TradingBotState  # noqa: E402

# THE SHIPPED DEFAULT IS EMPTY, AND THESE SCENARIOS TEST THE MECHANISM.
#
# LEVELS was emptied 2026-10-08 at the account owner's instruction: both of
# its entries had been overtaken by the fleet, and the LINK one would have
# LOWERED that branch from 10 levels to 6. See startup_fix's note on it,
# and test_startup_fix_levels_emptied.py, which pins the default itself.
#
# Everything below exercises the level-writing MACHINERY - the cold-fleet
# retry, the unspent ticket, the refusal logging - which must keep working
# for any future deliberate change. So these scenarios state their own
# request rather than inheriting whatever the shipped default happens to
# be. The default is asserted from the SOURCE further down, so overriding
# the attribute here cannot hide a change to it.
MECHANISM_LEVELS = {"XRP-USD": 10, "LINK-USD": 6}
startup_fix.LEVELS = dict(MECHANISM_LEVELS)

fail = 0


def ok(label, cond, detail=""):
    global fail
    print(f"{'PASS' if cond else 'FAIL'}  {label}"
          + ("" if cond or not detail else f"\n        -> {detail}"))
    if not cond:
        fail += 1


# ----------------------------------------------------------------- fakes
class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class FakeDB:
    """Answers select(X).where(X.col == value) off an in-memory store."""

    def __init__(self, store, log):
        self.store, self.log = store, log

    async def execute(self, stmt):
        ent = stmt.column_descriptions[0]["entity"]
        w = stmt.whereclause
        col, val = w.left.key, w.right.value
        self.log.append(("select", ent.__name__, col, val))
        rows = [r for r in self.store.get(ent.__name__, [])
                if getattr(r, col, None) == val]
        return FakeResult(rows)

    def add(self, row):
        self.log.append(("add", type(row).__name__))
        self.store.setdefault(type(row).__name__, []).append(row)

    async def delete(self, row):
        self.log.append(("delete", type(row).__name__, getattr(row, "id", None)))
        rows = self.store.get(type(row).__name__, [])
        if row in rows:
            rows.remove(row)

    async def commit(self):
        self.log.append(("commit",))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeGrid:
    def __init__(self, status, store, raise_on_status=False, cold_calls=0):
        self.status, self.store = status, store
        self.raise_on_status = raise_on_status
        self.cold_calls = cold_calls      # boots served an empty fleet first
        self.status_calls = 0
        self.log = []
        self.activity = []

    async def get_grid_status(self):
        self.status_calls += 1
        if self.raise_on_status:
            raise RuntimeError("grid status unavailable")
        if self.status_calls <= self.cold_calls:
            return {"branches": []}       # the shape that lost the level change
        return self.status

    def get_session_factory(self):
        store, log = self.store, self.log
        return lambda: FakeDB(store, log)

    async def _log_activity_safe(self, *a, **k):
        self.activity.append(a)

    def activity_kinds(self):
        return [x[2] for x in self.activity if len(x) > 2]


def branch_row(bot_name, levels):
    r = CryptoGridBranch()
    r.bot_name, r.num_levels = bot_name, levels
    return r


def slice_row(i, qty):
    r = CryptoGridSlice()
    r.id, r.qty = i, qty
    return r


def sl(i, qty, price):
    return {"id": i, "qty": qty, "entry_price": price, "opened_at": None}


# Live shape, measured 2026-10-03.
def live_status():
    return {"branches": [
        {"product_id": "XRP-USD", "bot_name": "crypto_grid_6", "num_levels": 3,
         "allocated_usd": 2256.22, "current_price": 2.80,
         "slices": [sl(100 + n, 38.0, 2.86) for n in range(7)]},
        {"product_id": "LINK-USD", "bot_name": "crypto_grid_16", "num_levels": 3,
         "allocated_usd": 137.87, "current_price": 21.0,
         "slices": [sl(200 + n, 1.44, 21.07) for n in range(3)]},
    ]}


def census_owned(**owned):
    return {"available": True, "held_including_zero": dict(owned),
            "holdings": [{"asset": k, "units": v} for k, v in owned.items()]}


def run(coro):
    return asyncio.run(coro)


def fresh_store(levels=(("crypto_grid_6", 3), ("crypto_grid_16", 3)), slices=()):
    return {"CryptoGridBranch": [branch_row(n, lv) for n, lv in levels],
            "CryptoGridSlice": [slice_row(i, q) for i, q in slices],
            "TradingBotState": []}


# ----------------------------------------------------------------- 1
print("\n[1] inert without a ticket - and silent")
store = fresh_store()
g = FakeGrid(live_status(), store)
rep = run(startup_fix.run_at_boot(g, tkt=None))
ok("ran is False", rep.get("ran") is False, rep)
ok("the reason is NO_TICKET", rep.get("reason") == "NO_TICKET", rep)
ok("not one query was issued", g.log == [], g.log)
ok("the module names the env var it waits for",
   startup_fix.TICKET_ENV == "STARTUP_FIX_TICKET")

# ----------------------------------------------------------------- 2
print("\n[2] STOP_TRADING stops it")
import os  # noqa: E402
os.environ["STOP_TRADING"] = "true"
g = FakeGrid(live_status(), fresh_store())
rep = run(startup_fix.run_at_boot(g, tkt="t-stop"))
os.environ.pop("STOP_TRADING")
ok("ran is False", rep.get("ran") is False, rep)
ok("the reason is STOP_TRADING", rep.get("reason") == "STOP_TRADING", rep)
ok("not one query was issued", g.log == [], g.log)

# ----------------------------------------------------------------- 3
print("\n[3] the happy path writes num_levels, slice qty and the marker")
# XRP tracked 7 x 38 = 266 units; the wallet owns 190, so 76 units are
# over-claimed. LINK tracked 4.32 against 4.32 owned - nothing to do.
store = fresh_store(slices=[(100 + n, 38.0) for n in range(7)]
                           + [(200 + n, 1.44) for n in range(3)])
g = FakeGrid(live_status(), store)


async def _c(**kw):
    """The census reading, with the unfiltered OWNED-units map."""
    return census_owned(**kw)


rep = run(startup_fix.run_at_boot(
    g, tkt="t-happy", census_fn=lambda: _c(XRP=190.0, LINK=4.32)))
lv = rep.get("levels") or {}
rc = rep.get("reconcile") or {}
xrp = [r for r in store["CryptoGridBranch"] if r.bot_name == "crypto_grid_6"][0]
link = [r for r in store["CryptoGridBranch"] if r.bot_name == "crypto_grid_16"][0]
ok("ran is True", rep.get("ran") is True, rep.get("reason"))
ok("XRP's row really holds 10 levels now", xrp.num_levels == 10, xrp.num_levels)
ok("LINK's row really holds 6 levels now", link.num_levels == 6, link.num_levels)
ok("two level rows are reported written", lv.get("rows_written") == 2, lv)
ok("the reconcile wrote at least one slice row", (rc.get("rows_written") or 0) >= 1, rc)
ok("the total counts both", rep.get("rows_written_total") == 2 + rc["rows_written"], rep)
ok("the marker was set to DONE",
   any(r.bot_name == "startup_fix:t-happy" and r.base_capital == startup_fix.DONE
       for r in store["TradingBotState"]),
   [(r.bot_name, r.base_capital) for r in store["TradingBotState"]])
ok("the report does not claim a cleared figure larger than what it wrote",
   (rc.get("cost_basis_removed_usd") or 0) <= (rc.get("cost_basis_planned_usd") or 0),
   rc)

# ----------------------------------------------------------------- 4
print("\n[4] it cannot run twice on the same ticket")
before = (xrp.num_levels, link.num_levels, len(store["CryptoGridSlice"]))
rep2 = run(startup_fix.run_at_boot(
    g, tkt="t-happy", census_fn=lambda: _c(XRP=190.0, LINK=4.32)))
after = (xrp.num_levels, link.num_levels, len(store["CryptoGridSlice"]))
ok("the second run did not run", rep2.get("ran") is False, rep2)
ok("the reason is ALREADY_DONE", rep2.get("reason") == "ALREADY_DONE", rep2)
ok("nothing changed on the second run", before == after, f"{before} -> {after}")

# ----------------------------------------------------------------- 5
print("\n[5] a level below the branch's open slices is never written")
store = fresh_store(slices=[(100 + n, 38.0) for n in range(7)])
g = FakeGrid(live_status(), store)
out = run(startup_fix.apply_levels(g, wanted={"XRP-USD": 2}))
xrp = store["CryptoGridBranch"][0]
ok("nothing was written", out.get("rows_written") == 0, out)
ok("the row still holds its original 3 levels", xrp.num_levels == 3, xrp.num_levels)
ok("XRP-USD is named as refused", "XRP-USD" in (out.get("refused") or []), out)
ok("the report says NOTHING WAS WRITTEN",
   "NOTHING WAS WRITTEN" in (out.get("detail") or ""), out.get("detail"))

# ----------------------------------------------------------------- 6
print("\n[6] an unreadable wallet writes nothing")
store = fresh_store(slices=[(100 + n, 38.0) for n in range(7)])
g = FakeGrid(live_status(), store)


async def _blind():
    return {"available": False}


out = run(startup_fix.apply_reconcile(g, census_fn=_blind))
ok("the status names the unreadable wallet",
   out.get("status") == "SKIPPED_WALLET_UNREADABLE", out)
ok("nothing was written", out.get("rows_written") == 0, out)
ok("every slice row survives", len(store["CryptoGridSlice"]) == 7,
   len(store["CryptoGridSlice"]))
ok("it says a gap is not a zero", "gap is not a zero" in (out.get("detail") or ""),
   out.get("detail"))

# ----------------------------------------------------------------- 7
print("\n[7] no dust-filtered fallback: no unfiltered map, no write")


async def _dust_only():
    # Exactly the shape that made four branches unreconcilable forever:
    # holdings present, the unfiltered owned-units map absent.
    return {"available": True, "holdings": [{"asset": "XRP", "units": 190.0}]}


store = fresh_store(slices=[(100 + n, 38.0) for n in range(7)])
g = FakeGrid(live_status(), store)
out = run(startup_fix.apply_reconcile(g, census_fn=_dust_only))
ok("the status names the missing map",
   out.get("status") == "SKIPPED_NO_UNFILTERED_WALLET_MAP", out)
ok("nothing was written", out.get("rows_written") == 0, out)
ok("every slice row survives", len(store["CryptoGridSlice"]) == 7,
   len(store["CryptoGridSlice"]))
src = open("/home/user/empire-v2/startup_fix.py").read()
ok("the module never reads the dust-filtered holdings list as a wallet",
   'census.get("holdings")' not in src and "census['holdings']" not in src,
   "a fallback to holdings would reintroduce the bug this skips for")

# ----------------------------------------------------------------- 8
print("\n[8] OWNED, not AVAILABLE - staked and locked coin survives")
# SOL: 1.035 owned, 0.776 of it staked, so 0.259 available. A branch
# tracking 1.0 unit is fully backed by what is OWNED.
sol_status = {"branches": [
    {"product_id": "SOL-USD", "bot_name": "crypto_grid_sol", "num_levels": 3,
     "allocated_usd": 200.0, "current_price": 150.0,
     "slices": [sl(300, 1.0, 150.0)]}]}
store = {"CryptoGridBranch": [branch_row("crypto_grid_sol", 3)],
         "CryptoGridSlice": [slice_row(300, 1.0)], "TradingBotState": []}
g = FakeGrid(sol_status, store)
out = run(startup_fix.apply_reconcile(g, census_fn=lambda: _c(SOL=1.035)))
ok("against OWNED units nothing is written", out.get("rows_written") == 0, out)
ok("the staked slice row survives", len(store["CryptoGridSlice"]) == 1)
ok("the status is NOTHING_TO_DO", out.get("status") == "NOTHING_TO_DO", out)
# And the choice is load-bearing: against AVAILABLE units the same slice
# would have been planned for a write-off.
_a, _r = slice_reconcile.plan([sl(300, 1.0, 150.0)], 0.259, price=150.0)
ok("against AVAILABLE units the same slice WOULD have been written off",
   _r.get("status") == "READY" and _a, _r)

# ----------------------------------------------------------------- 9
print("\n[9] a write-off over the ceiling is refused")
store = fresh_store(slices=[(100 + n, 38.0) for n in range(7)])
g = FakeGrid(live_status(), store)
out = run(startup_fix.apply_reconcile(g, census_fn=lambda: _c(XRP=0.0, LINK=0.0),
                                      max_writeoff_usd=1.0))
ok("the status names the ceiling", out.get("status") == "REFUSED_OVER_CEILING", out)
ok("nothing was written", out.get("rows_written") == 0, out)
ok("every slice row survives", len(store["CryptoGridSlice"]) == 7,
   len(store["CryptoGridSlice"]))
ok("it calls the plan a measurement to check",
   "measurement to check" in (out.get("detail") or ""), out.get("detail"))
ok("the ceiling leaves headroom over the audited $1,157.41 plan",
   startup_fix.MAX_WRITEOFF_USD >= 1200.0, startup_fix.MAX_WRITEOFF_USD)

# ----------------------------------------------------------------- 10
print("\n[10] a plan whose rows cannot be found reports NOTHING WAS CHANGED")
store = fresh_store(slices=[])           # the plan exists; no rows to write
g = FakeGrid(live_status(), store)
out = run(startup_fix.apply_reconcile(g, census_fn=lambda: _c(XRP=190.0, LINK=4.32)))
ok("a branch was planned", (out.get("branch_count") or 0) >= 1, out)
ok("the status is NOTHING_WRITTEN", out.get("status") == "NOTHING_WRITTEN", out)
ok("rows_written is 0", out.get("rows_written") == 0, out)
ok("the cleared figure is 0.0, not the planned figure",
   out.get("cost_basis_removed_usd") == 0.0, out)
ok("the detail says NOTHING WAS CHANGED",
   "NOTHING WAS CHANGED" in (out.get("detail") or ""), out.get("detail"))
ok("it does not use the word corrected",
   "corrected" not in (out.get("detail") or ""), out.get("detail"))

# ----------------------------------------------------------------- 11
print("\n[11] attempts are bounded - a crash loop cannot re-run forever")
store = fresh_store()
reasons = []
for boot in range(startup_fix.MAX_ATTEMPTS + 2):
    g = FakeGrid(live_status(), store, raise_on_status=True)
    r = run(startup_fix.run_at_boot(g, tkt="t-crash", census_fn=lambda: _c(XRP=1.0)))
    reasons.append((r.get("ran"), r.get("reason"), r.get("failed_steps")))
ok(f"the first {startup_fix.MAX_ATTEMPTS} boots ran and both steps failed",
   all(x[0] is True and x[2] == ["levels", "reconcile"]
       for x in reasons[:startup_fix.MAX_ATTEMPTS]), reasons)
ok("every later boot refuses as ATTEMPTS_EXHAUSTED",
   all(x[0] is False and x[1] == "ATTEMPTS_EXHAUSTED"
       for x in reasons[startup_fix.MAX_ATTEMPTS:]), reasons)
ok("a failed run is never marked done",
   all(r.base_capital != startup_fix.DONE for r in store["TradingBotState"]),
   [(r.bot_name, r.base_capital) for r in store["TradingBotState"]])
ok("a raised step is reported as FAILED, never as a write",
   all(x[2] == ["levels", "reconcile"] for x in reasons[:startup_fix.MAX_ATTEMPTS]))

# ----------------------------------------------------------------- 12
print("\n[12] it places no order and writes nothing but levels and slice qty")
import re  # noqa: E402
for bad in ("place_order", "create_order", "market_order", "place_market",
            "sell(", "buy(", "allocated_usd =", "reference_price =",
            "GRID_CASH_RESERVE", "num_levels = row"):
    ok(f"no {bad!r} in the module", bad not in src)
assigns = sorted(set(re.findall(r"^\s*row\.(\w+)\s*=", src, re.M)))
ok("the only row attributes written are num_levels, qty and base_capital",
   assigns == ["base_capital", "num_levels", "qty"], assigns)
ok("the SHIPPED default requests no level change at all",
   re.search(r"^LEVELS = \{\}\s*$", src, re.M) is not None,
   "LEVELS is not empty in the file - a level change was put back into the "
   "startup ticket; read startup_fix's note on why it was emptied")
ok("the attempt is claimed before the work, not after",
   src.index("_claim_attempt(grid.get_session_factory") < src.index("apply_levels(grid)"),
   "an attempt counted after the work would let a crash loop re-run forever")
ok("the module is inert unless the environment arms it",
   'os.getenv(TICKET_ENV)' in src)

# ----------------------------------------------------------------- 13
print("\n[13] a cold fleet is waited for, not acted on")
startup_fix.READY_SLEEP_SECONDS = 0.0      # no real waiting in a test
store = fresh_store()
g = FakeGrid(live_status(), store, cold_calls=2)
out = run(startup_fix.apply_levels(g))
xrp = [r for r in store["CryptoGridBranch"] if r.bot_name == "crypto_grid_6"][0]
ok("it kept asking until the fleet was readable", g.status_calls == 3, g.status_calls)
ok("and then wrote the levels", out.get("rows_written") == 2, out)
ok("XRP really holds 10 levels", xrp.num_levels == 10, xrp.num_levels)
ok("the step is settled", out.get("settled") is True, out)

# ----------------------------------------------------------------- 14
print("\n[14] a fleet that never arrives is UNKNOWN, and never settled")
startup_fix.READY_TRIES = 2
store = fresh_store()
g = FakeGrid({"branches": []}, store, cold_calls=99)
out = run(startup_fix.apply_levels(g))
ok("the status names the unreadable fleet",
   out.get("status") == "UNKNOWN_FLEET_NOT_READABLE", out)
ok("nothing was written", out.get("rows_written") == 0, out)
ok("the levels row is untouched",
   store["CryptoGridBranch"][0].num_levels == 3,
   store["CryptoGridBranch"][0].num_levels)
ok("it is NOT settled", out.get("settled") is False, out)
ok("it calls itself UNKNOWN, not a refusal", "UNKNOWN" in (out.get("detail") or ""),
   out.get("detail"))

# ----------------------------------------------------------------- 15
print("\n[15] an unsettled step leaves the ticket unspent - the fault that lost "
      "the level change")
store = fresh_store(slices=[(100 + n, 38.0) for n in range(7)]
                           + [(200 + n, 1.44) for n in range(3)])
g = FakeGrid({"branches": []}, store, cold_calls=99)
rep = run(startup_fix.run_at_boot(
    g, tkt="t-cold", census_fn=lambda: _c(XRP=266.0, LINK=4.32)))
ok("the run reports levels as unsettled",
   "levels" in (rep.get("unsettled_steps") or []), rep)
ok("the ticket is NOT marked done", rep.get("marked_done") is False, rep)
ok("the marker never reads DONE",
   all(r.base_capital != startup_fix.DONE for r in store["TradingBotState"]),
   [(r.bot_name, r.base_capital) for r in store["TradingBotState"]])
ok("the detail says the next boot will try again",
   "next boot will try again" in (rep.get("detail") or ""), rep.get("detail"))
startup_fix.READY_TRIES = 20
# A second boot really does retry, and once the fleet is there it writes.
g2 = FakeGrid(live_status(), store)
rep2 = run(startup_fix.run_at_boot(g2, tkt="t-cold", census_fn=lambda: _c(XRP=190.0, LINK=4.32)))
ok("the second boot ran", rep2.get("ran") is True, rep2)
ok("and wrote the levels it had lost",
   [r for r in store["CryptoGridBranch"] if r.bot_name == "crypto_grid_6"][0].num_levels == 10,
   [(r.bot_name, r.num_levels) for r in store["CryptoGridBranch"]])
ok("now it is marked done", rep2.get("marked_done") is True, rep2)

# ----------------------------------------------------------------- 16
print("\n[16] both level outcomes are logged durably, not only to memory")
store = fresh_store()
g = FakeGrid(live_status(), store)
run(startup_fix.apply_levels(g))
ok("a write is logged as LEVELS", g.activity_kinds().count("LEVELS") == 2,
   g.activity_kinds())
g = FakeGrid(live_status(), fresh_store())
run(startup_fix.apply_levels(g, wanted={"XRP-USD": 2}))
ok("a refusal is logged too, so why it did not write survives a restart",
   g.activity_kinds().count("LEVELS") == 1, g.activity_kinds())
ok("the refusal line says it was not changed",
   any("NOT changed" in x[3] for x in g.activity if len(x) > 3),
   [x[3][:80] for x in g.activity if len(x) > 3])

print("\n" + ("ALL PASS" if not fail else f"{fail} FAILURE(S)"))
sys.exit(1 if fail else 0)
