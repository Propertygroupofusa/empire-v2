"""A measurement that cannot support its own claim must say so.

WHY THIS FILE EXISTS, and it is as much about the first draft as the second.

The finding behind this module is real and was measured across all 165 closed
trades: the fleet's top earners held no adopted slices, and ZEC and XRP held
52.9% of the capital while producing 0.3% of the profit.

The FIRST version of ladder_health.py turned that into a verdict that claimed
to predict earnings. Checked against realised return it separated LADDER from
LUMP by 1.08 vs 0.84 per $100 - almost nothing - and it scored ZEC a LADDER
while ZEC had earned $0.00, which is the one branch the whole exercise is
about. Tuning the spread threshold to 5.0% produced a beautiful 5.42
separation on FOUR branches, which is not a rule, it is fitting noise; this
repo already has the line about three samples not being a result.

So the module describes shape and MEASURES ITS OWN SEPARATION instead of
asserting one, and refuses to call it a finding under MIN_BRANCHES_FOR_A_
FINDING. On the live fleet today that is 21 branches, separation 0.489,
is_a_finding False - which is the honest answer and the one worth shipping.

Run: python3 test_ladder_health.py
"""
import sys

import ladder_health as lh

checks = []


def ok(label, cond, detail=""):
    checks.append((label, bool(cond), detail))


def section(t):
    checks.append((t, None, ""))


def br(pid, entries, adopted=0, alloc=100.0, bot="b"):
    sl = [{"entry_price": e, "qty": 1.0,
           "adopted": i < adopted} for i, e in enumerate(entries)]
    return {"product_id": pid, "bot_name": bot, "allocated_usd": alloc,
            "slices": sl}


section("[1] unreadable is UNKNOWN, never a good score")
r = lh.assess_branch({"product_id": "X-USD", "allocated_usd": 500.0,
                      "slices": [{"qty": 1.0}, {"qty": 2.0}]})
ok("no readable entry price -> UNKNOWN", r["verdict"] == lh.UNKNOWN, r["verdict"])
ok("it is NOT scored a LADDER", r["verdict"] != lh.LADDER)
ok("and it says unknown is not a clean bill of health",
   "not a clean bill of health" in str(r["why"]))
for bad in (None, {}, {"slices": None}):
    v = lh.assess_branch(bad)["verdict"]
    ok(f"assess_branch({str(bad)[:18]}) does not claim LADDER", v != lh.LADDER)

section("[2] a branch holding nothing is THIN, not broken")
r = lh.assess_branch(br("J-USD", []))
ok("THIN", r["verdict"] == lh.THIN)
ok("explicitly not a fault", "Not a fault" in str(r["why"]))

section("[3] shape is described, and the label says it is a description")
r = lh.assess_branch(br("XRP-USD", [1.5383, 1.5383, 1.5383, 1.5282, 1.5252]))
ok("tightly clustered slices read LUMP", r["verdict"] == lh.LUMP, r["verdict"])
ok("the spread is reported", r["spread_pct"] is not None)
ok("it calls itself a description, not a forecast",
   "DESCRIPTION, not a forecast" in str(r["why"]), str(r["why"]))
r = lh.assess_branch(br("LINK-USD", [14.343, 15.212, 14.651]))
ok("well-spread slices read LADDER", r["verdict"] == lh.LADDER, r["verdict"])
ok("that label is also marked a description",
   "DESCRIPTION, not a forecast" in str(r["why"]))
r = lh.assess_branch(br("ETH-USD", [2710.799, 2710.799, 2710.799]))
ok("three slices at ONE price is a LUMP", r["verdict"] == lh.LUMP)
ok("and it counts one distinct entry", r["distinct_entries"] == 1)

