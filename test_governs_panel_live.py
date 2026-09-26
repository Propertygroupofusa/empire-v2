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
# These assert the per-cohort maths, which lives in _edge_cohort - the
# cohort split moved it out of get_realized_edge. Repointed rather than
# deleted: the claims are unchanged, only their address is.
BOT_FN = BOT.split("def _edge_cohort")[1].split("\nasync def ")[0]
ok("the backend measures realized edge from the closed book",
   "async def get_realized_edge" in BOT)
# Served through _never_fails: the builder's DB read was guarded but its
# arithmetic was not, and a raise there propagated out of
# get_grid_fleet_status() and took the whole payload - dashboard, runner
# panel and watch - down with it, over a diagnostic nobody trades on.
ok("it is served in the grid-status payload",
   '"realized_edge": await _never_fails(get_realized_edge' in BOT)
ok("and it is served through the guard, so telemetry cannot break the payload",
   "async def _never_fails" in BOT and BOT.count("await _never_fails(") == 3)
ok("gross and net share ONE denominator (notional-weighted)",
   'gross / notional' in BOT_FN and 'net / notional' in BOT_FN,
   "an unweighted mean of percentages beside a weighted total yields an "
   "implied cost that is not any real cost")
ok("the cost it derives is named 'implied', never 'measured'",
   '"implied_cost_pct"' in BOT_FN and '"measured_adverse' not in BOT_FN)
ok("it ships the survivorship warning with the number",
   "survivorship_warning" in BOT,
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
   "data.realized_edge" in CARD_CODE and "re.current.net_pct" in CARD_CODE)
ok("it prints the realized cost against the ASSUMED one",
   "assumed cost" in CARD_CODE and "re.current.implied_cost_pct" in CARD_CODE)
ok("it warns on screen that the realized cost is understated",
   "is understated" in CARD_CODE)
ok("it shows days-since-last-close, the number margin cannot answer",
   "days_since_last_close" in CARD_CODE and "since ANY completed cycle" in CARD_CODE)


print("\ncohorts are split, never pooled")
EDGE = BOT.split("async def get_realized_edge")[1].split("\nasync def ")[0]
COH = BOT.split("def _edge_cohort")[1].split("\nasync def ")[0]
ok("a configuration epoch exists and is tunable",
   "GRID_CONFIG_EPOCH" in BOT and 'os.getenv("GRID_CONFIG_EPOCH"' in BOT,
   "it must be re-baselined whenever step, fee path or coins change")
ok("the payload separates current from retired",
   '"current":' in EDGE and '"retired":' in EDGE)
ok("REGRESSION: there is no single pooled net_pct at the top level",
   '"net_pct": round' not in EDGE,
   "pooled, 50 retired trades reported +1.95% for a fleet with zero cycles")
ok("an unparseable epoch fails toward RETIRED, never toward current",
   "datetime.max" in BOT,
   "the failure that matters is old trades counted as new")
ok("a baseline threshold is stated, not implied",
   "current_has_baseline" in EDGE)
ok("the headline says plainly when there is no baseline",
   "no baseline yet" in EDGE)
ok("cohort math stays notional-weighted", "gross / notional" in COH and "net / notional" in COH)
ok("the survivorship warning survives the refactor", "survivorship_warning" in EDGE)

ok("the card leads with the CURRENT cohort", "re.current" in CARD_CODE)
ok("the retired cohort is labelled as not representative",
   "RETIRED configuration" in CARD_CODE and "not representative" in CARD_CODE)
ok("with zero current cycles the card says the theory is not a result",
   "arithmetic, not a result" in CARD_CODE)
ok("days-since-close is in the headline row, not a footnote",
   "since ANY completed cycle" in CARD_CODE)


print("\npost-expiry drift: the ledger of trades that did NOT happen")
REC = BOT.split("async def _record_maker_expiry")[1].split("\nasync def ")[0]
RES = BOT.split("async def _resolve_maker_expiries")[1].split("\nasync def ")[0]
DRIFT = BOT.split("async def get_maker_expiry_drift")[1].split("\nasync def ")[0]
MODELS = open(os.path.join(HERE, "models.py"), encoding="utf-8").read()

ok("a table records each cancelled post-only order", "class GridMakerExpiry" in MODELS)
ok("four horizons, not one",
   all(f"price_{t} = Column" in MODELS for t in ("1m","3m","5m","10m")),
   "protective at 1m and too-aggressive at 10m is a real shape a single "
   "horizon reports half of")
ok("every resolved column is nullable, so a row is usable while filling in",
   MODELS.split("class GridMakerExpiry")[1].split("\nclass ")[0].count("nullable=True") >= 12)

ok("it is anchored only where maker-only actually cancelled",
   '_record_maker_expiry(session, product_id, "buy"' in BOT
   and '_record_maker_expiry(session, product_id, "sell"' in BOT)
ok("NOT on the maker-first path, where a market order still traded",
   BOT.count('_record_maker_expiry(session, product_id, "') == 2,
   "an order that fell back to market DID trade; it has no 'what did we miss'")
ok("mid price at BOTH ends, so the spread is not booked as a move",
   "(bid + ask) / 2.0" in REC and "(bid + ask) / 2.0" in RES)

ok("the sign convention is fixed in exactly one place",
   RES.count('-drift if row.side == "buy" else drift') == 1)
ok("a cancelled BUY counts a FALL as the benefit",
   '-drift if row.side == "buy"' in RES,
   "we did not buy, so a cheaper price afterwards is the gain")
ok("recording can never break trading", "log.debug" in REC and "except Exception" in REC)
ok("resolution can never break trading", "log.debug" in RES and "except Exception" in RES)
ok("resolution is capped per cycle", "_EXPIRY_RESOLVE_MAX_PER_CYCLE" in BOT and ".limit(" in RES)
ok("it runs AFTER the branch loop, so it cannot delay a trade",
   BOT.index("await _resolve_maker_expiries(session)")
   > BOT.index("await run_grid_branch_cycle(session, branch)"))
ok("each horizon resolves once and only once",
   'getattr(row, f"price_{tag}") is None' in RES)

ok("the verdict refuses to speak on a small sample",
   "not enough data" in DRIFT and "< 20" in DRIFT,
   "reading three samples as a finding is how the 50-trade book got misread")
ok("POSITIVE is defined on screen as 'cancelling helped'",
   "moved against us after cancelling" in CARD_CODE)
ok("the drift block is served in the payload", '"maker_expiry_drift"' in BOT)
ok("the card renders all four horizons", "['1m','3m','5m','10m']" in CARD_CODE)

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
