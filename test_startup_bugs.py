"""Three startup failures read straight off a production boot log.

None of them stopped the container. All three were logged as warnings and
scrolled past on every single deploy, which is exactly why they survived:
a failure that does not stop anything and repeats forever stops being read.
"""
import ast
import asyncio
import re
import sys
import threading
import weakref

_checks = []


def ok(label, cond, detail=""):
    _checks.append((label, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  -- {detail}" if detail and not cond else ""))


# ---------------------------------------------------------------------------
# 1. name 'engine' is not defined
#
#    main.py moved from a module-level `engine` to a get_engine() accessor.
#    Most call sites were updated. Eight were not, and the import fallback
#    kept binding the OLD name - so when the import succeeded, the bare
#    references raised NameError, and when it failed the fallback did not
#    even bind the name the module actually uses.
#
#    On every boot: "Monitor tables failed", "Foreign key validation
#    failed", "Retention manager failed" - all three, same cause.
# ---------------------------------------------------------------------------
MAIN = open("main.py").read()
MAIN_TREE = ast.parse(MAIN)

# Names bound anywhere in the module, so a bare Name load can be judged.
_bound = {"get_engine"}
for node in ast.walk(MAIN_TREE):
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                _bound.add(t.id)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            _bound.add(a.asname or a.name.split(".")[0])

_bare_engine_loads = []
for node in ast.walk(MAIN_TREE):
    if isinstance(node, ast.Name) and node.id == "engine" and isinstance(node.ctx, ast.Load):
        _bare_engine_loads.append(node.lineno)

ok("no bare `engine` is ever read in main.py", not _bare_engine_loads,
   f"still read at lines {_bare_engine_loads}")

ok("the import fallback binds get_engine, not the dead name",
   "    get_engine = None" in MAIN and "\n    engine = None" not in MAIN)

ok("every dialect check goes through the accessor",
   "get_engine().dialect" in MAIN and not re.search(r"(?<![\w.])engine\.dialect", MAIN))

ok("the retention manager is handed the accessor's engine",
   not re.search(r"retention_manager\.\w+\((?:[^()]*?, )?engine[,)]", MAIN))

# The three blocks whose warnings appeared in the boot log must still be
# guarded - the fix is to stop them raising, not to stop catching.
for fn in ("create_monitor_tables", "validate_foreign_keys"):
    ok(f"{fn} still exists to be called", f"def {fn}" in MAIN)


# ---------------------------------------------------------------------------
# 2. "<Semaphore ...> is bound to a different event loop"
#
#    This process runs several event loops: uvicorn's, plus one inside each
#    background bot thread. A single cached global binds to whichever loop
#    touches it first; every other loop then raises on every acquire that
#    has to WAIT (the uncontended fast path skips the loop check, which is
#    why this only bites under load - exactly when candles are being pulled
#    for many coins at once).
#
#    Cost in the log: every candle page failed on the losing loop, so
#    "[HORIZON] no usable history for BTC-USD" and the same for NEAR, ONDO
#    and FLOKI. Coin selection was running blind.
# ---------------------------------------------------------------------------
CSB = open("crypto_selection_backtest.py").read()

ok("the throttle is keyed per event loop", "WeakKeyDictionary" in CSB)
ok("it asks for the RUNNING loop to key on", "asyncio.get_running_loop()" in CSB)
ok("no single cached global semaphore remains",
   "_CANDLE_HTTP_SEMAPHORE = None" not in CSB)
ok("a synchronous caller still gets a usable object rather than an exception",
   "except RuntimeError" in CSB)
ok("the weak keying is explained as leak avoidance, not decoration",
   "collected with it" in CSB)


def _exercise(getter, tag, out, fanout=5):
    """Fan out past the semaphore's capacity so waiters really form.

    Without contention asyncio.Semaphore.acquire() returns on a fast path
    that never checks the loop, and the bug does not reproduce.
    """
    async def worker(sem):
        async with sem:
            await asyncio.sleep(0.01)

    async def main():
        sem = getter()
        await asyncio.gather(*[worker(sem) for _ in range(fanout)])

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
        out.append((tag, None))
    except Exception as e:
        out.append((tag, f"{type(e).__name__}: {e}"))
    finally:
        loop.close()


def _run_across_loops(getter, n=3):
    out = []
    for i in range(n):
        t = threading.Thread(target=_exercise, args=(getter, f"loop{i}", out))
        t.start()
        t.join()
    return out


# The old shape, kept here as the regression it is: if someone reverts to a
# single cached global, this test says exactly what breaks and where.
def _old_style_getter():
    cache = {"sem": None}

    def get():
        if cache["sem"] is None:
            cache["sem"] = asyncio.Semaphore(2)
        return cache["sem"]
    return get


_old = _run_across_loops(_old_style_getter())
ok("the old single-global shape really does fail on the 2nd and 3rd loop",
   _old[0][1] is None and all(r is not None and "different event loop" in r
                              for _, r in _old[1:]),
   f"{_old}")

sys.path.insert(0, ".")
import crypto_selection_backtest as csb  # noqa: E402

_new = _run_across_loops(csb._get_candle_semaphore)
ok("the shipped getter works on every loop", all(r is None for _, r in _new), f"{_new}")

def _collect_ids():
    ids = []

    def grab():
        async def main():
            ids.append(id(csb._get_candle_semaphore()))
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(main())
        finally:
            loop.close()
    for _ in range(3):
        t = threading.Thread(target=grab)
        t.start()
        t.join()
    return ids


_ids = _collect_ids()
ok("three loops get three distinct semaphores", len(set(_ids)) == 3, f"{_ids}")


def _same_loop_twice():
    got = []

    async def main():
        got.append(id(csb._get_candle_semaphore()))
        got.append(id(csb._get_candle_semaphore()))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
    return got


_same = _same_loop_twice()
ok("one loop gets the SAME semaphore twice, so it still throttles",
   _same[0] == _same[1], f"{_same}")

ok("the capacity per loop is unchanged", csb._CANDLE_HTTP_CONCURRENCY == 2)

_failed = [l for l, p in _checks if not p]
print(f"\n{len(_checks) - len(_failed)} passed, {len(_failed)} failed")
sys.exit(1 if _failed else 0)
