"""The sports panel on the Live Trading Dashboard reads, and nothing else.

The owner, 2026-10-15: "Add this to my dashboard where I'll be able to see
it on the dashboard with the crypto and alpaca." The panel shows the
Delfina sports board (who is likely to win each game, and how often the
favorites did). Delfina is a separate service with its own code; this page
only GETs its public /api/board. The panel must not be able to send
anything, must not reach the Alpaca or Coinbase endpoints, and must escape
every name it prints, since those come from another service.

Run: python3 test_sports_panel.py
"""
import re
from pathlib import Path

HTML = Path(__file__).with_name("trading_dashboard.html").read_text(encoding="utf-8")

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


def strip_comments(js):
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(re.sub(r"(^|\s)//.*$", "", ln) for ln in js.split("\n"))


print("\nTHE PANEL IS ON THE PAGE, ONCE")
ok("one sports panel", HTML.count('id="sportsPanel"') == 1)
ok("it says who's likely to win", "Sports: Who's Likely to Win" in HTML)
ok("it says it places no bets and touches neither account",
   "places no" in HTML and "nothing in this panel touches" in HTML)

start = HTML.index("// ---- Sports board (Delfina)")
end = HTML.index("setInterval(refreshSports", start)
block = HTML[start:end]
code = strip_comments(block)

print("\nIT ONLY READS, AND ONLY FROM THE SPORTS BOARD")
ok("exactly one network call in the panel", code.count("fetch(") == 1)
ok("that call is the board's read-only address", "fetch(base + '/api/board'" in code)
ok("it sends nothing: no method, no body",
   all(w not in code for w in ("method:", "body:", "POST", "PUT", "DELETE")))
ok("it uses no dashboard write path", "writeHeaders" not in code and "apiFetch(" not in code)
ok("it reaches neither trading account",
   all(w not in code.lower() for w in ("/api/trading-dashboard", "alpaca", "coinbase", "/api/crypto")))
url = re.search(r"const SPORTS_BOARD_URL = '([^']*)';", code)
ok("the board address is empty or https", url is not None and (url.group(1) == "" or url.group(1).startswith("https://")),
   url.group(1) if url else "missing")

print("\nIT ESCAPES WHAT ANOTHER SERVICE SENDS")
ok("there is an escape helper", "function sportsEsc(" in code)
for field in ("team", "g.sport", "g.price", "g.favorite", "g.better_price.site", "sportsWhen(g.start)"):
    ok(f"{field} is escaped before it is drawn", f"sportsEsc({field})" in code)
ok("numbers are formatted, not pasted", "Number(g.wins_on_100).toFixed(2)" in code)

print("\nIT SAYS WHAT IS MISSING INSTEAD OF DRAWING NOTHING")
ok("no address yet -> it says how to get one", "Not connected yet" in code and "Generate Domain" in code)
ok("an unreachable board says so", "Sports board unreachable" in code)
ok("no finished games -> the scorecard says why", "no finished games scored yet" in code)
ok("a loss and a profit carry a sign and a word",
   "' profit'" in code and "' loss'" in code and "'+$'" in code)

print("\nIT DOES NOT HAMMER THE BOARD")
ok("refreshes every 5 minutes", "setInterval(refreshSports, 300000)" in HTML)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
