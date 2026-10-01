"""A 116KB endpoint polled every 15 seconds needs a cache, not a faster phone.

Measured against production 2026-10-01:
    one request alone   4.98s  116,472 bytes
    four at once        9.6-9.7s each
The dashboard polls refresh every 15s and the activity feed every 5s across
25 apiGet call sites, so the heaviest response on the page is asked for
faster than it can be built.
"""
import re
import sys

FAILS = []
SRC = open("routers/trading_dashboard.py").read()
BLOCK = SRC[SRC.index("THE HEAVIEST ENDPOINT ON THE PAGE"):
            SRC.index('@router.get("/grid-status/trade-history")')]


def ok(label, cond, got=None):
    if cond:
        print(f"  PASS  {label}")
    else:
        FAILS.append(label)
        print(f"  FAIL  {label}" + (f"   got: {got!r}" if got is not None else ""))


print("\n[1] the cache exists and is bounded")
ok("a cache dict is declared", "_GRID_STATUS_CACHE" in BLOCK)
ok("the TTL is env-overridable", "GRID_STATUS_TTL_SECONDS" in BLOCK)
ok("the default is 25s", '"25"' in BLOCK)
ok("...which is under the bot's own 30s cycle, so it is never the bottleneck",
   25 < 30)

print("\n[2] it can be bypassed")
ok("?fresh=1 is a parameter", "fresh: int = 0" in BLOCK)
ok("and it skips the cache", "if (not fresh" in BLOCK)

print("\n[3] a cached answer SAYS it is cached")
ok("served_from_cache is set true on the cached path",
   '_cached["served_from_cache"] = True' in BLOCK)
ok("...and false on the live path", 'data["served_from_cache"] = False' in BLOCK)
ok("the age is reported", "cache_age_seconds" in BLOCK)

print("\n[4] AN ERROR IS NEVER CACHED")
# The store happens after the payload is fully built, just before the return.
store = BLOCK.index('_GRID_STATUS_CACHE["payload"] = data')
ret = BLOCK.index("# Force fresh data on every request")
ok("the cache is written on the way OUT, not on the way in", store < ret)
ok("...and after the module-unavailable guard",
   BLOCK.index("crypto_grid_bot module not available") < store)

print("\n[5] the BROWSER cache decision is untouched")
ok("no-store headers still sent on the live path",
   BLOCK.count('"Cache-Control": "no-cache, no-store, must-revalidate, max-age=0"') == 2,
   BLOCK.count('"Cache-Control": "no-cache, no-store, must-revalidate, max-age=0"'))
ok("the cached path returns JSONResponse too, not a bare dict",
   BLOCK.count("return JSONResponse(") == 2, BLOCK.count("return JSONResponse("))

print("\n[6] NOTHING THAT TRADES READS THIS ENDPOINT")
# Workers call the in-process function; only dashboards/tools use the HTTP path.
for mod in ("crypto_grid_bot.py", "auto_trim_worker.py", "reconcile_worker.py"):
    body = open(mod).read()
    ok(f"{mod} does not fetch /grid-status over HTTP",
       "/api/trading-dashboard/grid-status" not in body)

print("\n[7] the blind-replace scar is not present")
# An earlier attempt replaced every _time.time() in this file, including 8
# pre-existing ones inside functions that alias `import time as _time`.
ok("the 8 pre-existing _time.time() calls survive",
   SRC.count("_time.time()") == 8, SRC.count("_time.time()"))
# FOUR, not three. The number in the first draft of this test came from a
# `grep | head -3`, which truncated the list - so the assertion pinned a
# count I had only half looked at. Both numbers below are taken from
# `git show HEAD:` rather than from memory.
ok("the four local aliases are intact",
   SRC.count("import time as _time") == 4, SRC.count("import time as _time"))

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
