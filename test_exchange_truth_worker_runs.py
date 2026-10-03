"""check_once() must actually RUN against the factory main.py really passes.

THE BUG THIS EXISTS FOR, 2026-10-01

exchange_truth_worker shipped with

    async with session_factory() as db:

main.py passes `get_session_factory` ITSELF, not a session factory. So the
first call returns the sessionmaker and you need a SECOND call to open a
session - `session_factory()()`, which is what every other worker in this
repo already does. With one call, `async with` got a sessionmaker, raised,
was caught by run_periodically's own `except Exception`, logged as a
warning, and the loop carried on sleeping. The heartbeat ticked. Nothing was
written for ten minutes and the only symptom was a table that stayed empty -
which looks exactly like "nothing is wrong yet."

I had checked that the signatures lined up. Signatures were never the
problem; the CALLING CONVENTION was, and only running it finds that.

So this test runs check_once() end to end with stubs shaped like the real
collaborators, including a factory with the real two-call shape. It is
deliberately not a grep: a grep for "session_factory()()" would pass on a
file that never executes that line.

Run: python3 test_exchange_truth_worker_runs.py
"""
import asyncio
import os
import sys
import types

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

checks = []


def ok(label, cond, detail=""):
    checks.append((label, bool(cond), detail))


def section(t):
    checks.append((t, None, ""))


import audit_models as am                      # noqa: E402
import exchange_truth_worker as w              # noqa: E402

BRANCHES = [
    {"product_id": "ZEC-USD", "bot_name": "crypto_grid_2",
     "current_price": 1600.0, "total_unrealized_net_usd": -399.39,
     "slices": [{"qty": 1.41897}]},
    {"product_id": "QNT-USD", "bot_name": "crypto_grid_14",
     "current_price": 261.0, "total_unrealized_net_usd": 67.34,
     "slices": [{"qty": 0.67598}]},
]
BALANCES = {"available": True,
            "available_units": {"ZEC": 1.33176, "QNT": 0.00097},
            "held_including_zero": {"ZEC": 1.33176, "QNT": 0.00097}}


class _Session:
    def __init__(self, store, flag): self.store = store; self.flag = flag
    async def execute(self, stmt):
        class R:
            def scalar_one_or_none(self_inner): return None
        return R()
    def add(self, o): self.store.append(o)
    async def commit(self): self.flag["commits"] = self.flag.get("commits", 0) + 1
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


def real_shaped_factory(store, flag):
    """Mirrors main.py EXACTLY: the thing passed in is get_session_factory,
    so it must be called twice before you have a session."""
    def get_session_factory():
        def sessionmaker():
            flag["sessions_opened"] += 1
            return _Session(store, flag)
        return sessionmaker
    return get_session_factory


def install_stubs(balances=BALANCES, branches=BRANCHES):
    ac = types.ModuleType("account_census")
    async def fetch_balances(session): return balances
    ac.fetch_balances = fetch_balances
    sys.modules["account_census"] = ac

    gb = types.ModuleType("crypto_grid_bot")
    async def get_grid_status(): return {"branches": branches}
    gb.get_grid_status = get_grid_status
    sys.modules["crypto_grid_bot"] = gb

    class _HTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    ah = types.ModuleType("aiohttp")
    ah.ClientSession = lambda *a, **k: _HTTP()
    w.aiohttp = ah


install_stubs()
loop = asyncio.get_event_loop()

# ── 1. it runs, and it opens a session ───────────────────────────────
section("[1] check_once runs end to end against main.py's real factory shape")
store, flag = [], {"sessions_opened": 0}
try:
    r = loop.run_until_complete(w.check_once(real_shaped_factory(store, flag)))
    ran, err = True, None
except Exception as e:
    r, ran, err = None, False, f"{type(e).__name__}: {e}"
ok("it did not raise", ran, err or "")
ok("a session was actually OPENED (the two-call convention)",
   flag["sessions_opened"] == 1, f"opened {flag['sessions_opened']}")
if ran:
    ok("both branches were measured", r["measured"] == 2, str(r))
    ok("the backed branch reads MATCHED", r["matched"] == 1, str(r))
    ok("the dust branch reads MISMATCH", r["mismatch"] == 1, str(r))
    ok("a failure row was written", r["failures_written"] == 1, str(r))
    ok("rows actually reached the session",
       len([o for o in store if isinstance(o, am.ExchangeTruthFailure)]) == 1)
    ok("a control row per branch",
       len([o for o in store if isinstance(o, am.BranchControlState)]) == 2)
    # NOT a vacuous assertion. An uncommitted session discards every row
    # above, which would look identical to this test passing.
    ok("and the transaction was COMMITTED", flag.get("commits") == 1,
       f"commits={flag.get('commits')}")

# ── 2. the unreadable path still writes nothing, in the real loop ────
section("[2] an unreadable balance opens no session and writes nothing")
store2, flag2 = [], {"sessions_opened": 0}
install_stubs(balances={"available": False})
r2 = loop.run_until_complete(w.check_once(real_shaped_factory(store2, flag2)))
ok("no session opened at all", flag2["sessions_opened"] == 0)
ok("nothing written", store2 == [])
ok("it says it skipped", r2.get("skipped_unreadable") is True, str(r2))
ok("it does NOT claim anything matched", r2.get("measured") == 0, str(r2))

# ── 3. no branches is not a verdict either ───────────────────────────
section("[3] no branches reported is not 'everything is fine'")
store3, flag3 = [], {"sessions_opened": 0}
install_stubs(branches=[])
r3 = loop.run_until_complete(w.check_once(real_shaped_factory(store3, flag3)))
ok("no session opened", flag3["sessions_opened"] == 0)
ok("nothing written", store3 == [])
ok("measured is zero", r3.get("measured") == 0)

# ── 4. the loop survives a failing pass ──────────────────────────────
section("[4] run_periodically never dies on one bad pass")
install_stubs()
async def _one_bad_pass():
    orig = w.check_once
    calls = {"n": 0}
    async def boom(sf):
        calls["n"] += 1
        raise RuntimeError("venue exploded")
    w.check_once = boom
    w.CHECK_SECONDS = 0
    task = asyncio.ensure_future(w.run_periodically(real_shaped_factory([], {"sessions_opened": 0})))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    w.check_once = orig
    return calls["n"], w.HEARTBEAT
n, hb = loop.run_until_complete(_one_bad_pass())
ok("it kept going after the error", n > 1, f"only {n} pass(es)")
ok("the error is recorded, not swallowed",
   "venue exploded" in str(hb.get("last_error")), str(hb.get("last_error")))
ok("the heartbeat ticks even on failure", (hb.get("passes") or 0) > 0)

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
