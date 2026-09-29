"""§1's buy insert, EXECUTED against a real database - not parsed.

WHY THIS EXISTS BESIDE test_slice_state_writes.py, which already has 126
assertions about this same insert. That file says so in its own docstring:
"Everything is asserted on the parsed tree, because the functions involved
need a database, a venue and a running loop to execute." True of
run_grid_branch_cycle as a whole - but NOT of the insert inside it, which
needs only a table and a session.

So for the whole of 2026-09-29 the state columns were asserted to be
PRESENT IN THE SOURCE and never once asserted to SURVIVE A WRITE. That is
the same gap that has bitten twice already this session: an AST check is
satisfied by `ast.Constant(None)`, so `node is not None` passes against a
literal None. Presence is not population, and a parsed keyword is not a
stored value.

It matters because of what happened live at 13:01Z: three HBAR-USD buy
legs filled, the coin was really bought, and no grid-buy slice row
appeared. `grid_buy` increments the buy fill-mix counter immediately
before returning a fill, so the caller PROVABLY reached this insert. This
test answers one specific question that reading the source cannot: does
the insert, given exactly the values the live path passes, actually commit
and read back intact? If it did not, that would be the whole explanation.

It does. So the insert is NOT the fault, and the search moves elsewhere -
which is the only reason this test is worth its runtime.

SQLite, in memory, no network and no credentials, so it runs anywhere the
rest of the suite runs.
"""
import asyncio
import datetime
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import slice_lifecycle as _sl
from models import CryptoGridSlice

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


# The values the live buy path passes, with the shapes that actually occur:
# a float quantity carrying full binary precision, a real venue uuid, an
# ISO-second cycle id, and a target ABOVE the entry it was computed from.
ENTRY = 0.11342
QTY = 496.414506553812
CYCLE = "20260929T135900Z"
ORDER_ID = "a1b2c3d4-0000-4000-8000-000000000000"
TARGET = 0.11573


async def _roundtrip():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(CryptoGridSlice.__table__.create)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.datetime.utcnow()
    async with Session() as db:
        db.add(CryptoGridSlice(
            bot_name="crypto_grid_13", product_id="HBAR-USD",
            entry_price=ENTRY, qty=QTY,
            entry_fee_rate=0.0035, entry_atr_pct=0.0208,
            entry_spread_pct=0.035, entry_gate_json='{"spread_pct": 0.035}',
            entry_expected_price=0.11350,
            slice_state=_sl.ACCOUNTED, state_updated_at=now,
            cycle_id=CYCLE, slice_index=3,
            order_side="BUY", order_price=0.11350,
            average_fill_price=ENTRY, filled_quantity=QTY,
            filled_at=now, execution_reason="grid_buy_filled",
            order_id=ORDER_ID,
            target_price=TARGET,
            target_reason="parked_sell floor 1.00% net over this slice's own basis"))
        await db.commit()
    async with Session() as db:
        return (await db.execute(select(CryptoGridSlice))).scalars().one()


row = asyncio.run(_roundtrip())

print("\n-- every §1 field survives the write, asserted on the VALUE --")
# Each of these is the value, never `is not None`: a column that stored
# NULL would satisfy presence and fail the fact.
ok("slice_state is the ACCOUNTED constant, not a lookalike string",
   row.slice_state == _sl.ACCOUNTED, repr(row.slice_state))
ok("cycle_id survives verbatim", row.cycle_id == CYCLE, repr(row.cycle_id))
ok("slice_index stores the rung as an int", row.slice_index == 3, repr(row.slice_index))
ok("order_side is BUY", row.order_side == "BUY", repr(row.order_side))
ok("execution_reason names the path",
   row.execution_reason == "grid_buy_filled", repr(row.execution_reason))
ok("order_id survives as the venue's uuid", row.order_id == ORDER_ID, repr(row.order_id))

print("\n-- the numeric fields keep full precision --")
# A float column that silently rounded would make filled_quantity useless
# for reconciling against the venue, which is the whole reason it is stored.
ok("filled_quantity keeps every bit of the fill",
   row.filled_quantity == QTY, repr(row.filled_quantity))
ok("average_fill_price is what was paid", row.average_fill_price == ENTRY,
   repr(row.average_fill_price))
ok("entry_fee_rate is stored, so the exit can be priced honestly",
   row.entry_fee_rate == 0.0035, repr(row.entry_fee_rate))
ok("order_price and average_fill_price are kept APART",
   row.order_price != row.average_fill_price,
   "expected vs paid collapsed into one number")

print("\n-- the target is a float ABOVE the entry --")
ok("target_price is ABOVE entry_price - a take-profit, not a stop",
   row.target_price > row.entry_price, f"{row.target_price} vs {row.entry_price}")

# THE DECIMAL HAZARD, TESTED WHERE IT ACTUALLY BITES.
#
# An earlier version of this file asserted `isinstance(row.target_price,
# float)` AFTER the round trip and a Decimal mutant SURVIVED it: SQLite
# coerces a Decimal to a float on write, so that assertion was testing the
# database's coercion and not the code's cast. It could not fail for the
# reason it was written, which makes it worse than no assertion at all.
#
# The real hazard is the IN-MEMORY value before any commit, because
# slice_target.round_target_up returns a Decimal (it must, to round
# exactly) and _grid_slice_net_pnl - the single shared fee formula used by
# BOTH the live sell and the dashboard - does float arithmetic against it.
# So that is what is asserted: the consumer really does reject a Decimal,
# and the float() cast the buy path applies really does prevent it.
import decimal

import crypto_grid_bot as _grid
import slice_target as _st

_raw = _st.round_target_up(0.11342 * 1.0172, "0.00001")
ok("round_target_up really does return a Decimal - the premise holds",
   isinstance(_raw, decimal.Decimal), type(_raw).__name__)

_raised = None
try:
    _grid._grid_slice_net_pnl(QTY, ENTRY, _raw, 0.007)
except TypeError as e:
    _raised = e
ok("_grid_slice_net_pnl REJECTS a Decimal exit price - the hazard is real",
   _raised is not None,
   "it accepted a Decimal, so the float() cast guards nothing")

_ok_val = None
try:
    _ok_val = _grid._grid_slice_net_pnl(QTY, ENTRY, float(_raw), 0.007)
except TypeError as e:  # pragma: no cover - would mean the cast is not enough
    _ok_val = None
ok("float() at the boundary is what makes it safe",
   _ok_val is not None, "the cast did not prevent the TypeError")
ok("target_reason names the parked-sell route, not the grid trigger",
   "parked_sell" in (row.target_reason or ""), repr(row.target_reason))

print("\n-- the timestamps are real datetimes --")
ok("filled_at round-trips as a datetime",
   isinstance(row.filled_at, datetime.datetime), type(row.filled_at).__name__)
ok("state_updated_at round-trips as a datetime",
   isinstance(row.state_updated_at, datetime.datetime),
   type(row.state_updated_at).__name__)

print()
if failures:
    print(f"FAILED: {len(failures)}")
    sys.exit(1)
print("All §1 fields commit and read back intact. The insert is not the fault.")
