"""A slice can now say what it is doing, instead of being asked the wallet.

WHY IT EXISTS. §1's first requirement is that each slice have its own
persistent state, and its first prohibition is "do NOT infer slice state
from wallet balance alone". Until these columns existed a slice's
EXISTENCE was its state, so the only way to answer "what is this slice
doing?" was to look at the wallet - the thing the spec forbids, and the
thing slice_lifecycle.state_from_inventory() refuses to do by raising.

The checks are mostly about two things. Every new column must be NULLABLE,
because main.py's reflection loop adds columns nullable regardless of what
is declared and there is no Alembic - a NOT NULL default would be a
constraint the database never enforces and a claim about rows nobody
observed. And the columns deliberately LEFT OUT must stay out: each is a
second copy of something that is read live, and a stale copy of a venue
rule is worse than no copy.
"""
import ast
import sys
from datetime import datetime

from models import CryptoGridSlice as S

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


COLS = {c.name: c for c in S.__table__.columns}

ADDED = ["cycle_id", "slice_state", "slice_index", "target_price",
         "target_reason", "order_id", "order_side", "order_price",
         "execution_reason", "filled_quantity", "average_fill_price",
         "filled_at", "state_updated_at"]

print("== the state is persisted, not inferred ==")
for name in ADDED:
    ok(f"{name} exists", name in COLS, str(sorted(COLS)))

print("== every one of them is nullable, because NULL means UNKNOWN ==")
# The reflection loop adds columns ALWAYS nullable regardless of
# nullable=False, and there is no Alembic. A NOT NULL here would be a
# constraint the database never enforces.
for name in ADDED:
    if name in COLS:
        ok(f"{name} is nullable", COLS[name].nullable is True,
           str(COLS[name].nullable))

print("== and none of them carries a default that would invent a state ==")
# A default of CREATED on slice_state would claim every pre-existing row
# started here and was observed doing so. It was not.
for name in ADDED:
    if name in COLS:
        c = COLS[name]
        ok(f"{name} has no default", c.default is None and c.server_default is None,
           f"default={c.default!r} server_default={c.server_default!r}")

print("== the join key the venue actually gives us ==")
# Coinbase's fills feed carries order_id and NOT client_order_id - checked
# against a real fill, and recorded in fills_attribution's own docstring.
ok("order_id is indexed, because it is a lookup key",
   "order_id" in COLS and COLS["order_id"].index is True,
   str(COLS.get("order_id").index if "order_id" in COLS else None))
ok("cycle_id is indexed too", "cycle_id" in COLS and COLS["cycle_id"].index is True)

print("== what is deliberately NOT stored stays not stored ==")
# Each of these is read live. A persisted copy is a second source of truth
# that can disagree, and for the venue's own rules a stale copy would size
# an order against a rule the venue no longer has - the exact class of bug
# §3 exists to remove.
for name in ("base_increment", "base_min_size", "quote_increment",
             "quote_min_size", "executable_quantity", "remaining_quantity",
             "realized_pnl"):
    ok(f"{name} is not a slice column", name not in COLS, str(sorted(COLS)))

print("== the fleet's own shape is preserved ==")
# The spec models every position as exactly 1/3, 2/3, 3/3. A live branch
# runs up to its own num_levels rungs (default 10). slice_index records
# WHICH RUNG, and imposes no three-ness - forcing three would change how
# the grid trades, which is not what a persistence change is for.
ok("slice_index is an integer rung, not a fraction of three",
   "slice_index" in COLS and str(COLS["slice_index"].type).upper().startswith("INTEGER"),
   str(COLS.get("slice_index").type if "slice_index" in COLS else None))
import crypto_grid_bot as g
ok("and the default rung count is not three", g.DEFAULT_GRID_LEVELS != 3,
   str(g.DEFAULT_GRID_LEVELS))

print("== the states it can hold are the lifecycle's, not a second list ==")
import slice_lifecycle as sl
_src = open("models.py").read()
_tree = ast.parse(_src)
# No second enumeration of state names in the model file: the 13 states
# live in slice_lifecycle and nowhere else.
_strings = {n.value for n in ast.walk(_tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}
_states = {"CREATED", "ARMED", "READY", "SUBMITTING", "OPEN", "PARTIAL",
           "FILLED", "ACCOUNTED", "DUST", "REPRICE", "CANCELLED",
           "REJECTED", "ERROR"}
ok("models.py does not redeclare the state names",
   len(_states & _strings) == 0, str(sorted(_states & _strings)))
ok("slice_lifecycle still holds all thirteen",
   _states <= set(sl.ALL_STATES) if hasattr(sl, "ALL_STATES")
   else _states <= {getattr(sl, n) for n in dir(sl) if n.isupper()
                    and isinstance(getattr(sl, n), str)},
   "check slice_lifecycle's state names")
ok("and it still refuses to infer state from inventory",
   hasattr(sl, "state_from_inventory"))
try:
    sl.state_from_inventory(0.5)
    ok("  by raising", False, "it returned instead of raising")
except NotImplementedError:
    ok("  by raising NotImplementedError", True)
except Exception as e:
    ok("  by raising NotImplementedError", False, type(e).__name__)

print("== a row can actually be built with them ==")
row = S(bot_name="grid_LINK", product_id="LINK-USD", entry_price=22.80,
        qty=4.1, cycle_id="c-8841", slice_state="OPEN", slice_index=2,
        target_price=23.016, order_id="abc-123", order_side="SELL",
        order_price=23.016, filled_quantity=0.0,
        state_updated_at=datetime.utcnow())
ok("the state round-trips on the object", row.slice_state == "OPEN")
ok("the rung round-trips", row.slice_index == 2)
ok("the order id round-trips", row.order_id == "abc-123")
row2 = S(bot_name="grid_LINK", product_id="LINK-USD", entry_price=22.80, qty=4.1)
ok("a row built the old way still works", row2.qty == 4.1)
ok("  and its state is UNKNOWN, not a default", row2.slice_state is None,
   repr(row2.slice_state))

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all slice-state column checks passed")
