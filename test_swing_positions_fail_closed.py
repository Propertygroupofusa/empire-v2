"""An unreadable positions list must never permit an entry.

WHY THIS FILE EXISTS. alpaca_swing_bot.get_open_positions returned {} on a
non-200 AND on any exception. An empty dict means "this account holds
nothing", and all three entry caps in that module are computed from it:

    if proxy in open_positions            -> duplicate-entry guard
    sum(1 for s in open_positions.keys()) -> concurrent-position count
    sum(market_value for .values())       -> open-notional budget

So one failed HTTP call did not merely fail to dedup. It reported zero
positions held, zero slots used and zero notional committed - every cap
reading as "nothing used" - which could permit a duplicate buy into a
position already open, past a concurrency ceiling already reached, against
a budget already spent.

Same fail-open shape as 0af01f1 on the crypto side, where an unreadable
balance was shipping sell orders at the tracked quantity. And against this
codebase's own rule: protections may fail open, but anything that MOVES
LIVE ORDERS fails closed.

None and {} are now different answers. Entries are refused for the cycle;
the EXIT loops still run, because an exit is a protection and one that
stops working when a read fails is worse than one that runs on a short
list.

The return-contract checks are BEHAVIOURAL - a fake session, each real
failure mode driven through the actual function. The wiring checks are
structural, since "does this entry block depend on the flag" is a question
about the code.
"""
import ast
import asyncio
import sys

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


import alpaca_swing_bot as bot

SRC = open("alpaca_swing_bot.py", encoding="utf-8").read()
TREE = ast.parse(SRC)


class _Resp:
    def __init__(self, status, payload=None, text="", raise_on_json=False):
        self.status = status
        self._payload = payload
        self._text = text
        self._raise = raise_on_json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        if self._raise:
            raise ValueError("not json")
        return self._payload

    async def text(self):
        return self._text


class _Session:
    def __init__(self, resp=None, boom=None):
        self._resp, self._boom = resp, boom

    def get(self, *a, **k):
        if self._boom is not None:
            raise self._boom
        return self._resp


def call(session):
    return asyncio.get_event_loop().run_until_complete(
        bot.get_open_positions(session))


print("== the return contract: None is not {} ==")

good = call(_Session(_Resp(200, [{"symbol": "META", "market_value": "734.57"}])))
ok("a readable account returns a dict keyed by ticker",
   isinstance(good, dict) and "META" in good, f"got {good!r}")

empty = call(_Session(_Resp(200, [])))
ok("a genuinely EMPTY account returns {} - a real, usable zero",
   empty == {}, f"got {empty!r}")

for status in (401, 403, 429, 500, 502, 503):
    r = call(_Session(_Resp(status, text="nope")))
    ok(f"HTTP {status} returns None, not an empty account",
       r is None, f"got {r!r} - an empty dict here resets every cap to zero")

r = call(_Session(_Resp(200, {"unexpected": "shape"})))
ok("a non-list body returns None", r is None, f"got {r!r}")

r = call(_Session(_Resp(200, raise_on_json=True)))
ok("an unparseable body returns None", r is None, f"got {r!r}")

r = call(_Session(boom=RuntimeError("connection reset")))
ok("an exception returns None", r is None, f"got {r!r}")

# The distinction is the whole point: these two must never be equal.
ok("empty and unreadable are DIFFERENT values",
   empty is not None and call(_Session(_Resp(500))) is None,
   "if both were {}, 'could not read' would permit every entry that "
   "'holds nothing' permits")


print("== entries refuse when unreadable; exits keep running ==")


def fn(name):
    for n in ast.walk(TREE):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            yield n


def _calls_reader(node):
    """True when node CALLS get_open_positions. Matched on the call node, not
    on a substring - `"get_open_positions(session)" in source` also matches
    the function's own `async def` line, which is how the first version of
    this check counted three readers instead of two."""
    if node.name == "get_open_positions":
        return False
    return any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
               and c.func.id == "get_open_positions"
               for c in ast.walk(node))


readers = [n for n in ast.walk(TREE)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
           and _calls_reader(n)]
ok("both cycle functions read the positions", len(readers) == 2,
   f"found {len(readers)} - each has its own entry loop AND exit loop, so a "
   f"fix applied to one leaves the other open")

for r in readers:
    seg = ast.get_source_segment(SRC, r) or ""
    sub = ast.parse(seg.strip())
    name = r.name

    ok(f"{name}: derives an unreadable flag from `is None`",
       "_positions_unreadable = open_positions is None" in seg,
       "a truthiness test would treat a real empty account as unreadable "
       "and stop the bot trading at all")

    # Every entry block must be gated on the flag. Found as: an If whose
    # test mentions the flag and whose body contains the duplicate guard.
    gated = 0
    for n in ast.walk(sub):
        if isinstance(n, ast.If) and "_positions_unreadable" in ast.dump(n.test):
            body = ast.dump(ast.Module(body=n.body, type_ignores=[]))
            if "open_positions" in body and "Continue" in body:
                gated += 1
    ok(f"{name}: the entry block is gated on that flag", gated >= 1,
       "the duplicate-entry guard, the concurrency count and the notional "
       "budget are all computed from open_positions, so one gate covers all "
       "three")

    # The exit loop must NOT be gated - a protection keeps working.
    exit_loops = [n for n in ast.walk(sub)
                  if isinstance(n, ast.For)
                  and "open_positions" in ast.dump(n.iter)
                  and "items" in ast.dump(n.iter)]
    ok(f"{name}: an exit loop over open_positions exists", bool(exit_loops))
    for el in exit_loops:
        # Walk up is awkward on a re-parsed tree; assert instead that no If
        # testing the flag contains the exit loop.
        blocked = any(
            isinstance(n, ast.If) and "_positions_unreadable" in ast.dump(n.test)
            and any(x is el for x in ast.walk(n))
            for n in ast.walk(sub))
        ok(f"{name}: the exit loop is NOT blocked by an unreadable read",
           not blocked,
           "exits are protections; one that stops when a read fails leaves a "
           "real position past its own stop")

    ok(f"{name}: the refusal is logged at ERROR",
       any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
           and n.func.attr == "error"
           and "REFUSED" in " ".join(
               a.value for a in ast.walk(n)
               if isinstance(a, ast.Constant) and isinstance(a.value, str))
           for n in ast.walk(sub)),
       "a refusal nobody can see is indistinguishable from a quiet cycle")


print("== the function says so out loud ==")

g = next(fn("get_open_positions"), None)
ok("get_open_positions exists", g is not None)
if g is not None:
    seg = ast.get_source_segment(SRC, g) or ""
    ok("it NEVER returns an empty dict on a failure path",
       "return {}" not in seg,
       "that literal is the whole defect: it is indistinguishable from a "
       "real empty account")
    errs = [n for n in ast.walk(g)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "error"]
    ok("every failure path logs at ERROR, not warning", len(errs) >= 3,
       f"found {len(errs)} - the old code logged one of them at warning and "
       f"the other two not at all")


print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all swing fail-closed checks passed")
