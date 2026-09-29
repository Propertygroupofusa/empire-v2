"""The execution-and-inventory panel must never render an all-clear it cannot support.

WHY IT EXISTS. /grid-status/invariants was reporting FAIL with $639.28 of
coin claimed by branches and not held, across nine positions, and the
owner's dashboard did not mention it anywhere. The three endpoints that
explain WHY a branch cannot sell - orders-not-placed, asset-balance,
maker-expiries - were reachable only by curl. A check nobody can see
protects nothing, which is the same defect as a column written and never
read, one layer up.

WHAT THESE CHECKS PIN. The panel's job is to be honest under failure, and
a dashboard fails differently from a script: a blank space beside a green
page reads as "nothing wrong". So an unreadable fetch must SAY so, a null
available must not be drawn as a zero, and the unreadable footnote on the
invariant must never be dropped.

Structural: the panel is JavaScript inside a 519KB HTML file, so what is
asserted is the code, not a rendered DOM. The rendering itself was
exercised separately by running the real function over the live payloads
in node.
"""
import re
import sys

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


HTML = open("family_tree_dashboard.html", encoding="utf-8").read()

# SLICE TO THE NEXT TOP-LEVEL FUNCTION, NOT TO A NAMED ONE.
#
# This used to end the slice at renderAccountCensus, which was the next
# function in the file ON THE DAY IT WAS WRITTEN. A later pass inserted two
# functions between them, and the slice silently grew to cover all three -
# so every content assertion below could have been satisfied by unrelated
# code. Same defect as anchoring a mutant to a guessed line: the anchor
# stopped matching what it named and nothing said so.
i = HTML.find("async function renderExecutionInventory()")
j = HTML.find("\nasync function ", i + 1) if i != -1 else -1
FN_RAW = HTML[i:j] if (i != -1 and j != -1 and j > i) else ""

# COMMENTS ARE STRIPPED BEFORE ANY CONTENT ASSERTION.
#
# The first version of this file matched "UNREADABLE" against the raw
# source - and the word also appears in an explanatory comment, so a mutant
# that deleted the RENDERED string sailed through. This repo has been
# burned by matching comment text before; in Python the answer is to parse
# the AST, and for JavaScript embedded in HTML the cheap equivalent is to
# remove the comments first. FN_RAW is kept only for the syntax check,
# where comments are part of what must parse.
def _strip_js_comments(src):
    out, i, n = [], 0, len(src)
    while i < n:
        two = src[i:i + 2]
        if two == "//":
            j = src.find("\n", i)
            i = n if j == -1 else j
        elif two == "/*":
            j = src.find("*/", i + 2)
            i = n if j == -1 else j + 2
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


FN = _strip_js_comments(FN_RAW)

print("== the panel exists and is wired in ==")

ok("the render function is defined", bool(FN_RAW))
ok("it has a container on the page",
   'id="execution-inventory-panel"' in HTML)
ok("and it is CALLED on the render cycle",
   "renderExecutionInventory();" in HTML,
   "a function nobody calls is the same as no function")

print("== it reads both sources ==")

ok("it reads the invariant check",
   "grid-status/invariants" in FN,
   "this is the only thing that answers 'is the fleet claiming coin it "
   "does not have?'")
ok("it reads the blocked-order ledger",
   "grid-status/orders-not-placed" in FN,
   "this is the only thing that answers 'why could that branch not sell?'")
ok("it names the check it wants rather than taking checks[0]",
   "coin_tracked_is_held" in FN,
   "positional access would silently follow a reordering of the payload")

print("== an unreadable fetch is never an all-clear ==")

# Both failing must produce a VISIBLE statement, not an empty panel.
ok("both-unreadable renders the word UNREADABLE",
   re.search(r"UNREADABLE", FN) is not None)
ok("and it says it is a GAP, not an all-clear",
   "not an all-clear" in FN,
   "a blank panel beside a green page reads as 'nothing wrong'")

# Each half has to fail independently - one source down must not blank both.
ok("the invariant half has its own failure branch",
   "invErr" in FN and FN.count("invErr") >= 3)
ok("the blocked half has its own failure branch",
   "blkErr" in FN and FN.count("blkErr") >= 3)

ok("an empty blocked list is explained, not left to imply success",
   "never attempts" in FN,
   "a branch below its own sell trigger never attempts, so it never "
   "appears - absence is not proof of selling")

print("== UNKNOWN is drawn as UNKNOWN ==")

# A null available must not fall into the locked or dust bucket.
ok("a null available renders UNKNOWN",
   re.search(r"av === null", FN) is not None
   and re.search(r"UNKNOWN &mdash; the balance could not be read", FN) is not None,
   "drawing it as 0 would label a gap as 'nothing free at all'")
ok("the UNKNOWN case is checked BEFORE the zero case",
   FN.index("av === null") < FN.index("av === 0"),
   "null == 0 is false in JS, but ordering the null test first makes the "
   "intent unmistakable and survives a later loosening to ==")
ok("a confirmed zero renders LOCKED",
   "LOCKED &mdash; nothing free at all" in FN)

ok("a non-OK, non-FAIL invariant status is NOT drawn as clear",
   "check.status !== 'FAIL'" in FN and "check.status === 'OK'" in FN,
   "UNKNOWN is a third verdict and must not share a branch with OK")

print("== the footnote and the cap are not dropped ==")

ok("the invariant's unreadable list is surfaced",
   "if (check.unreadable && check.unreadable.length)" in FN,
   # The guard itself, not the field name: a mutant that replaced the
   # condition with `if (false)` left `check.unreadable` sitting in the
   # dead block and passed a looser check.
   "those are the coins that could not be confirmed either way, and on "
   "this fleet they once held the three LARGEST shortfalls")
ok("and it says UNKNOWN, not zero", "UNKNOWN, not zero" in FN)
ok("a truncated blocked list says so",
   "if (blocked.truncated)" in FN and "partial view" in FN,
   "a capped list read as complete is how the 50-row trade-history "
   "window decided what the data appeared to say")

print("== it stays read-only ==")

for verb in ("free-locked-inventory", "POST", "method:", "x-dashboard-token"):
    ok(f"the panel never references '{verb}'", verb not in FN,
       "this panel reports; moving money is a deliberate call elsewhere")
ok("and it says so on the page", "Read-only" in FN)

print("== the JavaScript parses ==")

import shutil
import subprocess
import tempfile

node = shutil.which("node")
if not node:
    ok("node is available to parse the panel", False,
       "cannot verify syntax without it - a syntax error here breaks the "
       "WHOLE dashboard, so this is not an optional check")
else:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(FN_RAW)
        path = fh.name
    r = subprocess.run([node, "--check", path], capture_output=True, text=True)
    ok("the panel's JavaScript parses", r.returncode == 0,
       (r.stderr or "")[:300])

    # And the whole script block, since a stray brace breaks everything.
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", HTML, re.S)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write("\n;\n".join(blocks))
        allpath = fh.name
    r2 = subprocess.run([node, "--check", allpath], capture_output=True, text=True)
    ok("and the dashboard's whole script block still parses",
       r2.returncode == 0, (r2.stderr or "")[:300])

print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all execution-inventory panel checks passed")
