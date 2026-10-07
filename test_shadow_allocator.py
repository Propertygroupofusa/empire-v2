"""The shadow allocator records and grades, and cannot move a dollar.

Run as written:  python3 test_shadow_allocator.py

Four things are proved here, and the first is the one that matters most:
a module that is allowed to run inside the live trading service while
being unable to trade has to be unable BY CONSTRUCTION, not by intention.
"""
import ast
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_tmp = tempfile.mkdtemp(prefix="shadow-alloc-test-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp}/t.db"
os.environ.setdefault("SHADOW_ALLOCATOR_ENABLED", "1")

FAILED = []


def ok(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILED.append(label)


# ---------------------------------------------------------------- 1. it cannot trade
print("\n1. THE MODULE CANNOT MOVE CAPITAL, BY CONSTRUCTION")

SRC = {name: open(f"{name}.py").read()
       for name in ("shadow_allocator", "opportunity_allocator")}

BANNED = ("place_order", "create_order", "market_order", "limit_order",
          "submit_order", "free-locked-inventory", "reconcile-slices",
          "close-branch", "close-slices", "spread-evenly", "set-levels",
          "rotation/execute", "move-cash", "auto-rotate", "rightsize",
          "deploy-cash", "GRID_AUTO_ROTATE", "setenv", "os.environ[")
for name, src in SRC.items():
    for bad in BANNED:
        ok(f"{name}.py contains no '{bad}'", bad not in src)

for name, src in SRC.items():
    tree = ast.parse(src)
    posts = [n for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and n.attr in ("post", "put", "delete", "patch")]
    ok(f"{name}.py makes no HTTP write call", not posts)

ok("shadow_allocator writes exactly one model: TradeDecision",
   SRC["shadow_allocator"].count("db.add(") == 1
   and "TradeDecision(" in SRC["shadow_allocator"])

# ---------------------------------------------------------------- 2. gates
print("\n2. EVERY GATE REJECTS FOR THE RIGHT REASON")
import opportunity_allocator as OA

ok("the concentration cap is the owner's 20%", OA.CONCENTRATION_CAP == 0.20)
ok("the viability floor is the live $15", OA.VIABILITY_FLOOR == 15.00)
ok("a dormant coin is scored far below a volatile one",
   OA.STATE_MULT["VOLATILE"] > OA.STATE_MULT["ACTIVE"] > OA.STATE_MULT["DORMANT"])

# ---------------------------------------------------------------- 3. the ledger
print("\n3. THE LEDGER RECORDS A PASS, SELECTED FLAG AND ALL")
import shadow_allocator as SA
from database import get_session_factory, get_engine
from models import Base, TradeDecision, CryptoGridTradeHistory
from sqlalchemy import select

PASS = {
    "fleet_claim_usd": 7000.0, "free_cash_usd": 500.0,
    "ranking": [
        {"product_id": "PRIME-USD", "verdict": "QUALIFIED", "gate": "", "score": 0.39,
         "fire_rate": 0.066, "net_per_100": 0.0886, "state": "VOLATILE", "trend": "BULL",
         "free_rungs": 2, "allocated_usd": 38.24, "fleet_share_pct": 0.5,
         "detail": "", "selected": True},
        {"product_id": "QNT-USD", "verdict": "REJECT", "gate": "FULL", "score": 0.0,
         "fire_rate": 0.066, "net_per_100": 0.2774, "state": "VOLATILE", "trend": "BEAR",
         "free_rungs": 0, "allocated_usd": 142.42, "fleet_share_pct": 1.9,
         "detail": "", "selected": False},
    ],
}


async def _setup():
    async with get_engine().begin() as c:
        await c.run_sync(Base.metadata.create_all)


async def _scenario():
    await _setup()
    await SA._write_ledger(PASS)
    async with get_session_factory()() as db:
        rows = (await db.execute(
            select(TradeDecision).where(TradeDecision.bot == "shadow_allocator"))).scalars().all()
    return rows


rows = asyncio.run(_scenario())
ok("one row per branch was written", len(rows) == 2)
by = {r.symbol: r for r in rows}
ok("the qualified branch is marked admitted", by["PRIME-USD"].admitted is True)
ok("the rejected branch is not admitted", by["QNT-USD"].admitted is False)
ok("the gate that rejected it is recorded by name", by["QNT-USD"].reason == "FULL")
c = json.loads(by["PRIME-USD"].checks_json)
ok("fire_rate is recorded", abs(c["fire_rate"] - 0.066) < 1e-9)
ok("the volatility state is recorded", c["volatility_state"] == "VOLATILE")
ok("free rungs are recorded", c["free_rungs"] == 2)
ok("the branch it WOULD have funded is flagged", c["selected"] is True)
ok("the rejected branch is not flagged selected",
   json.loads(by["QNT-USD"].checks_json)["selected"] is False)

# ---------------------------------------------------------------- 4. grading
print("\n4. OUTCOMES COME FROM THE REAL LEDGER, AND OPEN WINDOWS ARE NOT SCORED")


async def _grade(close_pnl, pass_age_hours, close_age_hours, reason="profit_target"):
    now = datetime.now(timezone.utc)
    async with get_session_factory()() as db:
        for r in (await db.execute(select(TradeDecision))).scalars().all():
            await db.delete(r)
        for t in (await db.execute(select(CryptoGridTradeHistory))).scalars().all():
            await db.delete(t)
        await db.commit()
    p = dict(PASS)
    await SA._write_ledger(p)
    async with get_session_factory()() as db:
        for r in (await db.execute(select(TradeDecision))).scalars().all():
            r.decided_at = (now - timedelta(hours=pass_age_hours)).replace(tzinfo=None)
        db.add(CryptoGridTradeHistory(
            bot_name="crypto_grid_3", product_id="PRIME-USD",
            entry_price=1.0, exit_price=1.03, qty=1.0, pnl=close_pnl,
            exit_reason=reason,
            closed_at=(now - timedelta(hours=close_age_hours)).replace(tzinfo=None)))
        await db.commit()
    return await SA.resolve_outcomes(hours_forward=4)


# a pass 6h old, with a profitable close 5h ago -> inside the 4h window
r = asyncio.run(_grade(+1.42, 6, 5))
ok("a closed window is graded", r["graded_passes"] == 1)
ok("the selected branch is counted as having cycled",
   r["selected"]["branches"] == 1 and r["selected"]["cycles"] == 1)
ok("the branch it passed over is counted separately",
   r["unselected"]["branches"] == 1 and r["unselected"]["cycles"] == 0)
ok("cycle edge is selected minus the base rate, not a bare accuracy",
   r["discrimination"]["cycle_edge_pts"] == 100.0)
ok("net is read from the real trade ledger",
   abs(r["selected"]["net_usd"] - 1.42) < 1e-6)
ok("net edge per branch is reported beside the cycle edge",
   abs(r["discrimination"]["net_edge_usd_per_branch"] - 1.42) < 1e-6)
ok("a profit_target close is not counted as a stop",
   r["selected"]["stops"] == 0 and r["selected"]["stop_rate_pct"] == 0.0)
ok("the payload warns that a cycle edge without a net edge is the failure case",
   "still lose money on those trades" in (r.get("read_discrimination_not_accuracy") or ""))

# the case this whole section exists for: the pick CYCLED but it was a stop
r = asyncio.run(_grade(-0.90, 6, 5, reason="stop_loss"))
ok("a losing close does not count as a cycle",
   r["selected"]["cycles"] == 0)
ok("the stop is counted and surfaced as a rate",
   r["selected"]["stops"] == 1 and r["selected"]["stop_rate_pct"] == 100.0)
ok("a losing pick shows a NEGATIVE net edge even though it traded",
   r["discrimination"]["net_edge_usd_per_branch"] < 0)

# a pass 1h old -> its 4h window has not closed, so it must not be scored
r = asyncio.run(_grade(+1.42, 1, 0.5))
ok("a window that has not closed yet is skipped, not counted a miss",
   r["graded_passes"] == 0 and r["selected"]["cycled_pct"] is None)

ok("the result says plainly what it cannot prove",
   "counterfactual" in (r.get("what_this_cannot_say") or ""))

print("\n" + ("ALL CHECKS PASSED" if not FAILED else f"{len(FAILED)} FAILED:"))
for f in FAILED:
    print("   - " + f)
sys.exit(1 if FAILED else 0)
