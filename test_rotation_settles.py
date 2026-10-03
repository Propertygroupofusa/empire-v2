"""A rotation that sells without settling is worse than no rotation at all.

TWO ATTEMPTS AT THIS FILE WERE THROWN AWAY, both for the same defect: on a
successful fill they did `placed += 1` and wrote a log line. Neither retired
the slice it had just sold, adjusted allocated_usd, invalidated the balance
cache, or wrote a trade-history row. Every "successful" rotation would have
widened the gap between what the books claim and what the wallet holds -
8 branches and $1,168.59 of it when this was written.

So the single most important thing to test is NOT that it sells. It is that
it does not own a sell path at all, and that the capital it frees is settled
by `close_all_grid_slices`, which is the grid's book of record.

These tests RUN check_once() against stubs. The last two attempts would both
have passed a source-grep; only executing them shows what reaches the venue
and what gets written back.

Run: python3 test_rotation_settles.py
"""
import asyncio
import ast
import os
import sys
import types

os.environ["CONCENTRATION_ROTATION_MODE"] = "arm"

import concentration_rotation as cr          # noqa: E402
import concentration_rotation_worker as w    # noqa: E402

checks = []


def ok(label, cond, detail=""):
    checks.append((label, bool(cond), detail))


def section(t):
    checks.append((t, None, ""))


class Row:
    def __init__(self, id, entry_price, qty, entry_fee_rate=0.0035):
        self.id, self.entry_price, self.qty = id, entry_price, qty
        self.entry_fee_rate = entry_fee_rate


def stub_grid(branches, rows, closed_calls, price_ok=True):
    g = types.ModuleType("crypto_grid_bot")
    async def get_grid_status(): return {"branches": branches}
    async def get_grid_slices(bot_name): return rows
    async def close_all_grid_slices(**kw):
        closed_calls.append(kw)
        return {"slices_closed": len(kw.get("only_slice_ids") or []),
                "total_realized_pnl": 4.21, "results": [{"sold": True}]}
    g.get_grid_status = get_grid_status
    g.get_grid_slices = get_grid_slices
    g.close_all_grid_slices = close_all_grid_slices
    sys.modules["crypto_grid_bot"] = g
    return g


# A slice bought at 100 and now worth 110 clears 1.6407% cost easily.
BRANCHES = [{"product_id": "ZEC-USD", "bot_name": "crypto_grid_2",
             "current_price": 110.0, "allocated_usd": 3000.0,
             "slices": [{"entry_price": 100.0, "qty": 1.0}]},
            {"product_id": "XLM-USD", "bot_name": "crypto_grid_3",
             "current_price": 1.0, "allocated_usd": 100.0, "slices": []}]
ROWS = [Row(11, 100.0, 1.0), Row(12, 100.0, 1.0), Row(13, 109.9, 1.0),
        Row(14, 100.0, 1.0), Row(15, 100.0, 1.0)]

loop = asyncio.get_event_loop()
_orig_over = cr.over_limit
cr.over_limit = lambda branches, **kw: [{"product_id": "ZEC-USD"}]

# ── 1. it settles through the grid's own path ────────────────────────
section("[1] the capital is settled by close_all_grid_slices, not by this file")
calls = []
stub_grid(BRANCHES, ROWS, calls)
r = loop.run_until_complete(w.check_once())
ok("it called close_all_grid_slices", len(calls) == 1, f"{len(calls)} calls")
if calls:
    c = calls[0]
    ok("scoped to the one branch", c.get("only_bot_name") == "crypto_grid_2")
    ok("it named SPECIFIC slice ids", bool(c.get("only_slice_ids")), str(c))
    ok("it did NOT close the whole branch",
       len(c["only_slice_ids"]) < len(ROWS),
       f"{len(c['only_slice_ids'])} of {len(ROWS)}")
    ok("capped at MAX_SELLS_PER_PASS",
       len(c["only_slice_ids"]) <= w.MAX_SELLS_PER_PASS)
    ok("the ledger can tell it from a close-all",
       c.get("exit_reason") == "concentration_rotation", str(c.get("exit_reason")))
    ok("it REFUSES an unverified balance - it is not a protection",
       c.get("allow_unverified_balance") is False,
       "close-all may sell unverified; an opportunistic rotation may not")
    ok("the unprofitable slice (entry 109.9) was not chosen",
       13 not in c["only_slice_ids"], str(c["only_slice_ids"]))
