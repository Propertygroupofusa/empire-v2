"""Is holding_cost reachable from the lessons endpoint, with the right inputs?

test_holding_cost.py proves the module is right about its inputs. This
proves the endpoint hands it the right ones. Three silent failures it is
built to catch: lessons passed WITHOUT the live branches (so the ZEC case
stays invisible, which is the whole bug), the backing report left out (so
ten phantom branches get counted as real holding cost), and an exception
path that reports zero cost instead of UNKNOWN.
"""
import ast

FAILED = []


def ok(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        FAILED.append(label)


SRC = open("routers/trading_dashboard.py").read()
tree = ast.parse(SRC)

calls = [n for n in ast.walk(tree)
         if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
         and n.func.attr == "assess" and isinstance(n.func.value, ast.Name)
         and n.func.value.id == "holding_cost"]
ok("holding_cost.assess is called exactly once", len(calls) == 1)

if calls:
    c = calls[0]
    pos = [ast.unparse(a) for a in c.args]
    kw = {k.arg: ast.unparse(k.value) for k in c.keywords}
    ok("it is given two positional arguments", len(pos) == 2)
    ok("arg 1 is the LIVE branches - without them ZEC, which has no "
       "lesson, can never appear at all",
       "branches" in pos[0] and "_status" in pos[0])
    ok("arg 2 is the lessons payload", "lessons" in pos[1])
    ok("the backing report is passed, so a phantom mark-to-market is not "
       "counted as a cost of holding", "backing" in kw and "backing" in kw["backing"])
    ok("branches come from a LIVE grid status read, not from the lessons table",
       "get_grid_status" in SRC[SRC.index("import holding_cost"):][:1200])

# ---- the failure path reports UNKNOWN, never zero ------------------
blk = SRC[SRC.index("import holding_cost"):]
blk = blk[:blk.index("return JSONResponse(content=payload")]
ok("the exception path sets the key", 'payload["holding_cost"]' in blk)
ok("it is set on BOTH paths (success and failure)",
   blk.count('payload["holding_cost"]') == 2)
ok("the failure path says UNKNOWN, not zero cost",
   "this_is_unknown_not_zero_cost" in blk)
ok("the failure path reports readable False", '"readable": False' in blk)
ok("the failure path quotes no dollar figure",
   not any(t in blk.split('except Exception')[1] for t in ("net_usd", "0.0", "$")))
ok("a missing grid module does not raise - it degrades to empty",
   "if crypto_grid_bot_module is not None else {}" in blk)

# ---- end to end on the real live payload shape --------------------
import json
import holding_cost
S = "/tmp/claude-0/-home-user-Delfina/8bc5b730-e02d-506c-9c98-0f6519adc72d/scratchpad/"
g = json.load(open(S + "g6.json"))
les = json.load(open(S + "les.json"))

r = holding_cost.assess(g["branches"], les["lessons"], backing=g.get("backing"))
print("\n-- end to end on the live 2026-10-02 reading")
ok("every branch holding coin is accounted for",
   r["branches_holding_coin"] == sum(1 for b in g["branches"] if (b.get("slices") or [])))
ok("ZEC is named the worst coin to hold", r["worst"] == "ZEC-USD")
ok("ZEC is flagged as never having sold", r["never_sold"] == ["ZEC-USD"])
zec = next(x for x in r["ranked"] if x["product_id"] == "ZEC-USD")
ok("ZEC shows 0 closed trades", zec["closed_trades"] == 0)
ok("ZEC's capital tied is over $2,000", zec["capital_tied_usd"] > 2000)
ok("ZEC's net is worse than -$300", zec["net_usd"] < -300)
ok("HBAR reads EARNING_BUT_BEHIND - the old memory called it 'earning'",
   next(x for x in r["ranked"] if x["product_id"] == "HBAR-USD")["verdict"]
   == "EARNING_BUT_BEHIND")
hb_old = next(x for x in les["lessons"] if x["product_id"] == "HBAR-USD")
ok("...and the old memory really did say earning, with a positive P&L",
   hb_old["verdict"] == "earning" and hb_old["total_pnl"] > 0)
ok("the 10 unbacked branches are PHANTOM, not counted as holding cost",
   len(r["phantom"]) == len(g["backing"]["unbacked"]))
ok("the fleet mark-to-market EXCLUDES every phantom figure",
   abs(r["mark_to_market_usd"]
       - sum(x["mark_to_market_usd"] for x in r["ranked"]
             if x["verdict"] != "PHANTOM")) < 0.01)
ok("the net is negative - which is the point; the old memory could not say it",
   r["net_usd"] < 0)
ok("no verdict in the new report is the old vocabulary",
   not ({x["verdict"] for x in r["ranked"]} & {"earning", "watch", "avoid"}))
ok("nothing reads NaN or None where a number belongs",
   all(isinstance(x["capital_tied_usd"], float) for x in r["ranked"]))

print()
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for f in FAILED:
        print("  -", f)
    raise SystemExit(1)
print("all checks passed")
