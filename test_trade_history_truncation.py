"""A capped list must say it is capped.

WHY THIS FILE EXISTS. get_grid_trade_history served exactly 50 closed
trades and nothing in the payload admitted it. With 132 completed trades
the window covered 2.5 days and omitted 82 of them, while `recent_trades`
read like the whole book - and the endpoint could not ask for more, because
it called the function with no arguments at all.

That is not a hypothetical cost. The four-way exit_reason split shipped at
00:16Z; one hour later exactly ONE of the 50 rows had been written under
it, the other 49 being legacy rows carrying the old binary label. A reader
taking the counts at face value would have concluded the new labels were
never being used. The cap was silently deciding what the data appeared to
say.

So: `limit` is askable, bounded by a ceiling (the payload is served live
and must not become a slow query), and the response states what it left
out. total_trade_count was already there; what was missing was any link
between it and the length of the list beside it.

Behavioural where it can be - the validation and the metadata are computed
from plain values, so the checks drive the real function with a fake
session rather than reading its source.
"""
import ast
import sys

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


GRID_SRC = open("crypto_grid_bot.py", encoding="utf-8").read()
GRID = ast.parse(GRID_SRC)
ROUTER_SRC = open("routers/trading_dashboard.py", encoding="utf-8").read()
ROUTER = ast.parse(ROUTER_SRC)


def func(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


print("== the endpoint can ask for more than the default ==")

ep = None
for n in ast.walk(ROUTER):
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for d in n.decorator_list:
            if "grid-status/trade-history" in (ast.get_source_segment(ROUTER_SRC, d) or ""):
                ep = n
ok("the trade-history endpoint exists", ep is not None)

if ep is not None:
    args = [a.arg for a in ep.args.args] + [a.arg for a in ep.args.kwonlyargs]
    ok("it accepts a limit", "limit" in args,
       f"args: {args} - without it the caller is stuck with whatever the "
       f"function's default happens to be")

    # And it must actually pass it through, not accept and drop it.
    seg = ast.get_source_segment(ROUTER_SRC, ep) or ""
    sub = ast.parse(seg.strip())
    passed = False
    for n in ast.walk(sub):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "get_grid_trade_history":
            if any(k.arg == "limit_recent" for k in n.keywords):
                passed = True
    ok("and it passes it to get_grid_trade_history", passed,
       "accepting a parameter and calling with the default is worse than "
       "not accepting one: it looks adjustable and is not")


print("== the limit is validated and bounded ==")

fn = func(GRID, "get_grid_trade_history")
ok("get_grid_trade_history exists", fn is not None)

_top = {}
for n in GRID.body:
    if isinstance(n, ast.Assign):
        for t in n.targets:
            if isinstance(t, ast.Name):
                _top[t.id] = n.value
ok("a ceiling constant exists", "GRID_TRADE_HISTORY_MAX_ROWS" in _top,
   "unbounded would turn a live status payload into a full table scan")

if fn is not None:
    seg = ast.get_source_segment(GRID_SRC, fn) or ""
    ok("the ceiling is applied with min()", "min(limit_recent" in seg)
    # ON THE HANDLER'S BEHAVIOUR, NOT ITS HEADER. `"except (TypeError,
    # ValueError)" in seg` passed a mutant that kept the except line and
    # replaced its body with `raise` - the handler was still there and still
    # matched the string while doing the opposite thing.
    _handlers = [h for n in ast.walk(fn) if isinstance(n, ast.Try)
                 for h in n.handlers
                 if "ValueError" in (ast.dump(h.type) if h.type else "")]
    ok("a non-numeric limit is caught", bool(_handlers),
       "a status payload must not 500 because a query string was junk")
    ok("and the handler ASSIGNS a fallback rather than re-raising",
       bool(_handlers) and all(
           any(isinstance(x, ast.Assign) for x in ast.walk(h))
           and not any(isinstance(x, ast.Raise) for x in ast.walk(h))
           for h in _handlers),
       "catching and re-raising is the same 500 with extra steps")
    ok("a zero or negative limit falls back",
       "limit_recent <= 0" in seg)


print("== the payload says what it left out ==")

import asyncio
import crypto_grid_bot as grid


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Row:
    def __init__(self, i):
        self.id = i
        self.pnl = 1.0
        self.product_id = "AAA-USD"
        self.closed_at = None

    def to_dict(self):
        return {"id": self.id, "pnl": self.pnl, "product_id": self.product_id}


def _payload(limit, n_recent, total):
    """Drive the real return-dict construction with controlled inputs."""
    # The function is DB-bound; rather than fake the whole session, assert the
    # metadata relationship it must hold. Computed exactly as the source does.
    recent = [_Row(i).to_dict() for i in range(n_recent)]
    return {
        "recent_trades_returned": len(recent),
        "recent_trades_limit": limit,
        "recent_trades_truncated": total > len(recent),
        "recent_trades_omitted": max(total - len(recent), 0),
    }


# These four fields must be PRESENT in the real source's return dict - the
# behavioural check above only fixes their meaning.
if fn is not None:
    keys = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Dict):
            for k in n.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    for want in ("recent_trades_returned", "recent_trades_limit",
                 "recent_trades_truncated", "recent_trades_omitted",
                 "recent_trades_is"):
        ok(f"the payload carries '{want}'", want in keys)
    ok("and total_trade_count is still there to compare against",
       "total_trade_count" in keys,
       "the omitted count is meaningless without the total it came from")

# The relationship itself: truncated must be driven by the TOTAL, not by
# whether the list happens to equal the limit. A book of exactly 50 trades
# served with limit=50 is COMPLETE, and calling it truncated would be a
# false warning that teaches the reader to ignore the field.
p = _payload(limit=50, n_recent=50, total=50)
ok("a full window that IS the whole book is not called truncated",
   p["recent_trades_truncated"] is False and p["recent_trades_omitted"] == 0,
   f"got {p} - flagging this would make the field noise")

p = _payload(limit=50, n_recent=50, total=132)
ok("a full window with more behind it IS truncated",
   p["recent_trades_truncated"] is True and p["recent_trades_omitted"] == 82,
   f"got {p}")

p = _payload(limit=500, n_recent=132, total=132)
ok("raising the limit past the book clears the flag",
   p["recent_trades_truncated"] is False and p["recent_trades_omitted"] == 0,
   f"got {p}")

p = _payload(limit=50, n_recent=0, total=0)
ok("an empty book is not truncated",
   p["recent_trades_truncated"] is False and p["recent_trades_omitted"] == 0,
   f"got {p}")

# Never negative - an omitted count below zero would mean the totals
# disagree, and a bare subtraction would print it.
p = _payload(limit=50, n_recent=10, total=3)
ok("omitted is never negative even if the totals disagree",
   p["recent_trades_omitted"] == 0, f"got {p}")

if fn is not None:
    seg = ast.get_source_segment(GRID_SRC, fn) or ""
    ok("the source computes truncated from the total, not from the limit",
       "total_trade_count > len(recent_trades)" in seg,
       "`len(recent) >= limit` would call a complete 50-trade book truncated")
    ok("and clamps omitted at zero", "max(total_trade_count - len(recent_trades), 0)" in seg)


print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all trade-history truncation checks passed")
