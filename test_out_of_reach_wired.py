"""Is out_of_reach actually reachable from the grid-status payload?

test_out_of_reach.py proves the module is right about its inputs. This
proves the endpoint hands it the right ones. The two failures it is built
to catch are both silent: handing it `available_units` (where a staked
coin looks identical to a sold one) instead of `held_including_zero`, and
a second trip to the accounts endpoint that gets rate-limited and leaves
the block reporting UNKNOWN with nothing in it.
"""
import ast
import re

FAILED = []


def ok(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        FAILED.append(label)


SRC = open("routers/trading_dashboard.py").read()

# ---- the call site exists and is shaped right ------------------------
tree = ast.parse(SRC)
calls = [n for n in ast.walk(tree)
         if isinstance(n, ast.Call)
         and isinstance(n.func, ast.Attribute)
         and n.func.attr == "assess"
         and isinstance(n.func.value, ast.Name)
         and n.func.value.id == "out_of_reach"]
ok("out_of_reach.assess is called exactly once", len(calls) == 1)

if calls:
    c = calls[0]
    ok("it is passed three positional arguments", len(c.args) == 3)
    args = [ast.unparse(a) for a in c.args]
    ok("arg 1 is the branch list", "branches" in args[0])
    ok("arg 2 is held_including_zero, NOT available_units "
       "(a staked coin and a sold one look the same in available_units)",
       "held_including_zero" in args[1] and "available_units" not in args[1])
    ok("arg 3 is available_units", "available_units" in args[2])
    ok("it reads the SAME _bal the backing block already fetched - "
       "no second trip to the accounts endpoint",
       args[1].startswith("_bal.") and args[2].startswith("_bal."))

# ---- it is imported, and in the same block as the balance read -------
ok("the module is imported", re.search(r"^\s+import out_of_reach$",
                                       SRC, re.M) is not None)
block = SRC[SRC.index("import slice_backing"):]
block = block[:block.index("# CAN EACH BRANCH WORK ITS POSITION")]
ok("the import and the call are inside the one balance-reading block",
   "import out_of_reach" in block and "out_of_reach.assess" in block)
ok("fetch_balances is still called only once in that block",
   block.count("fetch_balances") == 1)

# ---- every exit from that block sets the key -------------------------
ok('the payload key is "out_of_reach"', 'data["out_of_reach"]' in block)
ok("it is set on the unreadable-balances path too",
   block.count('data["out_of_reach"]') == 3)
ok("and on the exception path", 'data["out_of_reach"] = {"readable": False,\n'
   '                                "reason": f"{type(_exc).__name__}' in block)
for frag in ("UNKNOWN, not zero",):
    ok(f"the failure paths say {frag!r}", block.count(frag) >= 2)
ok("no failure path reports a dollar figure",
   'data["out_of_reach"] = {"readable": False' in block
   and block.count('"out_of_reach_usd": None') == 2)

# ---- end to end on the live reading ---------------------------------
import out_of_reach

# Shaped exactly as account_census.fetch_balances returns it, with the
# live 2026-10-02 figures: ETH dust, ATOM/ADA confirmed zero, SOL staked.
BAL = {"available": True,
       "held_including_zero": {"ETH": 2.803169192e-09, "SOL": 1.03460007,
                               "XRP": 1396.790632, "ATOM": 0.0, "ADA": 0.0,
                               "TIA": 0.0, "PRIME": 0.0, "QNT": 0.00097323},
       "available_units": {"SOL": 0.25865002, "XRP": 1396.790632,
                           "QNT": 0.00097323}}
BRANCHES = [{"product_id": p} for p in
            ("ETH-USD", "SOL-USD", "XRP-USD", "TIA-USD", "PRIME-USD", "QNT-USD")]

r = out_of_reach.assess(BRANCHES,
                        BAL.get("held_including_zero") or {},
                        BAL.get("available_units") or {},
                        env={})
print("\n-- end to end on the live reading")
ok("readable", r["readable"] is True)
flagged = {e["asset"] for e in r["empty_trading_balance"]}
ok("ETH is flagged - $2,294.82 in the app, 2.8e-09 to the API",
   "ETH" in flagged)
ok("TIA and PRIME (confirmed zeros) are flagged",
   {"TIA", "PRIME"} <= flagged)
# QNT holds 0.00097323 units - a thousand times DUST_UNITS. It is a real,
# VISIBLE balance that the venue reports and this key can read; it is
# unsellable only because it is under QNT's 0.001 order minimum, which is
# slice_backing's finding (it reports the branch 0.144% backed). Flagging
# it here would be a false positive AND a duplicate, and would blur the
# one distinction this module exists to draw: coin the API cannot see at
# all, versus coin it can see and cannot sell.
ok("QNT at 0.00097 is NOT flagged - it is visible, just tiny "
   "(unsellable-but-visible is slice_backing's finding)",
   "QNT" not in flagged)
ok("the dust threshold sits between ETH's 2.8e-09 and QNT's 0.00097",
   2.803169192e-09 < out_of_reach.DUST_UNITS < 0.00097323)
ok("SOL is NOT flagged - it HAS a trading balance, just a partial one "
   "(that shortfall is slice_backing's finding, not this one)",
   "SOL" not in flagged)
ok("XRP is not flagged", "XRP" not in flagged)
ok("the total is still UNKNOWN, because the key cannot see staked coin",
   r["verdict"] == "UNKNOWN" and r["out_of_reach_usd"] is None)
ok("nothing in the payload claims the out-of-reach total is zero",
   "0.0" != str(r["out_of_reach_usd"]))

# The declared figure, as the owner would set it from the app.
r2 = out_of_reach.assess(BRANCHES,
                         BAL.get("held_including_zero") or {},
                         BAL.get("available_units") or {},
                         readable_total_usd=10195.63,
                         env={"GRID_OUT_OF_REACH_USD": "4093.14"})
ok("declared: the account is $14,288.77, not $10,195.63",
   r2["true_total_usd"] == 14288.77)
ok("declared: 71.35% of the crypto is reachable",
   abs(r2["reachable_share_pct"] - 71.35) < 0.05)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for f in FAILED:
        print("  -", f)
    raise SystemExit(1)
print("all checks passed")
