"""Exactly one process may trade the Coinbase wallet - and never zero.

Found live on 2026-09-25. The grid could only run on the dedicated
crypto-trading service, because main.py delegated to it and started
nothing itself:

    elif CRYPTO_STRATEGY_MODE == "grid_fleet":
        log.info("Grid Fleet selected; execution is delegated ...")

The delegation was CORRECT - two processes on one wallet would
double-order. But it left no fallback. When that service silently was not
running the loop, the fleet was dead with no alarm: the master switch read
off, the heartbeat read "never recorded a cycle on this database", $572 sat
idle, and every piece of configuration looked right.

A LEASE makes a standby safe instead of dangerous:

  * whoever runs a cycle stamps their identity and renews it
  * a process may run only if it holds the lease, nobody holds it, or the
    holder has gone silent past GRID_LEASE_STALE_SECONDS
  * the dedicated service, cycling every 30s, renews constantly and keeps
    the lease forever - the standby can never steal it from a healthy owner

TWO FAILURES ARE BEING PREVENTED, in opposite directions, and a change
that fixes one by breaking the other is not a fix:

    DOUBLE-TRADE   two owners at once, on one real wallet
    DEAD FLEET     zero owners, silently, which is what actually happened

Run: python3 test_grid_lease.py
"""
import ast
import os
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


STALE = 180
STORE = {}


def owner_hash(o):
    return float(zlib.crc32(o.encode("utf-8")))


def acquire(me, now):
    """Pure re-statement of acquire_grid_lease()'s decision rule."""
    mine = owner_hash(me)
    row = STORE.get("lease")
    if row is None:
        STORE["lease"] = {"held": mine, "seen": now}
        return True, "claimed (was unheld)"
    if row["held"] == mine:
        row["seen"] = now
        return True, "renewed"
    if now - row["seen"] > STALE:
        row["held"], row["seen"] = mine, now
        return True, f"TAKEN OVER after {now - row['seen']:.0f}s"
    return False, f"held by another, seen {now - row['seen']:.0f}s ago"


DED, WEB = "crypto-trading:1", "web:2"
t = 1000.0

# --- the healthy case: the dedicated service owns it and keeps it --------
ok("the dedicated service can claim an unheld lease", acquire(DED, t)[0])
ok("the standby must NOT trade while the owner is alive",
   acquire(WEB, t + 10)[0] is False)
ok("the owner renews its own lease", acquire(DED, t + 30)[0])
ok("the standby is still refused after a renewal",
   acquire(WEB, t + 40)[0] is False)
ok("still refused just BEFORE the stale limit",
   acquire(WEB, t + 30 + STALE - 5)[0] is False)

# --- the failure this was built for: the owner dies ---------------------
allowed, why = acquire(WEB, t + 30 + STALE + 1)
ok("the standby TAKES OVER once the lease goes stale", allowed)
ok("and the takeover is announced, not silent", "TAKEN OVER" in why)

# --- the reverse must also hold: no stealing back ------------------------
# A returning service must NOT reclaim while the standby is actively
# renewing, or both trade at once. The handover is one-way until stale.
ok("a RETURNING service does not steal a live lease back",
   acquire(DED, t + 30 + STALE + 10)[0] is False)
ok("the standby keeps renewing while it holds it",
   acquire(WEB, t + 30 + STALE + 20)[0])
ok("the returning service reclaims only after the standby goes silent",
   acquire(DED, t + 30 + STALE + 20 + STALE + 1)[0])

# --- never two owners, across a long interleaved run --------------------
STORE.clear()
now, holders = 0.0, []
for step in range(200):
    now += 20
    a_ok, _ = acquire(DED, now)
    b_ok, _ = acquire(WEB, now)
    holders.append((a_ok, b_ok))
ok("no tick ever grants BOTH processes the lease",
   all(not (a and b) for a, b in holders))
ok("and some tick grants at least one - the fleet is never orphaned",
   any(a or b for a, b in holders))

# --- the implementation matches the rule --------------------------------
src = open(os.path.join(HERE, "crypto_grid_bot.py"), encoding="utf-8").read()
tree = ast.parse(src)
fn = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
      and n.name == "acquire_grid_lease"][0]
body = "\n".join(ast.unparse(s) for s in fn.body[1:])
ok("an unreadable lease FAILS CLOSED - never a second trader",
   "return (False" in body or "return False" in body)
ok("the stale window is configurable", "GRID_LEASE_STALE_SECONDS" in src)
ok("the owner id includes SERVICE_ROLE so the two services differ",
   "SERVICE_ROLE" in src.split("def _grid_owner_id")[1][:400])

cycle = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
         and n.name == "run_grid_branches_cycle"][0]
cbody = "\n".join(ast.unparse(s) for s in cycle.body[1:])
ok("the cycle acquires the lease before trading", "acquire_grid_lease" in cbody)
ok("the heartbeat is stamped BEFORE the lease check, so a non-owner still "
   "proves it is alive",
   cbody.index("_record_grid_heartbeat") < cbody.index("acquire_grid_lease"))
ok("a takeover is logged at WARNING, not swallowed",
   "TAKEN OVER" in cbody and "log.warning" in cbody)

main = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
ok("the web service now starts a standby thread instead of only logging",
   "Grid Fleet standby thread started" in main)
ok("and it no longer merely delegates and starts nothing",
   "execution is delegated to the dedicated crypto-trading service\")" not in main)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
