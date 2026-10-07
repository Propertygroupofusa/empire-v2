"""An AWARE datetime into a NAIVE column is a silent, total write failure.

Run as written:  python3 test_naive_datetime_columns.py

WHAT HAPPENED, AND WHY IT SURVIVED FIFTY-EIGHT DAYS

closed_trades.closed_at is Column(DateTime) - TIMESTAMP WITHOUT TIME ZONE
in Postgres - and its own default is datetime.utcnow, which is naive.
Three separate writers passed datetime.now(timezone.utc), which is aware.
asyncpg refuses that pairing:

    asyncpg.exceptions.DataError: invalid input for query argument $16
    (can't subtract offset-naive and offset-aware datetimes)

Every one of those writers wraps its insert in try/except and logs
"trade happened, sample lost" at WARNING. So the inserts failed, the bots
carried on, and the table stayed empty.

    2026-08-01  4176677  ClosedTrade created, closed_at naive
    2026-08-10  ed64b93  prop_bot writes it AWARE. First instance.
    2026-09-24  f59747f  the slot governor is created AND alpaca_swing
                         copies the aware write. The governor now reads a
                         table nothing can write to.
    2026-10-03  6000871  the auto-close path copies it a third time.

THE COST WAS NOT BOOKKEEPING. intraday_slots_allowed() computes the
profit factor from closed_trades and earns slots from it. With zero rows
it cannot compute one and holds at the floor, so the live log reads
"Governor: 0 closed trades, needs 20 before more than 1 slot is earned |
ceiling 2 -> 1 slot(s) in force". The account ran at half its permitted
size for thirteen days because of a timezone.

WHY NO TEST CAUGHT IT. SQLite stores an aware datetime without complaint;
only Postgres rejects it. And test_alpaca_growth.py - the one test that
inserts ClosedTrade rows - uses datetime.utcnow(), which is correct. So
the only code in the repository writing a VALID closed_at was the test,
and it proved the reader worked on data that never arrived.

So the guard below is structural rather than behavioural: it reads the
models to learn which columns are naive, then reads the source to find
anyone handing them an aware value. That check does not care which
database is underneath, which is the whole point.
"""
import ast
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

FAILED = []


def ok(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILED.append(label)


# ---------------------------------------------- which columns are naive?
print("\n1. READ THE MODELS: which DateTime columns are NAIVE?")

import tempfile as _tf
_TMP = _tf.mkdtemp(prefix="naive-dt-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP}/t.db"
import models
from sqlalchemy import DateTime

naive_cols = {}          # model class name -> {column names that are naive}
for obj in vars(models).values():
    table = getattr(obj, "__table__", None)
    if table is None:
        continue
    cols = set()
    for c in table.columns:
        if isinstance(c.type, DateTime) and not getattr(c.type, "timezone", False):
            cols.add(c.name)
    if cols:
        naive_cols[obj.__name__] = cols

ok("ClosedTrade is among the models with naive DateTime columns",
   "ClosedTrade" in naive_cols)
ok("closed_at is one of them", "closed_at" in naive_cols.get("ClosedTrade", set()))
print(f"       {len(naive_cols)} model(s) carry naive DateTime columns")


# ---------------------------------------- nobody may hand them an aware value
print("\n2. SCAN THE SOURCE: nobody passes an AWARE value to one")


def _is_aware_now(node):
    """datetime.now(timezone.utc) / datetime.now(tz=...) - anything with a tz."""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if not (isinstance(f, ast.Attribute) and f.attr == "now"):
        return False
    if node.args:                      # datetime.now(timezone.utc)
        return True
    return any(k.arg == "tz" for k in node.keywords)   # datetime.now(tz=...)


offenders = []
scanned = 0
for path in sorted(REPO.glob("**/*.py")):
    rel = path.relative_to(REPO).as_posix()
    if rel.startswith("test_") or "/test_" in rel or rel.startswith("scripts/"):
        continue
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        continue
    scanned += 1
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        cols = naive_cols.get(name)
        if not cols:
            continue
        for kw in node.keywords:
            if kw.arg in cols and _is_aware_now(kw.value):
                offenders.append(f"{rel}:{kw.value.lineno} {name}({kw.arg}=...)")

print(f"       scanned {scanned} modules")
ok("no model with a naive DateTime column is handed an aware value", not offenders)
for o in offenders:
    print("        ->", o)


# ------------------------------------------- the three sites are really fixed
print("\n3. THE THREE WRITERS THAT WERE BROKEN")

for rel, fn in (("alpaca_swing_bot.py", "_record_closed_trade"),
                ("prop_bot.py", "_db_save_closed_trade"),
                ("routers/trading_dashboard.py", "auto-close")):
    src = (REPO / rel).read_text()
    ok(f"{rel} no longer writes closed_at with an aware value",
       "closed_at=datetime.now(timezone.utc)" not in src)
    ok(f"{rel} writes a naive closed_at", "closed_at=datetime.utcnow()" in src)


# ----------------------------------------------- and a row actually persists
print("\n4. A ROW WRITTEN THE NEW WAY SURVIVES A REAL ROUND TRIP")

import asyncio
from datetime import datetime, timezone


async def _roundtrip():
    from sqlalchemy import select
    from database import get_engine, get_session_factory
    from models import Base, ClosedTrade
    async with get_engine().begin() as c:
        await c.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as db:
        db.add(ClosedTrade(bot="alpaca_swing", symbol="RWM", side="long",
                           entry_price=14.425, exit_price=14.435, qty=8.835035,
                           pnl=0.0883, pnl_pct=0.0693,
                           exit_reason="15-min RSI 75.5 > 65 (overbought)",
                           closed_at=datetime.utcnow()))
        await db.commit()
    async with get_session_factory()() as db:
        return (await db.execute(select(ClosedTrade))).scalars().all()


rows = asyncio.run(_roundtrip())
ok("the trade the live log lost is now recorded", len(rows) == 1)
ok("and its closed_at came back NAIVE, matching the column",
   bool(rows) and rows[0].closed_at.tzinfo is None)
_default = models.ClosedTrade.__table__.c.closed_at.default
ok("the column carries a default at all", _default is not None)
try:
    _dv = _default.arg(None) if callable(_default.arg) else _default.arg
except TypeError:
    _dv = _default.arg()
ok("and that default produces a NAIVE value, like the column",
   getattr(_dv, "tzinfo", "missing") is None)

print("\nNOTE: SQLite accepts an aware datetime without complaint, so part 4")
print("cannot reproduce the production failure. Part 2 is the real guard -")
print("it reads the schema and the source and needs no database at all.")

print("\n" + ("ALL CHECKS PASSED" if not FAILED else f"{len(FAILED)} FAILED:"))
for f in FAILED:
    print("   - " + f)
sys.exit(1 if FAILED else 0)
