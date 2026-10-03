"""The guard records what arrived - and never the credential.

Four rounds of one session were spent guessing between "the browser never
sent it", "the guard refused it" and "the endpoint wrote nothing", because
the only evidence was a log viewer the owner had to filter by hand on a
phone. This makes the answer readable.

THE PROPERTY THAT MATTERS MOST is the negative one: a diagnostic that
leaks the token it is diagnosing is worse than no diagnostic.
"""
import sys
sys.path.insert(0, "/home/user/empire-v2")
import write_guard as W

fail = 0
def ok(label, cond, detail=""):
    global fail
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + ("" if cond or not detail else f"   -> {detail}"))
    if not cond: fail += 1

SECRET = "sup3r-s3cret-dashboard-token-value"

print("\n[1] it starts empty, and empty MEANS something")
W._ATTEMPTS.clear()
ok("no attempts recorded at rest", W.recent_attempts() == [])

print("\n[2] a refusal is recorded with its status")
W.record_attempt("POST", "/api/trading-dashboard/grid-status/set-levels", 401, False,
                 "Missing x-dashboard-token header")
W.record_attempt("POST", "/api/trading-dashboard/grid-status/set-levels", 403, True,
                 "does not match this deployment's token")
rows = W.recent_attempts()
ok("two attempts recorded", len(rows) == 2, len(rows))
ok("newest first", rows[0]["guard_status"] == 403, rows[0])
ok("401 says no token was present", rows[1]["token_was_present"] is False, rows[1])
ok("403 says a token WAS present", rows[0]["token_was_present"] is True, rows[0])
ok("the path is recorded", "set-levels" in rows[0]["path"])

print("\n[3] a PASS is recorded too - it separates door from endpoint")
W.record_attempt("POST", "/api/trading-dashboard/grid-status/reconcile-slices", None, True,
                 "allowed through to the endpoint")
ok("guard_status None means it reached the endpoint",
   W.recent_attempts()[0]["guard_status"] is None)

print("\n[4] NO CREDENTIAL MATERIAL, ANYWHERE")
W._ATTEMPTS.clear()
W.record_attempt("POST", "/x", 403, True, "does not match this deployment's token")
blob = repr(W.recent_attempts())
ok("the token value is absent", SECRET not in blob)
for n in (4, 8, 12, 16):
    ok(f"no {n}-char prefix of the token leaks", SECRET[:n] not in blob)
ok("no length is recorded", str(len(SECRET)) not in blob.replace("403", ""))
import inspect
src = inspect.getsource(W.record_attempt)
ok("record_attempt takes a BOOL, never the token itself",
   "presented_present: bool" in src)
ok("it stores only bool(presented_present)", "bool(presented_present)" in src)
for bad in ("hashlib", "sha", "md5", "[:4]", "[:8]", "len(presented"):
    ok(f"no {bad} in the recorder", bad not in src)

print("\n[5] the middleware passes a BOOL, not the value")
mw = inspect.getsource(W.guard)
ok("refusal path passes bool(_p)", "record_attempt(request.method, request.url.path, status, bool(_p)" in mw)
ok("allow path passes bool(_p)", "bool(_p)," in mw)
ok("the raw presented value is never handed to the recorder",
   "record_attempt(request.method, request.url.path, status, _p," not in mw
   and "record_attempt(request.method, request.url.path, None, _p," not in mw)

print("\n[6] it is bounded - a ring buffer, not a leak")
W._ATTEMPTS.clear()
for i in range(W._ATTEMPTS_MAX + 25):
    W.record_attempt("POST", f"/p{i}", 403, True)
ok(f"capped at {W._ATTEMPTS_MAX}", len(W.recent_attempts()) == W._ATTEMPTS_MAX,
   len(W.recent_attempts()))
ok("it keeps the NEWEST", W.recent_attempts()[0]["path"].endswith(str(W._ATTEMPTS_MAX + 24)),
   W.recent_attempts()[0]["path"])

print("\n[7] only protected paths are worth recording")
ok("auth paths are not protected", W.is_protected("POST", "/api/auth/login") is False)
ok("a GET is not protected", W.is_protected("GET", "/api/trading-dashboard/grid-status") is False)
ok("a write IS protected", W.is_protected("POST", "/api/trading-dashboard/grid-status/set-levels") is True)

print("\n[8] the endpoint says what an empty list MEANS")
ep = open("/home/user/empire-v2/routers/trading_dashboard.py").read()
i = ep.find('@router.get("/write-attempts")')
body = ep[i:i + 2600]
ok("it is a GET, so the guard never blocks reading it", '@router.get("/write-attempts")' in body)
ok("empty is explained as a browser-side fault", "never left the browser" in body)
ok("it states that a restart empties it", "a restart empties it" in body)
ok("and that empty-after-restart is UNKNOWN, not proof",
   "UNKNOWN, not proof" in body)
ok("it declares it carries no token material", "carries_no_token_material" in body)

print()
if fail:
    print(f"{fail} FAILED"); sys.exit(1)
print("all checks passed")
