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
   "token=" not in tok_block and "?token" not in HTML)
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
ok("a 401/403 says the token was refused instead",
   "The token was refused" in HTML)
ok("the locked state explains what the token IS",
   "not a link" in HTML and "password you invent" in HTML,
   "the operator asked what link to paste - it is not a link")

print("\nTHE SALE IS TWO TAPS AND THE FIRST PLACES NOTHING")

ok("preview sends confirm: false", "confirm: false" in HTML)
ok("execute sends confirm: true", "confirm: true" in HTML)
ok("the preview says nothing was placed", "Nothing has been placed" in HTML)
ok("execute is only reachable after a preview",
   "if (!_pendingSale) return;" in HTML,
   "a stale or cancelled plan must not be placeable")
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
