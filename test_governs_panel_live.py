"""The "What governs this fleet" panel must MEASURE, not recite.

2026-09-26: the maker-only card read

    2.00%  what the step earns
    1.37%  what a round trip costs
    +0.63% it clears

against a fleet whose live step was 2.50% and whose live maker round trip
was 0.70%. All three came from one line of constants:

    const takerCost = 2.17, makerCost = 1.37, step = 2.00;

So the card understated the real clearance (+1.13%) by nearly half, on the
one screen used to judge whether the spacing is wide enough to trade. The
labels said "what the step earns" and "what a round trip costs" - the
language of measurement - which is exactly why nobody suspected them.

The fee-floor row had the matching fault in words: it hardcoded "the
smallest step that clears a TAKER round trip" while the card below it
reported the same 0.90% as priced against maker. Both cannot be true, and
the taker one is arithmetically impossible - a 0.90% step cannot clear a
1.50% round trip.

Run: python3 test_governs_panel_live.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HTML = open(os.path.join(HERE, "family_tree_dashboard.html"), encoding="utf-8").read()
BOT = open(os.path.join(HERE, "crypto_grid_bot.py"), encoding="utf-8").read()

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


CARD = HTML.split("function renderGridMakerOnlyToggle")[1].split("\nfunction ")[0]
PANEL = HTML.split("function renderGridGovernsPanel")[1].split("\nfunction ")[0]


def code(block):
    """Executable lines only.

    The comment above each fix QUOTES the wrong code it replaced, to record
    what the numbers were and why they were wrong. Matching the raw block
    scores that explanation as the bug still being present - the same trap
    that let "nothing will be bought or sold" pass a regression check from a
    comment earlier today.
    """
    return "\n".join(l for l in block.splitlines() if not l.strip().startswith("//"))


CARD_CODE, PANEL_CODE = code(CARD), code(PANEL)


print("\nthe card's numbers come from the payload, not from the file")
ok("REGRESSION: the three hardcoded costs are gone",
   "takerCost = 2.17" not in CARD_CODE and "makerCost = 1.37" not in CARD_CODE,
   "these outlived the fee tier and the step they were written for")
ok("the step is read from the live branches",
   "data.branches" in CARD and "grid_pct" in CARD,
   "the same field the panel above it already renders as 'Step in force'")
ok("it takes the NARROWEST branch, the one that decides",
   "Math.min(...liveSteps)" in CARD,
   "an average hides a branch that cannot clear its own cost")
ok("the maker fee is read from the payload",
   "maker_round_trip_fee_rate" in CARD)
ok("the taker fee is read from the payload",
   "taker_round_trip_fee_rate" in CARD)
ok("a missing fill_mix falls back to the live effective rate, not a constant",
   "effective_round_trip_fee_rate" in CARD)


print("\nadverse selection is named, not silently folded into 'fees'")
ok("it is a named constant, not a magic number in an expression",
   "const ADVERSE" in CARD)
ok("it matches the figure the net-edge gate measured",
   "0.67" in CARD and "adverse selection  0.67%" in BOT)
ok("the card shows the split on screen",
   "fees +" in CARD and "adverse selection" in CARD,
   "'what a round trip costs' and 'real round-trip fee' are DIFFERENT "
   "quantities; unlabelled they read as one number contradicting itself")


print("\nthe fee floor says what it is actually priced against")
ok("REGRESSION: 'taker' is no longer asserted unconditionally",
   "clears a <em>taker</em> round trip" not in PANEL_CODE,
   "a 0.90% floor cannot clear a 1.50% taker round trip")
ok("it renders the backend's floor_priced_against",
   "floor_priced_against" in PANEL)
ok("the backend still sends that field",
   '"floor_priced_against"' in BOT)
ok("and the floor itself is still priced against the WORST reachable leg",
   "worst_case_leg_fee_rate()" in BOT
   and "TARGET_NET_MARGIN_PCT + leg * 2" in BOT,
   "this test must not become a reason to soften the floor itself")


print("\nthe arithmetic the card claims")
# floor = max(MIN_DYNAMIC_GRID_PCT, TARGET_NET_MARGIN_PCT + maker_leg*2)
target = float(re.search(r'TARGET_NET_MARGIN_PCT = float\(os\.getenv\("[^"]+", "([^"]+)"\)\)', BOT).group(1))
mindyn = float(re.search(r'MIN_DYNAMIC_GRID_PCT = ([\d.]+)', BOT).group(1))
maker_leg = 0.0035
ok("the live 0.90% floor is reproducible from the constants",
   abs(max(mindyn, target + maker_leg * 2) - 0.009) < 1e-9,
   f"target={target} min={mindyn} -> {max(mindyn, target + maker_leg * 2)}")
ok("a 2.50% step really does clear 0.70% fees + 0.67% adverse selection",
   round(2.50 - (0.70 + 0.67), 2) == 1.13)
ok("and the old constants really did understate it",
   round(2.00 - 1.37, 2) == 0.63 and 0.63 < 1.13)


print("\nno stale topology in the operator instruction")
ok("REGRESSION: it no longer sends the operator to a 'crypto-trading service'",
   "change it on the crypto-trading service" not in CARD_CODE,
   "the loop lease is held by web:1 - that service is not running the fleet")


print("\ntheoretical and realized are separated, not blended")
BOT_FN = BOT.split("async def get_realized_edge")[1].split("\nasync def ")[0]
ok("the backend measures realized edge from the closed book",
   "async def get_realized_edge" in BOT)
ok("it is served in the grid-status payload",
   '"realized_edge": await get_realized_edge()' in BOT)
ok("gross and net share ONE denominator (notional-weighted)",
   'gross / notional' in BOT_FN and 'net / notional' in BOT_FN,
   "an unweighted mean of percentages beside a weighted total yields an "
   "implied cost that is not any real cost")
ok("the cost it derives is named 'implied', never 'measured'",
   '"implied_cost_pct"' in BOT_FN and '"measured_adverse' not in BOT_FN)
ok("it ships the survivorship warning with the number",
   "survivorship_warning" in BOT_FN,
   "completed round trips only - the slices that never come back are "
   "exactly where adverse selection lands")
ok("velocity is returned BESIDE the margin, not omitted",
   "closes_per_day" in BOT_FN and "days_since_last_close" in BOT_FN,
   "margin per completed cycle is not a return")
ok("stop-loss closes are counted separately",
   "stop_loss_closes" in BOT_FN)

ok("the card labels the theoretical figure as theoretical",
   "in theory" in CARD_CODE)
ok("the card shows the realized figure from the payload",
   "data.realized_edge" in CARD_CODE and "re.net_pct" in CARD_CODE)
ok("it prints the realized cost against the ASSUMED one",
   "assumed" in CARD_CODE and "re.implied_cost_pct" in CARD_CODE)
ok("it warns on screen that the realized cost is understated",
   "UNDERSTATES" in CARD_CODE)
ok("it shows days-since-last-close, the number margin cannot answer",
   "days_since_last_close" in CARD_CODE and "not a return" in CARD_CODE)

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
