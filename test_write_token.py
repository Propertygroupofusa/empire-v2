"""Thirty-six write buttons on this page sent no token, so all of them 503'd.

write_guard was added for a real reason - an unauthenticated
POST /api/crypto/withdraw would liquidate a position - but no page was ever
updated to SEND the header. Every control on the dashboard has been failing
since, showing a bare "HTTP 503" with nothing saying a token was the missing
piece.

These tests pin the fix, and the two things that make a credential field in
a browser defensible:

  * it does not outlive the tab
  * it is never logged, never in a URL, never sent off-origin
"""
import re
from pathlib import Path

HTML = Path(__file__).with_name("family_tree_dashboard.html").read_text(encoding="utf-8")


def strip_comments(js):
    """Source with // line comments and /* */ blocks removed.

    This check was RED for hours on a comment. The page quotes the
    server's own refusal - "Missing x-dashboard-token header (or
    ?token=). This endpoint changes state." - so a substring search over
    the raw source found "token=" and "?token" in prose explaining why
    neither may appear in a URL, and reported the page as leaking the
    key into a query string.

    A security check that cries wolf on its own documentation is one
    people learn to ignore, which is the more expensive failure here
    than the one it was watching for. It must run against what the page
    can EXECUTE.

    Same stripper as test_dashboard_accuracy.strip_comments, for the
    same reason.
    """
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(re.sub(r"(^|\s)//.*$", "", ln) for ln in js.split("\n"))

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


print("\nEVERY WRITE CARRIES THE TOKEN, BECAUSE THERE IS ONE PLACE TO FORGET IT")

ok("there is a single header builder", "function writeHeaders()" in HTML)
ok("no call site still sends a bare content-type header",
   "headers: { 'Content-Type': 'application/json' }," not in HTML,
   "that is the shape that 503'd on all 36 buttons")
n_posts = HTML.count("method: 'POST'")
n_hdrs = HTML.count("headers: writeHeaders(),")
ok(f"all {n_posts} POSTs use it ({n_hdrs} header sites)", n_hdrs >= n_posts, f"{n_hdrs} vs {n_posts}")
ok("the builder attaches x-dashboard-token", "'x-dashboard-token'" in HTML)
ok("and omits it rather than sending an empty one",
   "if (t) h['x-dashboard-token'] = t;" in HTML)

print("\nTHE CREDENTIAL DOES NOT OUTLIVE THE TAB")

ok("it is held in sessionStorage", "sessionStorage.getItem(TOKEN_KEY)" in HTML)
ok("and NOT in localStorage",
   "localStorage.setItem(TOKEN_KEY" not in HTML and "localStorage.getItem(TOKEN_KEY" not in HTML,
   "a key that moves money should not survive on a phone that is lost")
ok("a blocked storage accessor does not break the page",
   HTML.count("catch (e) { return ''; }") >= 1 and "private mode" in HTML)
ok("there is a way to lock it again", "setWriteToken('')" in HTML)
ok("the input is type=password", 'id="token-input" type="password"' in HTML)
ok("with autocomplete off", 'autocomplete="off"' in HTML)

print("\nIT IS NEVER LOGGED, NEVER IN A URL, NEVER SENT OFF-ORIGIN")

tok_block = HTML[HTML.index("const TOKEN_KEY"):HTML.index("// SELL A DOLLAR AMOUNT")]
ok("the token is never console.logged",
   "console.log" not in tok_block and "console.warn" not in tok_block)
ok("it never goes into a query string",
   "token=" not in strip_comments(tok_block) and "?token" not in strip_comments(HTML))
ok("writes go to same-origin paths only",
   all(u.startswith("'/api/") for u in re.findall(r"postGuarded\((['\"][^'\"]+)", HTML)
       ) if "postGuarded(" in HTML else True)
ok("the page says the token is never stored on the server",
   "never stored on the server" in HTML)

print("\nA MISSING TOKEN AND A WRONG TOKEN SAY DIFFERENT THINGS")

ok("a 503 from the guard explains the deployment has none set",
   "no DASHBOARD_WRITE_TOKEN set at all" in HTML)
ok("and names the Railway restart requirement",
   "only injects" in HTML and "container starts" in HTML,
   "an existing container never picks up a new variable")
# This section's own heading is "A MISSING TOKEN AND A WRONG TOKEN SAY
# DIFFERENT THINGS", and until now it asserted the opposite: one message,
# "The token was refused. Check it matches DASHBOARD_WRITE_TOKEN exactly.",
# served both 401 and 403. They are different faults with different fixes -
# a 401 means the server received NO token, so re-reading the value in
# Railway is the wrong trip entirely. Each now says what happened.
ok("a 401 says the server received no token, not that the value is wrong",
   "received NO token" in HTML and "not the problem" in HTML)
ok("a 403 says a token DID arrive and did not match",
   "A token WAS received" in HTML and "does not match" in HTML)
ok("...and the two messages are genuinely different text",
   HTML.count("received NO token") == 1 and HTML.count("A token WAS received") == 1)
ok("the locked state explains what the token IS",
   "not a link" in HTML and "password you invent" in HTML,
   "the operator asked what link to paste - it is not a link")

print("\nTHE SALE IS TWO TAPS AND THE FIRST PLACES NOTHING")

ok("preview sends confirm: false", "confirm: false" in HTML)
ok("execute sends confirm: true", "confirm: true" in HTML)
ok("the preview says nothing was placed", "Nothing has been placed" in HTML)
# The GUARD, not one spelling of it. This matched the exact text
# "if (!_pendingSale) return;" and so broke the moment the early return
# gained a uiTrace beacon - while the property it names was untouched.
# What matters is that executeSale refuses when there is no pending plan.
_exec_sale = HTML[HTML.index("async function executeSale()"):]
_exec_sale = _exec_sale[:_exec_sale.index("\n}")]
ok("execute is only reachable after a preview",
   re.search(r"if \(!_pendingSale\)\s*\{?[^}]*return;", _exec_sale) is not None,
   "a stale or cancelled plan must not be placeable")
ok("...and the refusal happens before anything is sent",
   _exec_sale.index("_pendingSale") < _exec_sale.index("postGuarded"),
   "the guard must precede the request, not follow it")
ok("cancelling clears the pending plan", "_pendingSale=null;" in HTML)
ok("a successful sale refreshes the page state",
   "refresh();" in HTML and "loadTradingProfile();" in HTML)

print("\nthe panel states the arithmetic, not just the button")

ok("it names ZEC's real share", "25.35%" in HTML)
ok("and why $560 does not clear the rule",
   "$560 lands at 20.36%" in HTML,
   "selling moves coin to cash inside the same account - the denominator barely moves")
ok("and what $650 does", "19.56%" in HTML)
ok("it explains rounding down, and why",
   "rounds DOWN" in HTML and "more of your position than you authorised" in HTML)
ok("both amounts are offered, not just the recommended one",
   "previewSale('ZEC', 650)" in HTML and "previewSale('ZEC', 400)" in HTML)

print("\nthe token bar is on the page and renders at load")

ok("the bar has a home", 'id="token-bar"' in HTML)
ok("and is rendered on load", re.search(r"^renderTokenBar\(\);", HTML, re.M) is not None)
ok("everything it prints is escaped",
   "escText(e.message)" in HTML and "escText(p.note" in HTML)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
