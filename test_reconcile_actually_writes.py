"""The reconcile apply path: it must never report a write it did not make.

THE BUG THIS LOCKS DOWN. /grid-status served slices with no "id", so
slice_reconcile built every action with slice_id=None, every lookup in the
apply path matched nothing, and no row was ever deleted or reduced. And
because applied.append sat OUTSIDE the inner loop, the endpoint still
answered "8 branch(es), $1,157.41 of cost basis cleared". The account
owner ran it three times on that answer while the backing report never
moved a cent.

Two independent faults, so two independent guards:
  1. the id is served, so the plan is executable at all;
  2. a branch counts as applied ONLY when a row really changed.
"""
import sys, json, asyncio
sys.path.insert(0, "/home/user/empire-v2")
import slice_reconcile

fail = 0
def ok(label, cond, detail=""):
    global fail
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + ("" if cond or not detail else f"   -> {detail}"))
    if not cond: fail += 1

# ---------------------------------------------------------------- 1
print("\n[1] /grid-status serves the slice primary key")
src = open("/home/user/empire-v2/crypto_grid_bot.py").read()
i = src.find("slices_out.append({")
block = src[i:i + 900]
ok("the serialiser emits an id field", '"id": getattr(s, "id", None)' in block,
   block[:160])
ok("it uses getattr, so a row without one reads None not an exception",
   'getattr(s, "id", None)' in block)

# ---------------------------------------------------------------- 2
print("\n[2] with an id present, the plan is executable")
sl = lambda i, q, p: {"id": i, "qty": q, "entry_price": p, "opened_at": None}
actions, report = slice_reconcile.plan(
    [sl(11, 1.0, 10.0), sl(12, 2.0, 10.0), sl(13, 3.0, 10.0)], 1.0, price=10.0)
ok("the plan is READY", report.get("status") == "READY", report)
ok("every action carries a real slice_id",
   actions and all(a.get("slice_id") is not None for a in actions),
   json.dumps(actions)[:200])

print("\n[3] the exact live shape that failed - no id - plans but cannot execute")
noid = [{"qty": 0.01, "entry_price": 14.343, "opened_at": None},
        {"qty": 2.99, "entry_price": 15.212, "opened_at": None}]
a2, r2 = slice_reconcile.plan(noid, 0.22, price=14.0)
ok("it still reports READY (which is how this hid)", r2.get("status") == "READY", r2)
ok("but every slice_id is None", all(a.get("slice_id") is None for a in a2),
   json.dumps(a2)[:160])

# ---------------------------------------------------------------- 4
# Re-create the endpoint's apply loop exactly as it now stands, against a
# fake session, and assert on what it REPORTS versus what it WROTE.
print("\n[4] the apply loop reports only what it wrote")

class FakeRow:
    def __init__(self, i): self.id, self.qty, self.deleted = i, 99.0, False
class FakeDB:
    def __init__(self, rows): self.rows = {r.id: r for r in rows}; self.deleted = []
    async def execute(self, _q): return self
    def scalars(self): return self
    def first(self): return self._hit
    async def delete(self, row): row.deleted = True; self.deleted.append(row.id)
    async def commit(self): self.committed = True

async def run_apply(branches, existing_ids):
    """The endpoint's loop, verbatim in structure."""
    db = FakeDB([FakeRow(i) for i in existing_ids])
    applied, not_applied = [], []
    for br in branches:
        changed, unfound = 0, 0
        for a in br["actions"]:
            sid = a.get("slice_id")
            if sid is None:
                unfound += 1; continue
            db._hit = db.rows.get(sid)
            row = db._hit
            if row is None:
                unfound += 1; continue
            if a["action"] == "REMOVE":
                await db.delete(row)
            else:
                row.qty = a["qty_after"]
            changed += 1
        if changed:
            applied.append({"product_id": br["product_id"],
                            "cost_basis_removed_usd": br["cost_basis_removed_usd"],
                            "slice_rows_changed": changed})
        else:
            not_applied.append({"product_id": br["product_id"],
                                "slice_rows_not_found": unfound})
    await db.commit()
    return applied, not_applied, db