section("[4] adopted vs bought is recorded, not editorialised")
r = lh.assess_branch(br("ZEC-USD", [1659.17, 1650.61, 1586.44], adopted=2))
ok("adopted counted", r["adopted_slices"] == 2)
ok("bought counted", r["bought_slices"] == 1)
# bought_share is rounded to 4dp by the module, so the tolerance has to
# admit that. 1e-6 was tighter than the value it was checking - a test
# failing on its own precision, not on the code.
ok("share computed", abs(r["bought_share"] - 1/3) < 1e-4, str(r["bought_share"]))

section("[5] THE POINT: it refuses to call a thin sample a finding")
rows = [lh.assess_branch(br(f"A{i}-USD", [10.0, 11.0], alloc=100.0))
        for i in range(3)]
rows += [lh.assess_branch(br(f"B{i}-USD", [10.0, 10.0], alloc=100.0))
         for i in range(3)]
realized = {f"A{i}-USD": 9.0 for i in range(3)}
realized.update({f"B{i}-USD": 1.0 for i in range(3)})
s = lh.separation(rows, realized)
ok("it measured something", s["measured"] is True)
ok("a big gap is still NOT a finding at n=6", s["is_a_finding"] is False,
   f"n={s['branches']} sep={s['separation']}")
ok("it names the floor it did not reach",
   str(lh.MIN_BRANCHES_FOR_A_FINDING) in s["detail"], s["detail"])
ok("and says not to act on it as a rule", "not acted on as a rule" in s["detail"])

section("[6] one-sided data is not a comparison")
s = lh.separation([lh.assess_branch(br("A-USD", [10.0, 11.0]))], {"A-USD": 5.0})
ok("no lumps to compare against -> not measured", s["measured"] is False)
ok("it says why", "nothing to compare" in s["detail"])

section("[7] the fleet roll-up adds up and reports lump capital")
fleet = [br("BIG-USD", [100.0, 100.0], alloc=2000.0),      # LUMP
         br("OK-USD", [10.0, 11.0], alloc=500.0),          # LADDER
         br("EMPTY-USD", [], alloc=50.0)]                  # THIN
a = lh.assess(fleet)
ok("lump capital is the $2,000", a["capital_in_lumps_usd"] == 2000.0,
   str(a["capital_in_lumps_usd"]))
ok("ladder capital is the $500", a["capital_in_ladders_usd"] == 500.0)
ok("the empty branch is not counted as either",
   a["capital_unmeasured_usd"] == 50.0, str(a["capital_unmeasured_usd"]))
ok("lump share is of MEASURED capital only",
   abs(a["lump_share_pct"] - 80.0) < 0.01, str(a["lump_share_pct"]))
ok("rows are ordered biggest first",
   a["rows"][0]["product_id"] == "BIG-USD")
ok("with no realised P&L it does not pretend to have checked",
   a["separation"]["measured"] is False)
ok("the note says adoption is BY DESIGN, not a bug",
   "BY DESIGN" in a["note"])
ok("and that it forecasts nothing", "does not forecast" in a["note"])

section("[8] it changes nothing - no I/O, no writes, no orders")
import ast, os                                  # noqa: E402
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "ladder_health.py"), encoding="utf-8").read()
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        node.body = [n for n in node.body
                     if not (isinstance(n, ast.Expr)
                             and isinstance(n.value, ast.Constant)
                             and isinstance(n.value.value, str))]
body = ast.unparse(tree)
for bad in ("aiohttp", "requests", "session", "commit(", "db.", "place_",
            "os.getenv", "open("):
    ok(f"no {bad}", bad not in body)

print()
failed = 0
for label, res, detail in checks:
    if res is None:
        print(f"\n{label}")
    else:
        print(f"  {'PASS' if res else 'FAIL'}  {label}" + (f"   -> {detail}" if detail and not res else ""))
        failed += 0 if res else 1
total = sum(1 for _, r, _ in checks if r is not None)
print(f"\n{total - failed}/{total} checks passed")
print("ALL PASS" if not failed else f"{failed} FAILED")
sys.exit(1 if failed else 0)
