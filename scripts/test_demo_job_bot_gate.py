"""
Test that the demo job bot is off unless explicitly armed.

WHY THIS EXISTS
---------------
start_job_bot claims jobs, marks them complete two seconds later, and
books a payment. It selects on Job.status == "requested" with NO filter
on `paid`, and then sets job.paid = True itself.

The real notarization flow depends on that flag. routers/jobs.py opens a
job with paid=False and routes the client to Stripe checkout; notary_bot
and POST /{job_id}/match both refuse to match an unpaid job. This loop
bypasses that gate and overwrites the flag.

Concretely: a real customer submits a notarization request and, within 10
seconds and before entering a card, the bot claims it, stamps it paid,
marks it completed, and books a payout obligation for the full price. No
work performed, no money collected.

It was inert only because two other bugs kept it so - no bot workers
(passlib, #129) and no jobs. #129 and #131 removed the first protection,
so the demo needs a deliberate one.

WHAT IS ASSERTED
----------------
  1. the flag defaults to OFF - an absent or non-"true" value must not
     arm it, since a fresh environment must never auto-complete jobs
  2. the guard RETURNS before any job is claimed. A guard that only logs
     leaves execution falling through to exactly the behaviour it exists
     to prevent, so the check has to dominate the claim, not precede it
     decoratively.
  3. the claim really is what we think it is - the loop still mutates
     job.paid, so if someone later adds a paid filter and this test's
     premise goes stale, it says so instead of passing quietly

Static analysis: importing main.py needs fastapi/uvicorn/alpaca and a
live database, and the property here is structural.
"""
import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
FLAG = "DEMO_JOB_BOT_ENABLED"


def main():
    failures = []
    src = (REPO / "main.py").read_text()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "start_job_bot"), None)
    if fn is None:
        print("  FAIL  start_job_bot not found")
        return 1

    # ── 1. defaults to off ─────────────────────────────────────────────
    getenvs = [n for n in ast.walk(fn)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "getenv" and n.args
               and isinstance(n.args[0], ast.Constant) and n.args[0].value == FLAG]
    if not getenvs:
        failures.append(f"start_job_bot never reads {FLAG}")
    for g in getenvs:
        if len(g.args) < 2:
            failures.append(f"main.py:{g.lineno}: getenv({FLAG}) has no default - "
                            f"an unset variable must mean OFF")
        elif str(g.args[1].value).strip().lower() == "true":
            failures.append(f"main.py:{g.lineno}: getenv({FLAG}) defaults to "
                            f"{g.args[1].value!r} - the demo would be ON by default")
    if getenvs and not failures:
        print(f"  {FLAG} is read and defaults to OFF")

    # ── 3. locate the claim (also validates this test's premise) ───────
    claim_lines = []
    mutates_paid = False
    for n in ast.walk(fn):
        if isinstance(n, ast.Attribute) and n.attr == "paid" and \
                isinstance(n.value, ast.Name) and n.value.id == "job":
            mutates_paid = True
            claim_lines.append(n.lineno)
        if isinstance(n, ast.Attribute) and n.attr == "status" and \
                isinstance(n.value, ast.Name) and n.value.id == "job":
            claim_lines.append(n.lineno)
    if not mutates_paid:
        failures.append("start_job_bot no longer assigns job.paid - if the loop "
                        "now respects payment, re-examine whether this gate is "
                        "still the right fix (this test's premise is stale)")
    if not claim_lines:
        failures.append("could not locate the job claim; assumptions stale")
        print()
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    first_claim = min(claim_lines)
    print(f"  claim mutates job.status/job.paid from line {first_claim}")

    # ── 2. the guard must RETURN before the claim ──────────────────────
    guarded = False
    for n in ast.walk(fn):
        if not isinstance(n, ast.If):
            continue
        if FLAG not in ast.dump(n.test):
            continue
        returns = [b for b in ast.walk(n) if isinstance(b, ast.Return)]
        if not returns:
            failures.append(f"main.py:{n.lineno}: the {FLAG} check does not "
                            f"return - execution falls through to the claim")
            continue
        last = max(getattr(b, "lineno", 0) for b in n.body)
        if last < first_claim:
            guarded = True
        else:
            failures.append(f"main.py:{n.lineno}: the {FLAG} guard does not "
                            f"complete before the claim at line {first_claim}")
    if not guarded and not failures:
        failures.append(f"no {FLAG} guard returns before the job claim")
    elif guarded:
        print(f"  guard returns before line {first_claim}")

    print()
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  PASS  the demo job bot cannot claim or complete jobs unless armed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