# (a) the live failure: ids are all None
br_none = [{"product_id": "LINK-USD", "cost_basis_removed_usd": 90.0, "units_removed": 6.0,
            "actions": [{"slice_id": None, "action": "REMOVE", "qty_after": 0.0},
                        {"slice_id": None, "action": "REMOVE", "qty_after": 0.0}]}]
applied, not_applied, db = asyncio.run(run_apply(br_none, existing_ids=[11, 12]))
ok("a branch whose ids are all None is NOT reported as applied", applied == [], applied)
ok("it is reported as not_applied instead", len(not_applied) == 1, not_applied)
ok("and nothing was deleted", db.deleted == [], db.deleted)

# (b) rows genuinely missing from the table
br_missing = [{"product_id": "TIA-USD", "cost_basis_removed_usd": 10.0, "units_removed": 1.0,
               "actions": [{"slice_id": 999, "action": "REMOVE", "qty_after": 0.0}]}]
applied, not_applied, db = asyncio.run(run_apply(br_missing, existing_ids=[11]))
ok("a branch whose rows are absent is NOT reported as applied", applied == [], applied)
ok("nothing was deleted", db.deleted == [], db.deleted)

# (c) the real thing
br_ok = [{"product_id": "BCH-USD", "cost_basis_removed_usd": 60.0, "units_removed": 0.3,
          "actions": [{"slice_id": 11, "action": "REMOVE", "qty_after": 0.0},
                      {"slice_id": 12, "action": "REDUCE", "qty_after": 0.5}]}]
applied, not_applied, db = asyncio.run(run_apply(br_ok, existing_ids=[11, 12]))
ok("a branch that really wrote IS reported as applied", len(applied) == 1, applied)
ok("it counts the rows it touched", applied[0]["slice_rows_changed"] == 2, applied)
ok("the REMOVE actually deleted", db.deleted == [11], db.deleted)
ok("the REDUCE actually set the new qty", db.rows[12].qty == 0.5, db.rows[12].qty)
ok("nothing is in not_applied", not_applied == [], not_applied)

# (d) partial - some rows found, some not. Still applied, but honest.
br_part = [{"product_id": "ACH-USD", "cost_basis_removed_usd": 8.0, "units_removed": 0.1,
            "actions": [{"slice_id": 11, "action": "REMOVE", "qty_after": 0.0},
                        {"slice_id": 777, "action": "REMOVE", "qty_after": 0.0}]}]
applied, not_applied, db = asyncio.run(run_apply(br_part, existing_ids=[11]))
ok("a partial write is reported as applied", len(applied) == 1, applied)
ok("and counts only the row it changed", applied[0]["slice_rows_changed"] == 1, applied)

# ---------------------------------------------------------------- 5
print("\n[5] the endpoint's headline number describes the WRITE, not the plan")
ep = open("/home/user/empire-v2/routers/trading_dashboard.py").read()
i = ep.find('@router.post("/grid-status/reconcile-slices")')
body = ep[i:i + 9000]
ok("applied.append is inside the per-branch guard",
   "if changed:\n                applied.append(" in body)
ok("a branch with no write goes to not_applied", "not_applied.append(" in body)
ok("cost_basis_removed_usd is recomputed from applied only",
   'sum(a["cost_basis_removed_usd"] or 0.0 for a in applied)' in body)
ok("the plan's figure is kept under its own name",
   'out["cost_basis_planned_usd"] = total_basis' in body)
ok("a zero-write apply says NOTHING WAS CHANGED", "NOTHING WAS CHANGED" in body)
ok("a None slice_id is treated as unwritable, not as an absent row",
   "if sid is None:" in body)

print()
if fail:
    print(f"{fail} FAILED"); sys.exit(1)
print("all checks passed")
