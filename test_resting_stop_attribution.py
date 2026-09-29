"""A resting stop must record which subsystem placed it.

WHY IT EXISTS. /fills-by-source, on its first live read, found 7 TAKER
fills in 24h against an attribution table with ZERO rows - so not one of
those fills could be traced to a caller. Walking the live order paths
explained it:

  * the grid's market path tags itself, but the fleet runs maker-only, so
    that path never executes;
  * auto_trim posts its own order and already records its own source - its
    comment states that a loop bypassing the engine must;
  * resting_stops_worker.place() also bypasses the engine, and recorded
    NOTHING.

It matters most in exactly this place. A stop resting at the venue fills
while this service is asleep, so its fill is the one the account cannot
reconstruct from its own logs - and the unexplained 11,745.30-unit ACH
outflow has that shape. Coinbase fills carry order_id and NOT
client_order_id, so the row written here is the only join key a later
audit has.

WHAT THESE CHECKS PIN. That the row is written, that it is written only on
success, that it uses the id Coinbase mints rather than the one we sent,
and above all that a failure to record it can never cost a stop. An
instrumentation call that can raise inside a protection is worse than no
instrumentation at all.
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


SRC = open("resting_stops_worker.py", encoding="utf-8").read()
TREE = ast.parse(SRC)
fn = next((n for n in ast.walk(TREE)
           if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == "place"), None)
ok("place() exists", fn is not None)
if fn is None:
    print("\nFAILED: cannot inspect place()")
    sys.exit(1)
SEG = ast.get_source_segment(SRC, fn) or ""

print("== the attribution is recorded ==")
ok("place() calls _record_order_source", "_record_order_source" in SEG)
ok("and tags itself as resting_stop", '"resting_stop"' in SEG)
ok("the side recorded is SELL, which is all this worker places",
   '"resting_stop", product_id, "SELL"' in SEG)

print("== it uses the id the fills feed actually carries ==")
# client_order_id is what we SEND and is not returned on a fill. Joining
# on it would produce a tag that never comes back - which is why the
# table is keyed on Coinbase's own id.
ok("the order_id comes from success_response",
   'success_response' in SEG and 'get("order_id")' in SEG)
_call = next((n for n in ast.walk(fn) if isinstance(n, ast.Call)
              and getattr(n.func, "attr", None) == "_record_order_source"), None)
ok("the recorded id is not our client_order_id",
   _call is not None and "client_order_id" not in ast.dump(_call))

print("== it never costs a stop ==")
# The whole point. This is a protection path: an instrumentation call that
# can raise here would turn a missing audit row into a missing stop.
_try = next((n for n in ast.walk(fn) if isinstance(n, ast.Try)
             and any(isinstance(c, ast.Call)
                     and getattr(c.func, "attr", None) == "_record_order_source"
                     for c in ast.walk(n))), None)
ok("the record call is wrapped in try/except", _try is not None)
ok("and the handler catches broadly rather than one named error",
   _try is not None and any(
       h.type is None or (isinstance(h.type, ast.Name) and h.type.id == "Exception")
       for h in _try.handlers))
ok("the handler does not re-raise",
   _try is not None and not any(isinstance(n, ast.Raise) for h in _try.handlers for n in ast.walk(h)))
ok("nor return early, which would change what place() reports",
   _try is not None and not any(isinstance(n, ast.Return) for h in _try.handlers for n in ast.walk(h)))

print("== it only records a real order ==")
# A rejected POST has no order_id. Recording on failure would invent a
# placement that never happened.
_if_ok = next((n for n in ast.walk(fn) if isinstance(n, ast.If)
               and isinstance(n.test, ast.Name) and n.test.id == "ok"
               and any(isinstance(c, ast.Call)
                       and getattr(c.func, "attr", None) == "_record_order_source"
                       for c in ast.walk(n))), None)
ok("the record happens only when the POST succeeded", _if_ok is not None)

print("== the order itself is unchanged ==")
# Instrumentation must not alter what is sent to the venue.
ok("still a SELL", '"side": "SELL"' in SEG)
ok("still carries the worker's own client_order_id prefix",
   "COID_PREFIX" in SEG,
   "free-locked-inventory filters on that prefix to touch only our stops")
ok("the order_configuration still comes from the plan",
   'plan["order_configuration"]' in SEG)
ok("place() still returns (ok, body)",
   any(isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple)
       and len(n.value.elts) == 2 for n in ast.walk(fn)))

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all resting-stop attribution checks passed")
