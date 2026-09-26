"""The guard, exercised on real paths.

The endpoint it protects placed a real $10 BTC order on 2026-09-26 with one
curl and no credential. These check the three things that decide whether a
guard is worth having: that it covers every write, that an unset token
refuses rather than permits, and that it cannot be got round.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


import write_guard as G

SRC_GUARD = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "write_guard.py"), encoding="utf-8").read()

MONEY_MOVERS = [
    "/api/trading-dashboard/grid-status/force-buy",
    "/api/trading-dashboard/grid-status/quick-buy",
    "/api/trading-dashboard/grid-status/mode",
    "/api/trading-dashboard/grid-status/switch-to-scale-bot",
    "/api/trading-dashboard/family-tree-status/liquidate-and-buy-btc",
    "/api/trading-dashboard/alpaca-overview/liquidate-and-buy-spy",
    "/api/trading-dashboard/crypto-strategy-override",
]

print("\nevery endpoint that can move money is covered")
for p in MONEY_MOVERS:
    ok(f"  POST {p.split('/')[-1]}", G.is_protected("POST", p))
ok("PUT, PATCH and DELETE are covered too",
   all(G.is_protected(m, MONEY_MOVERS[0]) for m in ("PUT", "PATCH", "DELETE")))
ok("GET is NOT blocked - the dashboard must still render",
   not G.is_protected("GET", MONEY_MOVERS[0]))
ok("HEAD and OPTIONS pass, so CORS preflight is not broken",
   not G.is_protected("OPTIONS", MONEY_MOVERS[0]) and not G.is_protected("HEAD", MONEY_MOVERS[0]))
ok("other money routers are covered",
   all(G.is_protected("POST", p) for p in
       ("/api/sweep/run", "/api/orders/create", "/api/payments/charge",
        "/api/crypto-trading/buy", "/api/alpaca-funding/transfer")))
ok("an unrelated path is untouched", not G.is_protected("POST", "/api/study/notes"))

print("\nwith no token set, writes are REFUSED - not allowed")
os.environ.pop(G.TOKEN_ENV, None)
v = G.check("POST", MONEY_MOVERS[0], "")
ok("an unset token refuses", v is not None and v[0] == 503, str(v))
ok("and says how to fix it", G.TOKEN_ENV in v[1] and "header" in v[1])
ok("and says WHY failing closed was chosen",
   "would protect nothing" in v[1],
   "a default that permits until configured is protection that does nothing")
ok("even a presented token cannot open an unconfigured deployment",
   G.check("POST", MONEY_MOVERS[0], "anything")[0] == 503,
   "otherwise the guard is bypassed by guessing that it is unset")

print("\nwith a token set")
os.environ[G.TOKEN_ENV] = "s3cret-long-random-value"
ok("the right token is allowed", G.check("POST", MONEY_MOVERS[0], "s3cret-long-random-value") is None)
ok("a missing one is 401", G.check("POST", MONEY_MOVERS[0], "")[0] == 401)
ok("a wrong one is 403", G.check("POST", MONEY_MOVERS[0], "nope")[0] == 403)
ok("a prefix of the real token is refused",
   G.check("POST", MONEY_MOVERS[0], "s3cret")[0] == 403,
   "compare_digest, not startswith")
ok("case matters", G.check("POST", MONEY_MOVERS[0], "S3CRET-LONG-RANDOM-VALUE")[0] == 403)
ok("GET is still open with a token configured",
   G.check("GET", "/api/trading-dashboard/grid-status", "") is None)
ok("comparison is constant-time", "compare_digest" in open(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "write_guard.py")).read())

print("\nthere is no exemption list, and that is the point")
# The first draft carved out login/register/refresh. Every entry was dead:
# /api/auth is not under a protected prefix, so nothing in that list could
# ever have fired. A carve-out that cannot fire reads as considered and
# protects nothing.
ok("no exemption list exists at all", not hasattr(G, "EXEMPT"))
ok("login is untouched because /api/auth is out of scope, not exempted",
   not G.is_protected("POST", "/api/auth/login"))
ok("and so is every other auth route - one rule, no special cases",
   not G.is_protected("POST", "/api/auth/change-password"))
ok("the reason is written down",
   "authentication domain" in SRC_GUARD,
   "a shared token in front of login locks everyone out of the thing that issues credentials")

print("\nit is wired as middleware, not as 110 decorators")
MAIN = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py"),
            encoding="utf-8").read()
ok("main.py installs it", "write_guard" in MAIN and 'app.middleware("http")' in MAIN)
SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "write_guard.py"),
           encoding="utf-8").read()
ok("the file says why middleware beats decorators",
   "110 chances to forget one" in SRC)
ok("and records that reads are still exposed",
   "WHAT IS NOT COVERED" in SRC and "still return the full holdings" in SRC,
   "an unfixed hole must be written down, not implied")

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
