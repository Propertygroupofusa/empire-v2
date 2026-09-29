"""The account-wide exposure gate must actually be reached.

WHY THIS FILE EXISTS. can_open_position has accepted account_positions and
equity since the six identical META orders of 28 Sep - $734.57, 73% of
equity, in an account market_brain measured as 0% exposed because its own
book was empty. Both parameters default to None, and the ONE live call site
passed neither:

    if not can_open_position(state.positions, alloc):

so every real cycle silently took the self-only fallback, which that
function's own docstring calls "the wrong denominator ... and always was".
account_exposure.can_open existed, was imported, and was never reached from
a live path. The guard was written, documented with the incident that
motivated it, and not connected - the same shape as GridMakerExpiry's
unread rows and CryptoGridTradeHistory's dropped columns.

AND IT MUST FAIL CLOSED. An unreadable account is not an empty one.
api_call returns None on failure, and flattening that to an empty list
would let a concentration limit permit the exact position it exists to
prevent. Entries are refused for the cycle; exits are untouched, because a
protection must still work when a read fails.

Behavioural where the logic is pure (account_exposure.can_open), structural
for the wiring, since "which arguments does this call site pass" is a
question about the code and not about a return value.
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


SRC = open("market_brain.py", encoding="utf-8").read()
TREE = ast.parse(SRC)


def fn(name):
    for n in ast.walk(TREE):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


print("== the gate is reached with account-level inputs ==")

calls = [n for n in ast.walk(TREE)
         if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
         and n.func.id == "can_open_position"]
ok("can_open_position is called", bool(calls), f"found {len(calls)}")

# EVERY call site, not "at least one" - a second site added later that omits
# the account inputs puts the original blindness straight back.
def _has(c, name):
    return any(k.arg == name for k in c.keywords)

ok("EVERY call site passes account_positions",
   bool(calls) and all(_has(c, "account_positions") for c in calls),
   "a site that omits it silently takes the self-only fallback, which is "
   "the wrong denominator and the reason six META orders got through")
ok("EVERY call site passes equity",
   bool(calls) and all(_has(c, "equity") for c in calls))

# The inputs must not be literals or this proves nothing.
for c in calls:
    vals = {k.arg: k.value for k in c.keywords}
    for a in ("account_positions", "equity"):
        v = vals.get(a)
        ok(f"{a} is a real value, not a constant",
           v is not None and not isinstance(v, ast.Constant),
           f"passing a literal would satisfy the check above while "
           f"measuring nothing")


print("== unreadable is distinguished from empty ==")

gp = fn("get_account_positions")
ok("get_account_positions exists", gp is not None)
if gp is not None:
    seg = ast.get_source_segment(SRC, gp) or ""
    ok("it returns None when the read fails, never an empty list",
       "None" in seg and "[]" not in seg,
       "flattening a failed read to [] makes 'could not read' and 'nothing "
       "held' the same value, and one of them permits a buy")

inp = fn("account_exposure_inputs")
ok("account_exposure_inputs exists", inp is not None)
if inp is not None:
    rets = [r for r in ast.walk(inp) if isinstance(r, ast.Return)]
    # Every failure return must be a pair of Nones, so the caller cannot
    # mistake a partial read for a usable one.
    nones = [r for r in rets
             if isinstance(r.value, ast.Tuple)
             and all(isinstance(e, ast.Constant) and e.value is None
                     for e in r.value.elts)]
    ok("every failure path returns (None, None)", len(nones) >= 3,
       f"found {len(nones)} - an equity that parsed but a position list "
       f"that did not must not come back as usable")
    seg = ast.get_source_segment(SRC, inp) or ""
    ok("a NaN or non-positive equity is treated as unreadable",
       "eq != eq" in seg and "<= 0" in seg,
       "equity is the denominator; a NaN or zero there makes every "
       "exposure fraction meaningless or infinite")


print("== it fails CLOSED ==")

cycle = None
for n in ast.walk(TREE):
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        seg = ast.get_source_segment(SRC, n) or ""
        if "can_open_position(" in seg and "scan_parallel(" in seg:
            cycle = n
ok("the entry loop was located", cycle is not None)

if cycle is not None:
    seg = ast.get_source_segment(SRC, cycle) or ""
    ok("the cycle reads the account inputs once, not per candidate",
       seg.count("account_exposure_inputs()") == 1,
       "asking the account N times per pass is N API calls for an answer "
       "that cannot change between two entries in the same pass")

    sub = ast.parse(seg.strip())
    # A break guarded by the readability flag, inside the candidate loop.
    guarded_break = False
    for n in ast.walk(sub):
        if isinstance(n, ast.If) and "_exposure_readable" in ast.dump(n.test):
            if any(isinstance(x, ast.Break) for x in ast.walk(n)):
                guarded_break = True
    ok("an unreadable account STOPS every new entry this cycle",
       guarded_break,
       "this is the fail-closed direction and the only one that matters: "
       "an unknown exposure must never be read as a zero")

    # SCOPED TO THE REFUSAL ITSELF. `"log.error" in seg` passed a mutant
    # that downgraded this exact line to log.debug, because the cycle
    # contains other log.error calls - a substring check on a long function
    # proves only that SOME line logs at error.
    _refusals = []
    for n in ast.walk(sub):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            txt = " ".join(a.value for a in ast.walk(n)
                           if isinstance(a, ast.Constant)
                           and isinstance(a.value, str))
            if "REFUSING every new entry" in txt:
                _refusals.append(n.func.attr)
    ok("the refusal itself is logged", bool(_refusals),
       "no call carries the refusal message at all")
    ok("and it is logged at ERROR, not downgraded",
       bool(_refusals) and all(a == "error" for a in _refusals),
       f"logged at {_refusals} - a refusal nobody can see is "
       f"indistinguishable from a cycle that simply found no signals")


print("== the underlying gate still does its job ==")

import account_exposure

# Readable and already concentrated -> refuse.
pos = [{"symbol": "META", "market_value": "734.57"}]
allowed, reason, projected = account_exposure.can_open(pos, 1000.0, 100.0, 0.60)
ok("an account already at 73% refuses a further entry", allowed is False,
   f"allowed={allowed} reason={reason} projected={projected}")

# Readable and empty -> permit. This is the case that must NOT be confused
# with unreadable.
allowed2, _, _ = account_exposure.can_open([], 1000.0, 100.0, 0.60)
ok("a genuinely empty account still permits an entry", allowed2 is True,
   "failing closed on a real zero would stop the bot trading at all")

ok("the two are different answers",
   allowed is not allowed2,
   "if an empty account and a concentrated one agreed, the gate would be "
   "measuring nothing")


print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all account-exposure wiring checks passed")
