"""Checks for branch_sizing.py and GET /grid-status/sizing-check.

fastapi and sqlalchemy are not installed everywhere this runs, so the
router is checked as source text and branch_sizing is checked as the pure
module it is. Each check below is a way this report could mislead someone
about to put money into new branches.

Run: python3 test_branch_sizing.py
"""
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import branch_sizing as bs  # noqa: E402

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


def _const(path, name):
    """A module-level literal, read without importing a module too heavy to import."""
    for node in ast.parse(open(os.path.join(HERE, path)).read()).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise KeyError(name)


# --- the mirrored constants still match their sources ----------------------
ok("MIN_TRADE_USD matches crypto_grid_bot",
   bs.MIN_TRADE_USD == _const("crypto_grid_bot.py", "MIN_TRADE_USD"))
ok("DEFAULT_GRID_LEVELS matches crypto_grid_bot",
   bs.DEFAULT_GRID_LEVELS == _const("crypto_grid_bot.py", "DEFAULT_GRID_LEVELS"))
ok("KEEP_BRANCH_ALIVE_USD matches rotation_task",
   bs.KEEP_BRANCH_ALIVE_USD == _const("rotation_task.py", "KEEP_BRANCH_ALIVE_USD"))
_cands = _const("crypto_grid_bot.py", "GRID_LEVEL_SPACING_CANDIDATES")
ok("override level caps match GRID_LEVEL_SPACING_CANDIDATES",
   bs.OVERRIDE_LEVELS == {k: v["num_levels"] for k, v in _cands.items()})

# --- levels ----------------------------------------------------------------
ok("$70 under live_default -> 10 levels", bs.levels_for(70) == 10)
ok("$70 under a 3-level override -> 3 levels", bs.levels_for(70, "3_levels_2.5pct") == 3)
ok("$12 -> 2 levels, never a slice below $5", bs.levels_for(12) == 2)
ok("$3 -> floored at 1 level", bs.levels_for(3) == 1)

# --- the three cash rules, each able to fail alone -------------------------
ok("the 2026-10-04 reading: nothing deployable",
   bs.deployable_usd(0.17, 88.0, 0.0) == 0.0)
p = bs.plan(4, 70, free_cash=500.0, reserve=88.0, ceiling=339.5)
ok("$280 fits $500 free / $88 reserve / $339.50 share", p["fits"] is True)
ok("max at $70 is 4 (share binds, not cash)", p["max_branches_at_this_amount"] == 4)
p = bs.plan(4, 70, free_cash=300.0, reserve=88.0, ceiling=1000.0)
ok("covered by free cash but eats the reserve -> does not fit",
   p["checks"]["free_cash_covers_total"] is True
   and p["checks"]["leaves_grid_reserve"] is False and p["fits"] is False)
p = bs.plan(4, 70, free_cash=1000.0, reserve=88.0, ceiling=200.0)
ok("past the grid's allocator share -> does not fit",
   p["checks"]["within_grid_allocator_share"] is False and p["fits"] is False)
p = bs.plan(1, 70, free_cash=158.0, reserve=88.0, ceiling=70.0)
ok("exactly at every limit fits (cent tolerance)", p["fits"] is True)

# --- UNKNOWN is never zero and never "fits" --------------------------------
p = bs.plan(2, 50, free_cash=None, reserve=88.0, ceiling=None)
ok("unreadable cash -> fits is None", p["fits"] is None)
ok("unreadable cash -> max branches is None", p["max_branches_at_this_amount"] is None)
r = bs.assess({"real_free_cash_usd": 500.0, "branches": []}, None, {}, 88.0)
ok("missing allocator report -> ceiling None, deployable None",
   r["cash"]["grid_ceiling_usd"] is None
   and r["cash"]["deployable_for_new_branches_usd"] is None)
ok("unread wallet -> unbacked_branches None, not []", r["unbacked_branches"] is None)

