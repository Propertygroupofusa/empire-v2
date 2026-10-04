"""A success line that fires when nothing succeeded hides the real fault.

WHAT HAPPENED

2026-10-04 07:00 ET, three consecutive lines in the production log:

    [ERROR]   Claude call failed generating daily brief: 404 - not_found_error
    [WARNING] Daily brief NOT delivered (sendgrid 401, smtp filtered)
    [INFO]    Daily brief generated, persisted, and sent

The third line contradicts the two above it. It had been printing every
morning for months while the brief produced no summary and delivered no
email, because:

  * _generate_summary caught the API error and returned the PLACEHOLDER
    STRING "(Summary generation failed...)". The caller could not tell that
    from a real summary - both are just str.
  * _send_brief_email logged its own failure but returned None, so the
    caller had nothing to check.
  * The final log.info claimed all three of generated / persisted / sent
    without testing any of them.

The model id was the proximate cause and is fixed elsewhere. THIS is why it
survived: anyone skimming the log for "did the brief run?" found a success
line and stopped looking. A retired id is a day's bug; a log that lies
about it is a months-long one.

WHAT THIS GUARDS

Only the persist is unconditionally true (the DB row is written before
either other step), so only the persist may be claimed without checking.
"""
import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent
SRC = (REPO / "daily_brief.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)

checks = []


def ok(label, cond, detail=""):
    checks.append((label if not detail else f"{label}  -- {detail}", bool(cond)))


def func(name):
    # generate_and_send_brief is an `async def`, which is AsyncFunctionDef
    # and NOT FunctionDef. Matching only the latter returned None, and
    # ast.get_source_segment(SRC, None) gives an empty body - so every
    # assertion about the caller passed vacuously. A test that cannot see
    # the code it checks is worse than no test.
    for n in ast.walk(TREE):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


print("\n[1] the summary helper reports whether it really generated")
gen = func("_generate_summary")
ok("_generate_summary exists", gen is not None)
returns = [n for n in ast.walk(gen) if isinstance(n, ast.Return)] if gen else []
ok("every return is a 2-tuple (text, generated), not a bare string",
   returns and all(isinstance(r.value, ast.Tuple) and len(r.value.elts) == 2
                   for r in returns),
   f"{len(returns)} return(s)")
flags = []
for r in returns:
    second = r.value.elts[1]
    flags.append(second.value if isinstance(second, ast.Constant) else None)
ok("...and the flag is a real True/False on each path",
   set(flags) == {True, False}, str(flags))

print("\n[2] the email helper reports whether it landed")
snd = func("_send_brief_email")
ok("_send_brief_email exists", snd is not None)
snd_returns = [n for n in ast.walk(snd) if isinstance(n, ast.Return)] if snd else []
ok("it returns a value rather than None", bool(snd_returns))

print("\n[3] THE CALLER STOPS CLAIMING WHAT IT DID NOT CHECK")
caller = func("generate_and_send_brief")
body = ast.get_source_segment(SRC, caller) or ""
ok("the caller's source was actually located (not a vacuous pass)",
   len(body) > 200, f"{len(body)} chars")
ok("it captures the generated flag", "summary, summary_ok =" in body)
ok("it captures the delivery result", "sent_ok = _send_brief_email" in body)
# Check the CODE, not the comments. The fix deliberately quotes the old
# line in a comment so the next reader knows what went wrong; a substring
# search over the whole body would flag that documentation as the bug.
# Walk the log calls instead and look at what is actually being logged.
_log_literals = []
for _n in ast.walk(caller):
    if (isinstance(_n, ast.Call) and isinstance(_n.func, ast.Attribute)
            and _n.func.attr in ("info", "warning", "error")):
        for _a in _n.args:
            if isinstance(_a, ast.Constant) and isinstance(_a.value, str):
                _log_literals.append(_a.value)
ok("the old unconditional success line is GONE from the log calls",
   not any("generated, persisted, and sent" in t for t in _log_literals),
   str(_log_literals)[:120])
ok("...and a log call does exist to replace it", bool(_log_literals))
ok("the summary outcome is branched on, not assumed", "summary_ok" in body)
ok("the delivery outcome is branched on, not assumed", "sent_ok" in body)
ok("a failure is still reported as a failure, in words",
   "FAILED" in body)

print("\n[4] the brief still goes out when the summary fails")
# Graceful degradation is correct and must not be 'fixed' into a hard stop:
# raw numbers are worth sending even with no prose.
ok("the email is still sent regardless of the summary outcome",
   body.index("_send_brief_email") > body.index("summary_ok ="),
   "send must not be gated on the summary succeeding")
ok("the DB row is still written before either step",
   body.index("db.add(DailyBrief") < body.index("_send_brief_email"))

print("\n[5] no retired model id is hardcoded here any more")
ok("the model comes from the shared constant", "TEXT_MODEL" in SRC)
ok("and no literal model id is left in this file",
   '"claude-3-5-sonnet' not in SRC and "'claude-3-5-sonnet" not in SRC)

width = max(len(l) for l, _ in checks)
print()
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
