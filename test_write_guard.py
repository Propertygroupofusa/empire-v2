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
    # The one the prefix list missed. routers/crypto_trading.py mounts at
    # /api/crypto, the guard listed /api/crypto-trading, and this endpoint
    # market-sells an asset in full: "Sell all of an asset at market price
    # immediately. Supports any asset." Unprotected for the first hour the
    # guard existed.
    "/api/crypto/withdraw",
    "/api/crypto/order",
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
        "/api/alpaca-funding/transfer")))
# DENY BY DEFAULT. An allowlist of things to protect was wrong within the
# hour of being written; a route that moves will not silently fall out of
# coverage now, and a route invented tomorrow is covered the day it exists.
ok("a path nobody has written yet is already covered",
   G.is_protected("POST", "/api/some/route/invented/next/month"))
ok("a route that MOVES is still covered",
   G.is_protected("POST", "/api/crypto") and G.is_protected("POST", "/api/crypto-trading/x"))
ok("there is no list of things to protect - only of exceptions",
   not hasattr(G, "PROTECTED_PREFIXES") and hasattr(G, "OPEN_PREFIXES"))
ok("and the exception list is short enough to audit",
   len(G.OPEN_PREFIXES) + len(G.OPEN_SUFFIXES) <= 4,
   f"{len(G.OPEN_PREFIXES)} prefixes, {len(G.OPEN_SUFFIXES)} suffixes")

print("\nthe exceptions are the ones that cannot work any other way")
ok("Stripe webhooks stay open - Stripe cannot send our header",
   not G.is_protected("POST", "/api/orders/webhook/stripe")
   and not G.is_protected("POST", "/api/subscriptions/webhook/stripe"))
ok("but a path merely CONTAINING 'webhook' is not open",
   G.is_protected("POST", "/api/trading-dashboard/webhook/stripe/../force-buy"))
ok("the reason each exception exists is written down",
   "Stripe cannot send our header" in SRC_GUARD and "issues credentials" in SRC_GUARD)

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
   "issues credentials" in SRC_GUARD,
   "a shared token in front of login locks everyone out of the thing that issues credentials")

print("\nit is wired as middleware, not as 110 decorators")
MAIN = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py"),
            encoding="utf-8").read()
ok("main.py installs it", "write_guard" in MAIN and 'app.middleware("http")' in MAIN)
SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "write_guard.py"),
           encoding="utf-8").read()
ok("the file says why middleware beats decorators",
   "110 chances to forget one" in SRC)
ok("and records the miss that forced deny-by-default",
   "Sell all of an" in SRC and "25% of the account" in SRC,
   "an allowlist of things to defend fails silently when a route moves")
ok("and records that reads are still exposed",
   "WHAT IS NOT COVERED" in SRC and "still return the full holdings" in SRC,
   "an unfixed hole must be written down, not implied")

print("\nthe arming can be checked without firing a real order")
DASH = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "routers", "trading_dashboard.py"), encoding="utf-8").read()
ok("a status endpoint exists", '@router.get("/write-guard")' in DASH)
ok("it never returns the token",
   "os.getenv(write_guard.TOKEN_ENV)" in DASH and '"token":' not in DASH.split('"/write-guard"')[1][:2000])
ok("nor a prefix or an exact length - a band only",
   '"token_length_band"' in DASH and "len(tok) < 32" in DASH,
   "an exact length narrows a brute-force search")
ok("it says plainly that a local variable does nothing",
   "must be set where the server runs" in DASH)
ok("and repeats that reads are unprotected",
   '"reads_protected": False' in DASH)
ok("the reason it exists is recorded",
   "corrected six times through the Railway UI" in DASH,
   "a guard whose arming cannot be checked is a guard nobody can trust")

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
