"""
Test that payouts are OFF unless PAYOUTS_ENABLED=true.

WHY THIS EXISTS
---------------
The background payout loop (main.process_payouts_periodically) has never
completed a successful pass in production. Every cycle died at
select(Payment), because that emits every mapped column and
payments.stripe_payout_id did not exist - the "Payout cycle error"
repeating every 30 seconds in Railway. The failure landed BEFORE
stripe.Transfer.create, so no money ever moved.

That means a broken schema was the only thing holding the loop back, and
repairing the migration takes it away. What it would be armed into:

  - session.commit() sits OUTSIDE the per-payment loop, so one failed
    commit loses the "paid" status of every transfer in that pass while
    the transfers themselves are real and final
  - Transfer.create carries no idempotency key
  - the rows stay "pending", so 30 seconds later the same payments are
    transferred again, unbounded

A redeploy mid-loop is enough to trigger it. So the flag has to default to
off, and it has to short-circuit BEFORE any Stripe call - a gate that
merely logs, or that sits after the transfer, is not a gate.

WHAT IS ASSERTED
----------------
Both money paths, because they reach Stripe by different calls:

  main.process_payouts_periodically   stripe.Transfer.create
  routers/payments.process_pending_payouts  stripe.Payout.create

Static analysis rather than execution: importing either module offline
needs fastapi/stripe/asyncpg and a live database. The property that
matters is structural - does the flag check dominate every Stripe call in
the function - and the AST answers that directly, without a mock stack
deep enough to be its own source of bugs.
"""
import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
FLAG = "PAYOUTS_ENABLED"


def find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def stripe_calls(node):
    """(lineno, dotted name) for every stripe.X.Y(...) call in the subtree."""
    out = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        parts, cur = [], n.func
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name) and cur.id == "stripe":
            out.append((n.lineno, "stripe." + ".".join(reversed(parts))))
    return out


def guard_returns_before(node, lineno_floor):
    """True if some `if <FLAG-ish>: ... return` completes before lineno_floor.

    Requires an actual return inside the guard body - a check that only
    logs would leave execution falling through to the Stripe call.
    """
    for n in ast.walk(node):
        if not isinstance(n, ast.If):
            continue
        src = ast.dump(n.test)
        if FLAG not in src:
            continue
        if not any(isinstance(b, ast.Return) for b in ast.walk(n)):
            continue
        if max(getattr(b, "lineno", 0) for b in n.body) < lineno_floor:
            return True
    return False


def check(path, funcname, failures):
    tree = ast.parse((REPO / path).read_text())
    fn = find_func(tree, funcname)
    if fn is None:
        failures.append(f"{path}: function {funcname} not found")
        return

    calls = stripe_calls(fn)
    money = [(ln, nm) for ln, nm in calls
             if nm.endswith((".create", ".Payout.create", ".Transfer.create"))]
    if not money:
        failures.append(f"{path}:{funcname}: expected a money-moving stripe "
                        f"call; found {calls or 'none'} - has the code moved?")
        return

    print(f"  {path}:{funcname}")
    for ln, nm in money:
        print(f"      line {ln}: {nm}")

    first = min(ln for ln, _ in money)
    if guard_returns_before(fn, first):
        print(f"      guarded: {FLAG} check returns before line {first}\n")
    else:
        failures.append(f"{path}:{funcname}: no {FLAG} guard returns before "
                        f"the Stripe call at line {first}")


def check_default_off(path, failures):
    """The flag must default to off. `getenv(FLAG)` with a truthy default,
    or no default at all, would arm payouts on a fresh environment."""
    src = (REPO / path).read_text()
    tree = ast.parse(src)
    seen = False
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "getenv" and n.args
                and isinstance(n.args[0], ast.Constant) and n.args[0].value == FLAG):
            seen = True
            if len(n.args) < 2:
                failures.append(f"{path}:{n.lineno}: getenv({FLAG}) has no default "
                                f"- absent env var must mean OFF")
            elif str(n.args[1].value).strip().lower() == "true":
                failures.append(f"{path}:{n.lineno}: getenv({FLAG}) defaults to "
                                f"{n.args[1].value!r} - payouts would be ON by default")
    if not seen:
        failures.append(f"{path}: no getenv({FLAG}) found")


def main():
    failures = []
    print(f"Asserting every Stripe money call is behind {FLAG}\n")

    check("main.py", "process_payouts_periodically", failures)
    check("routers/payments.py", "process_pending_payouts", failures)

    for p in ("main.py", "routers/payments.py"):
        check_default_off(p, failures)
    print(f"  {FLAG} defaults to OFF in both modules")

    print()
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  PASS  payouts cannot fire unless explicitly armed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
