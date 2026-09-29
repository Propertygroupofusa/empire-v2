"""The id sent to the venue is the intent, not a coin toss.

WHY IT EXISTS. Ten call sites in this repository build client_order_id as
str(uuid.uuid4()). That field is the venue's own duplicate key, so a random
value per attempt means the system has no duplicate protection at any
layer: a retry, or an event delivered twice, opens a second real position.

So the checks below are mostly about two calls that must produce the SAME
id, and a handful that must produce a DIFFERENT one. Both directions
matter: an id that never repeats is not an idempotency key, and an id that
repeats when the intent genuinely changed silently drops a real order.
"""
import ast
import inspect
import subprocess
import sys

import order_idempotency as oi

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


# One complete, realistic intent: a LINK-USD parked sell, the shape that
# actually closes positions on this account today.
BASE = dict(cycle_id="c-8841", slice_id="s2", side="SELL",
            product_id="LINK-USD", target_price="23.016", quantity="4.1",
            attempt=0)


def coid(**over):
    d = dict(BASE)
    d.update(over)
    return oi.client_order_id(**d)


print("== the same intent is the same id ==")
ok("two calls agree", coid() == coid())
ok("a third call still agrees", coid() == coid() == coid())
ok("it is not empty", bool(coid()), repr(coid()))

print("== a different process agrees too ==")
# A key that depended on anything per-process - a hash seed, a module-level
# counter, the clock - would pass every check above and still fail here,
# which is the case that matters: the polling loop and an event loop are
# two processes deciding to place one order.
_prog = (
    "import order_idempotency as oi;"
    "print(oi.client_order_id(cycle_id='c-8841', slice_id='s2', side='SELL',"
    " product_id='LINK-USD', target_price='23.016', quantity='4.1', attempt=0))"
)
# -B: this child must not leave a __pycache__ behind. A later run against a
# changed module of the same byte-size in the same second would import that
# stale bytecode instead - which is how a broken module passes its own test.
_out = subprocess.run([sys.executable, "-B", "-c", _prog], capture_output=True,
                      text=True, cwd=".",
                      env={"PYTHONHASHSEED": "1", "PATH": "/usr/bin:/bin",
                           "PYTHONDONTWRITEBYTECODE": "1"})
ok("a fresh interpreter produces the identical id",
   _out.stdout.strip() == coid(), f"{_out.stdout.strip()!r} vs {coid()!r} {_out.stderr[-200:]}")

print("== one value written two ways is one intent ==")
ok("23.016 == 23.0160", coid(target_price="23.016") == coid(target_price="23.0160"))
ok("4.1 == 4.10", coid(quantity="4.1") == coid(quantity="4.10"))
ok("100 == 100.0", coid(target_price="100") == coid(target_price="100.0"))
ok("a float and its text agree", coid(target_price=23.5) == coid(target_price="23.5"))
ok("side is case-insensitive", coid(side="sell") == coid(side="SELL"))
ok("surrounding whitespace is not an intent",
   coid(slice_id=" s2 ") == coid(slice_id="s2"))

print("== a different intent is a different id ==")
_variants = {
    "baseline": coid(),
    "attempt": coid(attempt=1),
    "price": coid(target_price="23.017"),
    "price one tick up": coid(target_price="23.0161"),
    "quantity": coid(quantity="4.2"),
    "side": coid(side="BUY"),
    "product": coid(product_id="ETH-USD"),
    "cycle": coid(cycle_id="c-8842"),
    "slice": coid(slice_id="s3"),
}
ok("all nine differ from one another",
   len(set(_variants.values())) == len(_variants),
   str({k: v for k, v in _variants.items()}))
ok("attempt 1 is not attempt 0", _variants["attempt"] != _variants["baseline"])
ok("100.1 is not 100.2",
   coid(target_price="100.1") != coid(target_price="100.2"))

print("== fields cannot bleed into one another ==")
# Without a separator, ('ab','c') and ('a','bc') are one string. Two
# different slices of one cycle would then share an id.
ok("cycle/slice boundary is real",
   coid(cycle_id="ab", slice_id="c") != coid(cycle_id="a", slice_id="bc"))
ok("product/side boundary is real",
   coid(side="SELL", product_id="LINK-USD")
   != coid(side="SELLLINK", product_id="-USD"))

