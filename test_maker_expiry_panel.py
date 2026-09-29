"""The maker-expiry panel must not turn UNKNOWN rows into evidence.

WHY IT EXISTS. /grid-status/maker-expiries and /grid-status/asset-balance
were both written and read by nothing - reachable only by curl. That is
the same defect as a column persisted and never surfaced, one layer up,
and this panel is the reading side.

WHAT THESE CHECKS PIN. The panel carries the distinction that cost four
hours: a row where an order RESTED and went untaken is a different event
from a row where no order was ever created, and `order_rested` is
three-state - true, false, and null meaning UNKNOWN. Folding null into
either side manufactures a fact. On live data right now 337 of ALGO's 417
rows are null, so a panel that summed them would report a liquidity
failure the data cannot support.

It also pins the COST of the balance lookup. Reading every currency was
shipped once (6f68396): get_asset_balance paginates the venue's whole
account list per currency, five coins became five full reads, the key was
rate-limited and the invariant check went UNKNOWN. The lookup here is
deliberately one currency, on demand.

Structural: the panel is JavaScript inside a 519KB HTML file, so what is
asserted is the code, not a rendered DOM. The rendering itself was
exercised separately by running the real functions over the live payloads
in node - eight states, including a 502, a genuinely absent currency and
a direct-read gap.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


HTML = open("family_tree_dashboard.html", encoding="utf-8").read()


def _slice(name):
    """From `async function <name>(` to the NEXT top-level async function.

    Never to a named successor: the sibling test anchored its end at
    renderAccountCensus, two functions were later inserted before it, and
    the slice silently grew to cover code it did not name.
    """
    i = HTML.find("async function %s()" % name)
    if i == -1:
        return ""
    j = HTML.find("\nasync function ", i + 1)
    return HTML[i:j] if j > i else HTML[i:]


# COMMENTS ARE STRIPPED BEFORE ANY CONTENT ASSERTION.
#
# A substring check inside a long function proves almost nothing: it can
# match a `def`/`function` line, and it can match a COMMENT. That last one
# actually happened on the sibling panel - "UNREADABLE" survived in an
# explanatory comment after the rendered branch was deleted, and the
# mutant passed. Raw source is kept only for the syntax check, where the
# comments are part of what must parse.
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


EXP_RAW = _slice("renderMakerExpiries")
BAL_RAW = _slice("lookupAssetBalance")
EXP = _strip_js_comments(EXP_RAW)
BAL = _strip_js_comments(BAL_RAW)

print("== both functions exist and are wired in ==")
ok("renderMakerExpiries is defined", len(EXP) > 500, "len=%d" % len(EXP))
ok("lookupAssetBalance is defined", len(BAL) > 500, "len=%d" % len(BAL))
ok("the container div exists", 'id="maker-expiry-panel"' in HTML)
ok("the panel is actually called on render",
   re.search(r"\n\s*renderMakerExpiries\(\);", HTML) is not None,
   "defined but never invoked is the defect this panel exists to fix")
ok("the lookup is called from the button, not on load",
   "addEventListener('click', () => lookupAssetBalance())" in EXP)
ok("and from Enter in the field", "ev.key === 'Enter'" in EXP)
# The first version guarded only on truthiness and threw inside a headless
# render harness, which stubs getElementById with an object that has no
# addEventListener - taking the WHOLE dashboard script down with it. My own
# harness implemented the method, so it could not have found this: a stub
# built to match the code under test cannot test the code's assumptions.
ok("the listener is only attached when the method actually exists",
   EXP.count("typeof btn.addEventListener === 'function'") == 1
   and EXP.count("typeof inp.addEventListener === 'function'") == 1)

print("== the three states of order_rested stay three ==")
ok("rested is read from its own key", "r.rested" in EXP)
ok("unknown is read from its own key", "r.unknown_whether_rested" in EXP)
ok("legacy no-order rows are read from theirs", "r.no_order_placed" in EXP)
# The whole point. If any two of the three were added together the panel
# would report orders that rested when the rows cannot say that.
_sums = re.findall(r"r\.rested\s*\+\s*\(?r\.(unknown_whether_rested|no_order_placed)", EXP)
ok("rested is never summed with unknown or with legacy no-order rows",
   not _sums, "found %r" % (_sums,))
_sums2 = re.findall(r"r\.unknown_whether_rested[^;]{0,40}\+\s*\(?r\.rested", EXP)
ok("nor the other way round", not _sums2, "found %r" % (_sums2,))
ok("unknown gets its own colour, never the resting one",
   "unknown_whether_rested || 0) > 0 ? '#f59e0b'" in EXP)
ok("a window with no confirmed rest says so in words",
   "Not one row in this window confirms an order actually rested" in EXP)
ok("and that footnote is gated on rested being zero",
   re.search(r"anyRested\s*===\s*0\s*&&\s*anyUnknown\s*>\s*0", EXP) is not None)

print("== a gap is never an empty table ==")
ok("an unreadable fetch is named as a GAP",
   "this is a GAP" in EXP and "not \"no orders expired\"" in EXP)
ok("the unreadable branch checks readable, not just the HTTP status",
   re.search(r"expErr\s*\|\|\s*!exp\s*\|\|\s*!exp\.readable", EXP) is not None)
ok("an EMPTY result is called UNKNOWN, not a pass",
   "UNKNOWN, not a pass" in EXP)
ok("and explains why absence proves nothing",
   "no sell having been attempted" in EXP)

print("== truncation changes what the counts mean ==")
ok("truncation is surfaced", "exp.truncated" in EXP)
# Not a footnote about row count: the summary is aggregated over the
# returned rows only, so every per-coin count becomes a lower bound.
ok("and is described as a FLOOR on every count, not just a short list",
   "FLOOR, not a total" in EXP)
ok("the cap is quoted from the response, not hardcoded", "exp.limit" in EXP)

print("== the balance lookup fails closed ==")
ok("a non-200 is not parsed as a balance",
   re.search(r"if\s*\(!r\.ok\)\s*\{", BAL) is not None)
ok("the endpoint's own detail is shown when it refuses",
   "d.detail" in BAL)
ok("a failed read is UNKNOWN and says it is not a zero",
   "must not be read as a zero balance" in BAL)
ok("the failure branch also catches readable:false",
   re.search(r"err\s*\|\|\s*!d\s*\|\|\s*!d\.readable", BAL) is not None)
# Three outcomes, three colours. A direct-read gap must not be painted
# the same as a confirmed absence, and neither may share with a held
# balance - that collapse is the bug the endpoint itself was built to stop.
ok("a null venue_lists_no_such_account is treated as a GAP",
   re.search(r"venue_lists_no_such_account\s*===\s*null", BAL) is not None)
ok("and undefined too, so a missing key is not read as false",
   "undefined" in BAL and "venue_lists_no_such_account" in BAL)
ok("absent is its own state", "venue_lists_no_such_account === true" in BAL)
ok("the gap colour is not the absent colour",
   re.search(r"gap\s*\?\s*'#f59e0b'\s*:\s*\(absent\s*\?\s*'#ef4444'", BAL) is not None)
# ANCHOR THE GUARD, NOT THE MENTION. Asserting that "d.reads_disagree"
# appears anywhere in the function passed a mutant that replaced the `if`
# with `if (false)` - the key was still referenced inside the dead body,
# so the notice could never render and the check was happy.
ok("a disagreement between the two reads is reported, not resolved",
   re.search(r"if\s*\(d\.reads_disagree\)", BAL) is not None
   and "The two reads disagree" in BAL)
ok("currencies spanning several accounts are surfaced",
   re.search(r"if\s*\(d\.accounts_exceed_currencies\)", BAL) is not None
   and "may under-report" in BAL)

print("== the read cost is bounded and stated ==")
# The regression this pins: a per-currency loop. One fetch, and its
# argument comes from the input field rather than from a list of coins.
_fetches = re.findall(r"fetch\(", BAL)
ok("the lookup issues exactly one fetch", len(_fetches) == 1, "found %d" % len(_fetches))
ok("there is no loop around it in the lookup",
   not re.search(r"\b(for|forEach|map)\b[^;]{0,80}fetch\(", BAL))
ok("the panel itself does not fetch a balance on load",
   "asset-balance" not in EXP.replace("asset-balance-currency", "")
                              .replace("asset-balance-go", "")
                              .replace("asset-balance-result", ""))
ok("the cost is written on the page so nobody re-adds the loop",
   "each lookup reads the whole account list at the venue" in EXP)
ok("the currency is URL-encoded", "encodeURIComponent(cur)" in BAL)
ok("an empty currency is refused before the fetch",
   re.search(r"if\s*\(!cur\)", BAL) is not None)

print("== the panel does not claim more than the rows support ==")
# The title asserted "rested and went untaken" in the first draft, which
# is exactly the conflation the buckets exist to prevent - and on live
# data almost no row can support it.
ok("the heading does not assert that the orders rested",
   "rested and went untaken" not in EXP)
ok("the panel states it is read-only",
   "Read-only" in EXP and "places, cancels or frees an order" in EXP)

print("== the JavaScript parses ==")
node = shutil.which("node") or "/opt/node22/bin/node"
_scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", HTML, re.S)


def _parses(src):
    """node --check needs a real FILE.

    Feeding it /dev/stdin fails with ENOENT on a pipe, and that error was
    briefly read as a syntax failure. A tool that cannot run is a GAP, and
    this returns None for it rather than False - a syntax error here breaks
    the WHOLE dashboard, so "could not check" must never look like "checked".
    """
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(src)
            path = fh.name
        p = subprocess.run([node, "--check", path],
                           capture_output=True, text=True, timeout=120)
        os.unlink(path)
        return p.returncode == 0, (p.stderr or "")[:300]
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


_r, _e = _parses(EXP_RAW + BAL_RAW)
ok("both functions parse", _r is True,
   _e if _r is False else "COULD NOT CHECK (a gap, not a pass): " + _e)
_r2, _e2 = _parses(_scripts[0]) if len(_scripts) == 1 else (None, "no single script block")
ok("the dashboard's whole script block still parses", _r2 is True,
   _e2 if _r2 is False else "COULD NOT CHECK (a gap, not a pass): " + _e2)

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all maker-expiry panel checks passed")