# --- flat branches and drained coins ---------------------------------------
branches = [
    {"product_id": "QNT-USD", "bot_name": "g1", "allocated_usd": 160.65, "open_slices": 0},
    {"product_id": "TIA-USD", "bot_name": "g2", "allocated_usd": 15.0, "open_slices": 0},
    {"product_id": "JAS-USD", "bot_name": "g3", "allocated_usd": 100.0, "open_slices": 0,
     "locked": True},
    {"product_id": "XRP-USD", "bot_name": "g4", "allocated_usd": 2228.05, "open_slices": 7},
]
flat = bs.flat_branches(branches)
ok("a branch with open slices is never offered as a source",
   all(f["product_id"] != "XRP-USD" for f in flat))
ok("each source keeps $15 so its row is not deleted",
   flat[0]["withdrawable_keeping_branch_usd"] == 145.65
   and flat[1]["withdrawable_keeping_branch_usd"] == 0.0)
trades = [
    {"product_id": "DOGE-USD", "pnl": 3.0, "closed_at": "2026-09-01T00:00:00Z"},
    {"product_id": "DOGE-USD", "pnl": -2.27, "closed_at": "2026-09-09T00:00:00Z",
     "exit_reason": "stop"},
    {"product_id": "XRP-USD", "pnl": 4.0, "closed_at": "2026-10-01T00:00:00Z"},
]
r = bs.assess({"real_free_cash_usd": 0.17, "branches": branches},
              {"bots": [{"bot": "grid", "ceiling_usd": 0.0, "share_pct": 70.0,
                         "reason": "r"}]},
              {"recent_trades": trades, "recent_trades_truncated": True}, 88.0,
              backing={"readable": True, "unbacked": []}, count=8, amount=70)
ok("a locked flat branch is listed but not counted as freeable",
   r["freeable_from_flat_branches_usd"] == 145.65)
ok("a coin with a live branch is not 'drained'",
   [d["product_id"] for d in r["drained_coins"]] == ["DOGE-USD"])
d = r["drained_coins"][0]
ok("drained record: trades, P&L, win rate, worst, last close",
   d["trades"] == 2 and d["realized_pnl_usd"] == 0.73 and d["win_rate_pct"] == 50.0
   and d["worst_trade_usd"] == -2.27 and d["last_close"].startswith("2026-09-09"))
ok("truncated history is said, not hidden", r["drained_history_complete"] is False)
ok("8 x $70 on $0.17 does not fit", r["plan"]["fits"] is False
   and r["plan"]["max_branches_at_this_amount"] == 0)
ok("read wallet with nothing short -> [] not None", r["unbacked_branches"] == [])
ok("detail says DOES NOT FIT", "DOES NOT FIT" in r["detail"])
ok("the report says it is a measurement", r["is_a_measurement_not_a_change"] is True)

# --- the endpoint: read-only, wired, validated -----------------------------
src = open(os.path.join(HERE, "routers", "trading_dashboard.py")).read()
m = re.search(r'@router\.get\("/grid-status/sizing-check"\)\n(async def .*?)(?=\n@router\.)',
              src, re.S)
ok("GET /grid-status/sizing-check exists", m is not None)
body = m.group(1) if m else ""
ok("it is a GET, never a POST",
   '@router.post("/grid-status/sizing-check")' not in src)
ok("it calls branch_sizing.assess", "branch_sizing.assess(" in body)
ok("it passes the real GRID_CASH_RESERVE_USD", "GRID_CASH_RESERVE_USD" in body)
for forbidden in ("create_grid_branch", "withdraw_from_grid_branch", "add_cash_to_grid_branch",
                  "create_multiple_grid_branches", "commit()", "place_", "set_"):
    ok(f"it never calls {forbidden}", forbidden not in body)
ok("count without amount is refused", "count and amount go together" in body)

failed = [label for label, passed in checks if not passed]
for label, passed in checks:
    print(("PASS  " if passed else "FAIL  ") + label)
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)