print("== an incomplete intent refuses, it does not improvise ==")
for field in BASE:
    ok(f"{field}=None yields no id", coid(**{field: None}) is None,
       repr(coid(**{field: None})))
    ok(f"  and no key for {field}",
       oi.intent_key(**dict(BASE, **{field: None})) is None)
for bad in ("abc", float("nan"), float("inf"), float("-inf")):
    ok(f"target_price={bad!r} yields no id", coid(target_price=bad) is None)
    ok(f"quantity={bad!r} yields no id", coid(quantity=bad) is None)
ok("an empty side is not a side", coid(side="") is None)
ok("whitespace alone is not a slice", coid(slice_id="   ") is None)

print("== the prefix survives at the front, unhashed ==")
p = coid(prefix="rstop-")
ok("the prefix is present and leading", p is not None and p[:6] == "rstop-", repr(p))
ok("what follows is the same digest as the unprefixed id",
   p is not None and coid().startswith(p[6:]), repr(p))
ok("a prefixed id is still within the venue's 36",
   p is not None and len(p) <= oi.MAX_LENGTH, str(len(p or "")))
ok("an unprefixed id fills the 36", len(coid()) == oi.MAX_LENGTH, str(len(coid())))
ok("two prefixes do not collide", coid(prefix="rstop-") != coid(prefix="grid-"))
ok("a prefixed id still differs when the intent differs",
   coid(prefix="rstop-") != coid(prefix="rstop-", attempt=1))

print("== a prefix that will not fit refuses rather than being cut ==")
_long = "x" * (oi.MAX_LENGTH - oi.MIN_DIGEST + 1)
ok("an over-long prefix yields no id", coid(prefix=_long) is None, repr(coid(prefix=_long)))
_exact = "x" * (oi.MAX_LENGTH - oi.MIN_DIGEST)
_e = coid(prefix=_exact)
ok("the largest prefix that fits is accepted", _e is not None)
ok("  and leaves the full minimum digest",
   _e is not None and len(_e) - len(_exact) == oi.MIN_DIGEST,
   str(len(_e or "")))
ok("the minimum digest is not a token amount", oi.MIN_DIGEST >= 16,
   str(oi.MIN_DIGEST))

print("== same_intent answers only when both sides are known ==")
ok("identical intents match", oi.same_intent(dict(BASE), dict(BASE)))
ok("a differing attempt does not match",
   not oi.same_intent(dict(BASE), dict(BASE, attempt=1)))
ok("a differing price does not match",
   not oi.same_intent(dict(BASE), dict(BASE, target_price="23.017")))
ok("two spellings of one price do match",
   oi.same_intent(dict(BASE, target_price="23.016"),
                  dict(BASE, target_price="23.0160")))
ok("an incomplete left side is not a match",
   not oi.same_intent(dict(BASE, slice_id=None), dict(BASE)))
ok("an incomplete right side is not a match",
   not oi.same_intent(dict(BASE), dict(BASE, slice_id=None)))
ok("two equally incomplete intents are still not a match",
   not oi.same_intent(dict(BASE, slice_id=None), dict(BASE, slice_id=None)))
ok("a non-dict is not a match", not oi.same_intent(None, dict(BASE)))

print("== nothing here is random or time-dependent ==")
_tree = ast.parse(inspect.getsource(oi))
_imports = {n.name.split(".")[0] for a in ast.walk(_tree)
            if isinstance(a, ast.Import) for n in a.names}
_imports |= {a.module.split(".")[0] for a in ast.walk(_tree)
             if isinstance(a, ast.ImportFrom) and a.module}
ok("it imports no source of variability",
   not (_imports & {"uuid", "random", "time", "datetime", "secrets", "os"}),
   str(_imports))
_loads = {n.id for n in ast.walk(_tree)
          if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
_attrs = {n.attr for n in ast.walk(_tree) if isinstance(n, ast.Attribute)}
ok("and names none of them either",
   not ((_loads | _attrs) & {"uuid4", "uuid1", "random", "monotonic",
                             "urandom", "token_hex", "now"}),
   str(sorted((_loads | _attrs) & {"uuid4", "random", "now"})))
ok("it does no I/O of its own",
   not (_imports & {"aiohttp", "requests", "sqlite3", "sqlalchemy"}),
   str(_imports))
_funcs = {n.name for n in ast.walk(_tree)
          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
ok("it exposes the three the callers need",
   {"intent_key", "client_order_id", "same_intent"} <= _funcs, str(_funcs))

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all order-idempotency checks passed")