ok("it reports what settled", r.get("settled") == 3, str(r))

# ── 2. the defect that killed both previous attempts ─────────────────
section("[2] it owns NO sell path and NO bookkeeping of its own")
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "concentration_rotation_worker.py"), encoding="utf-8").read()
tree = ast.parse(src)
# CODE ONLY - the docstrings describe the old bug in detail on purpose.
for node in ast.walk(tree):
    if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        node.body = [n for n in node.body
                     if not (isinstance(n, ast.Expr)
                             and isinstance(n.value, ast.Constant)
                             and isinstance(n.value.value, str))]
body = ast.unparse(tree)
for bad in ("place_maker_sell", "place_market_sell", "place_maker_buy",
            "order_configuration", "client_order_id", "aiohttp",
            "session.add", "db.delete", "allocated_usd =", "commit("):
    ok(f"no {bad}", bad not in body)
ok("it does not buy", "buy" not in body.lower().replace("buys", ""))
ok("the ONLY settle call is close_all_grid_slices",
   body.count("close_all_grid_slices") == 1)

# ── 3. off unless armed, and disarming stops it ──────────────────────
section("[3] off unless armed")
os.environ["CONCENTRATION_ROTATION_MODE"] = "observe"
calls = []
stub_grid(BRANCHES, ROWS, calls)
r = loop.run_until_complete(w.check_once())
ok("nothing settled while observing", r.get("settled") == 0 and not calls, str(r))
ok("and it says so", "observing" in str(r.get("detail")))
# Only the exact word arms it. "ARM " with a trailing space SHOULD arm -
# the reader typed the right word - so it is asserted as arming, not as a
# vacuous pass. A test whose label says one thing while the assertion checks
# another is worse than no test.
for v in ("", "true", "1", "armed", "yes", "disarm"):
    os.environ["CONCENTRATION_ROTATION_MODE"] = v
    ok(f"{v!r} does NOT arm it", not w.is_armed())
for v in ("arm", "ARM", "ARM ", " Arm", '"arm"'):
    os.environ["CONCENTRATION_ROTATION_MODE"] = v
    ok(f"{v!r} DOES arm it", w.is_armed())
os.environ["CONCENTRATION_ROTATION_MODE"] = "arm"

# ── 4. nothing profitable, nothing sold ──────────────────────────────
section("[4] a loser is never settled")
calls = []
stub_grid(BRANCHES, [Row(21, 200.0, 1.0), Row(22, 111.0, 1.0)], calls)
r = loop.run_until_complete(w.check_once())
ok("no settle call at all", not calls, str(calls))
ok("it says why", any(s.get("reason") == "NO_SLICE_CLEARS_THE_COST"
                      for s in (r.get("skipped") or [])), str(r.get("skipped")))

section("[5] an unreadable price is skipped, not guessed")
calls = []
stub_grid([{"product_id": "ZEC-USD", "bot_name": "crypto_grid_2",
            "current_price": None, "slices": []}], ROWS, calls)
r = loop.run_until_complete(w.check_once())
ok("nothing settled", not calls)
ok("reported as PRICE_UNREADABLE",
   any(s.get("reason") == "PRICE_UNREADABLE" for s in (r.get("skipped") or [])),
   str(r.get("skipped")))

section("[6] no branch over the limit means no work")
cr.over_limit = lambda branches, **kw: []
calls = []
stub_grid(BRANCHES, ROWS, calls)
r = loop.run_until_complete(w.check_once())
ok("nothing settled", not calls and r.get("settled") == 0)
ok("and it says why", "over the concentration limit" in str(r.get("detail")))
cr.over_limit = _orig_over

section("[7] the margin bar is the real one, after the TAKER exit")
ok("exit leg priced at taker", cr.EXIT_FEE_PCT_DEFAULT == 0.75)
ok("adverse selection included", cr.ADVERSE_SELECTION_PCT > 0)
ok("a 1.00% gain does NOT clear a 1.6407% cost",
   (cr.exit_net_pct(100.0, 101.0, cr.DEFAULT_ROUND_TRIP_COST_PCT) or 0) <= cr.MIN_NET_MARGIN_PCT)
ok("a 10% gain does", (cr.exit_net_pct(100.0, 110.0, cr.DEFAULT_ROUND_TRIP_COST_PCT) or 0) > cr.MIN_NET_MARGIN_PCT)

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
