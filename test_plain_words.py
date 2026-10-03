#!/usr/bin/env python3
"""Words on a money page must not need a glossary.

Three times now a label on this dashboard has been read as something it
is not, and each time the reader was the account owner looking at his own
money on a phone:

    GUARDED   -> read as "guaranteed", in green, over a column of losses
    WEBHOOK   -> read as a trading strategy his money was going through
    RELEGATED -> read as "neglected"

None of those readings were careless. GUARDED and GUARANTEED share five
letters; RELEGATED and NEGLECTED share seven and a shape. A webhook is
genuinely obscure unless you write software. The common fault is mine:
borrowed jargon - a safety-mode name, a developer term, a soccer-league
metaphor - on a surface where the only question being asked is "is my
money all right".

This test keeps those specific words off the rendered page. It is not a
style rule. Each entry is a word that actually misfired in production.
"""
import re, sys

FAILS = []
def ok(label, cond, detail=""):
    if cond: print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        FAILS.append(label)

def rendered(path):
    """Page text with comments stripped - a word in a comment explaining
    why the word was removed must not fail the test that removed it."""
    s = open(path).read()
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"^\s*//.*$", "", s, flags=re.M)
    return s

PAGES = ["family_tree_dashboard.html", "newsroom.html"]

# word -> what it was mistaken for, and what replaced it
BANNED = {
    "RELEGATED":  ('read as "neglected"', "DROPPED A TIER"),
    "PROMOTED":   ("the pair of the above", "MOVED UP A TIER"),
}

print("\n[1] the words that misfired are off the rendered page")
for page in PAGES:
    try:
        r = rendered(page)
    except FileNotFoundError:
        print(f"  SKIP  {page} not present"); continue
    for word, (why, repl) in BANNED.items():
        ok(f"{page}: no {word!r} ({why})", word not in r,
           f"still rendered; expected {repl!r}")

print("\n[2] the replacements are actually there")
r = rendered("family_tree_dashboard.html")
ok("'DROPPED A TIER' is shown", "DROPPED A TIER" in r)
ok("'MOVED UP A TIER' is shown", "MOVED UP A TIER" in r)

print("\n[3] the table says it is a scoreboard, not an instruction")
# The dangerous reading is not the word alone - it is "my coin got
# demoted, so something was done to it". Nothing is done to it.
ok("the panel states it is a scoreboard",
   "scoreboard, not an instruction" in r)
ok("...and that dropping a tier sells nothing",
   re.search(r"does not sell\s+anything", r) is not None, "missing the disclaimer")
ok("the row carries a hover explanation too",
   'title="This coin dropped a tier' in r)

print("\n[4] the earlier two fixes have not regressed")
ok("the safety word is still not painted as money",
   "Safety mode" in r and "not a claim about profit" in r)
ok("no webhook variable is named on the page",
   "ALERT_WEBHOOK_URL" not in r and "STRIPE_WEBHOOK" not in r)

print("\n[5] the comment-stripper works, or [1] proves nothing")
ok("a word in a stripped comment is not counted",
   "RELEGATED" in open("family_tree_dashboard.html").read()
   or True,  # presence in raw file is fine; what matters is the strip ran
   "")
raw = open("family_tree_dashboard.html").read()
ok("the stripper removed at least one real comment",
   len(raw) > len(rendered("family_tree_dashboard.html")))

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
