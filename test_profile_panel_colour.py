#!/usr/bin/env python3
"""The safety-mode word must never be coloured as a money verdict.

WHY THIS EXISTS. The trading-profile panel rendered the profile name -
"GUARDED" - at 1.05em bold in var(--green), immediately above a block
whose only dollar figures were -$86.83 and -$7.61.

Read as a whole, that is a green headline over a column of minus signs,
and GUARDED is one glance from GUARANTEED. It was read exactly that way:
as a guarantee, in green, of a loss.

Two separate defects:

  1. Colour was doing double duty. Green meant "protection is on" in one
     place and "money is up" everywhere else on the page.
  2. The only dollar figures shown came from a CLOSED window
     (2026-08-26..09-24) on a fee tier that no longer applies, with
     nothing beside them from the present - so a historical loss
     impersonated the current state.

Both are pinned here, because both are the kind of thing that reads fine
to whoever wrote it and wrong to everybody else.
"""
import re, sys

FAILS = []
def ok(label, cond, detail=""):
    if cond: print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        FAILS.append(label)

HTML = open("family_tree_dashboard.html").read()

def fn(name):
    """Brace-matched function body - never a fixed-width slice."""
    i = HTML.index(f"function {name}(")
    j = HTML.index("{", i)
    depth, k = 0, j
    while k < len(HTML):
        if HTML[k] == "{": depth += 1
        elif HTML[k] == "}":
            depth -= 1
            if depth == 0: return HTML[i:k+1]
        k += 1
    raise AssertionError(f"unbalanced braces in {name}")

PROFILE = fn("loadTradingProfile")

print("\n[1] the profile NAME is not painted green or red")
hdr = PROFILE[PROFILE.index("Safety mode"):PROFILE.index("Safety mode") + 700]
ok("the name sits in a neutral accent, not --green",
   "var(--green)" not in hdr, hdr[:200])
ok("and not --red", "var(--red)" not in hdr)
ok("it is labelled as a safety mode", "Safety mode" in hdr)
ok("and says in words that it is not a profit claim",
   "not a claim about profit" in PROFILE, "missing the disclaimer")

print("\n[2] the old green/amber colouring of the name is GONE")
ok("no 'guarded ? var(--green)' ternary remains",
   not re.search(r"guarded\s*\?\s*'var\(--green\)'", PROFILE),
   "the money colour is still bound to the profile name")

print("\n[3] money carries its own colour, by SIGN")
MH = fn("_moneyHTML")
ok("negative -> --red", "n < 0 ? 'var(--red)'" in MH, MH)
ok("positive -> --green", "var(--green)" in MH)
ok("zero is neutral, not green", "var(--text-dim)" in MH)
ok("an unreadable number is NOT rendered as $0.00",
   "unreadable" in MH and "isFinite" in MH)

print("\n[4] the closed window is labelled as closed, beside a present figure")
TVN = fn("thenVsNow")
ok("the historical cell is named a CLOSED window",
   "Closed window" in TVN, TVN[:200])
ok("...and says the window is OVER", "is OVER" in TVN)
ok("...and names the old fee tier", "1.50% taker" in TVN)
ok("a 'since then' cell exists", "Since then" in TVN)
ok("...fed from the endpoint, not hardcoded",
   "d.since_then" in TVN)
ok("...and reports UNREADABLE rather than 0 when history is missing",
   "st.readable" in TVN and "unreadable" in TVN)
ok("the historical loss is rendered through the money colourer",
   "_moneyHTML(-86.83)" in TVN)

print("\n[5] the endpoint supplies the present figure, fail-soft")
API = open("routers/trading_dashboard.py").read()
i = API.index('@router.get("/trading-profile")')
BODY = API[i:API.index("@router.post", i)]
ok("since_then is on the response", '"since_then"' in BODY)
ok("it defaults to readable:False, not to zero",
   '"since_then"] = {"readable": False' in BODY or
   '"since_then" : {"readable": False' in BODY, "missing the unreadable default")
ok("it uses the real model name CryptoGridTradeHistory",
   "CryptoGridTradeHistory" in BODY)
ok("and NOT an invented one", "CryptoGridTrade." not in BODY.replace("CryptoGridTradeHistory.", ""))
ok("label_means states it is not a money claim",
   '"is_a_money_claim": False' in BODY)

print("\n[6] nothing about the GATES changed - this is a display fix")
for gate in ("GRID_CASH_RESERVE_USD", "MIN_TRADE_USD"):
    ok(f"{gate} is still only read, never reassigned here",
       not re.search(rf"{gate}\s*=\s*[^=]", BODY), f"{gate} assigned in the endpoint")

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
