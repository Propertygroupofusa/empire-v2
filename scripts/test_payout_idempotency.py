"""
Test that a payout cannot be sent twice.

WHY THIS EXISTS
---------------
The payout paths had a duplicate-payment failure mode. Money moved first,
the database was updated second, and nothing tied the two together:

  - main.process_payouts_periodically committed ONCE, after the whole
    loop, so a single failed commit discarded the "paid" status of every
    transfer in that pass while the transfers themselves stayed real
  - neither path passed an idempotency key, so replaying a call created a
    genuinely new transfer rather than returning the original
  - the rows stayed "pending", so 30 seconds later the same payments were
    sent again - unbounded, and a redeploy mid-loop was enough to start it

Three independent defences now, because any one of them can be defeated:

  1. idempotency_key derived from payment.id - a replay returns the
     ORIGINAL Stripe object instead of creating a second one
  2. commit immediately after each transfer - bounds the exposure to one
     payment instead of the whole batch
  3. the query skips rows that already carry a Stripe id - the backstop
     for Stripe's 24h key expiry, after which a stale key is treated as
     new and defence 1 silently stops working

WHAT IS ASSERTED, AND WHY STATICALLY
------------------------------------
Importing either module offline needs fastapi, stripe, asyncpg and a live
database. Every property here is structural - does the call carry a key,
does a commit occur inside the loop body, does the query filter on the id
column - and the AST answers all three directly. A mock Stripe deep enough
to answer them by execution would be a larger source of bugs than the code
it checks.
"""
import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent

TARGETS = [
    # path, function, stripe call, the column proving payment already went out
    ("main.py", "process_payouts_periodically", "Transfer", "stripe_transfer_id"),
    ("routers/payments.py", "process_pending_payouts", "Payout", "stripe_payout_id"),
]


def find_func(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def money_calls(node, kind):
    """Every stripe.<kind>.create(...) in the subtree."""
    out = []
    for n in ast.walk(node):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "create"
                and isinstance(n.func.value, ast.Attribute)
                and n.func.value.attr == kind):
            out.append(n)
    return out


def enclosing_loop(fn, node):
    """The innermost for-loop containing `node`, if any."""
    best = None
    for n in ast.walk(fn):
        if not isinstance(n, ast.For):
            continue
        lines = [getattr(c, "lineno", -1) for c in ast.walk(n)]
        if min(l for l in lines if l > 0) <= node.lineno <= max(lines):
            if best is None or n.lineno > best.lineno:
                best = n
    return best


def has_commit_between(loop, start_line):
    """A commit inside the loop body, at or after the Stripe call."""
    for n in ast.walk(loop):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "commit" and n.lineno >= start_line):
            return n.lineno
    return None


def filters_on(fn, column):
    """Any `<X>.<column>.is_(None)` appearing in the function."""
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "is_"
                and isinstance(n.func.value, ast.Attribute)
                and n.func.value.attr == column):
            return n.lineno
    return None


def main():
    failures = []
    keys = {}
    print("Asserting payouts cannot be sent twice\n")

    for path, funcname, kind, id_col in TARGETS:
        tree = ast.parse((REPO / path).read_text())
        fn = find_func(tree, funcname)
        if fn is None:
            failures.append(f"{path}: {funcname} not found")
            continue

        calls = money_calls(fn, kind)
        if len(calls) != 1:
            failures.append(f"{path}:{funcname}: expected exactly one "
                            f"stripe.{kind}.create, found {len(calls)}")
            continue
        call = calls[0]
        print(f"  {path}:{funcname}  ->  stripe.{kind}.create at line {call.lineno}")

        # 1. idempotency key, derived from the payment
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        if "idempotency_key" not in kw:
            failures.append(f"{path}:{funcname}: stripe.{kind}.create has no "
                            f"idempotency_key - a replay creates a second payment")
        else:
            node = kw["idempotency_key"]
            src = ast.dump(node)
            if "payment" not in src or "id" not in src:
                failures.append(f"{path}:{funcname}: idempotency_key is not derived "
                                f"from payment.id, so it cannot be stable per payment")
            else:
                literal = "".join(
                    v.value for v in getattr(node, "values", [])
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                ) or "<dynamic>"
                keys[funcname] = literal
                print(f"      idempotency_key prefix: {literal!r}")

        # 2. commit inside the loop, after the transfer
        loop = enclosing_loop(fn, call)
        if loop is None:
            failures.append(f"{path}:{funcname}: stripe call is not inside a for-loop; "
                            f"this test's assumptions no longer hold")
        else:
            line = has_commit_between(loop, call.lineno)
            if line is None:
                failures.append(f"{path}:{funcname}: no commit inside the loop after "
                                f"line {call.lineno} - one failure discards the whole batch")
            else:
                print(f"      per-payment commit at line {line}")

        # 3. already-paid rows excluded from the candidate query
        line = filters_on(fn, id_col)
        if line is None:
            failures.append(f"{path}:{funcname}: query does not exclude rows where "
                            f"{id_col} is set - Stripe keys expire after 24h and "
                            f"an already-paid row would be re-sent")
        else:
            print(f"      {id_col} IS NULL filter at line {line}\n")

    # The two paths are different Stripe operations on the same payment.
    # A shared key would make whichever ran second silently return the
    # other's object instead of doing its own work.
    if len(keys) == 2 and len(set(keys.values())) == 1:
        failures.append(f"both paths use the same idempotency_key prefix "
                        f"{next(iter(keys.values()))!r}; Transfer and Payout are "
                        f"distinct operations and must not collide")
    elif len(keys) == 2:
        print(f"  key namespaces are distinct: {sorted(keys.values())}")

    print()
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  PASS  idempotency key + per-payment commit + already-paid filter")
    return 0


if __name__ == "__main__":
    sys.exit(main())
