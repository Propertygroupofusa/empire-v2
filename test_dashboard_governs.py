"""A control on the dashboard is a loaded gun. Only show the ones that should fire.

The bill for not having this, 2026-09-25 — four switches on one page, each
of which cost real money or real time in a single evening:

  fee-tier spacing / average-swing spacing
      Either one silently overrides the per-coin measured step. Turning all
      three spacing modes OFF - needed so per-coin tuning could drive - put
      the fleet in the one state where the fee floor was never applied, and
      five new branches were created at 1.00% against a 1.70% floor. A
      completed round trip there nets -$0.02 at maker and -$0.12 at taker.

  auto-rotate
      Retired four EARNING branches to idle cash on DIRECTIONAL ROI, which
      is not how a grid earns. DOGE went out at -23.8% directional while
      measuring +$22.81 on the grid over 90 days. $276.80 - 64% of the
      account - sat idle until someone noticed.

  genuine-edge floor 20%
      The rule auto-rotate judged them by.

Two of them also failed SILENTLY when tapped: a confirm() dialog dismissed
on a phone returns with no request, no error and no feedback, so the page
kept showing the old state and the operator reasonably believed it changed.

THE RULES THIS FILE PROTECTS:

  1. Those five renderers and three toggles are GONE from the page. Not
     hidden, not disabled - absent, so they cannot be flipped by accident.
  2. What replaces them is READ-ONLY and MEASURED: the fee floor, whether
     any branch sits below it, the step actually in force, and how legs
     really filled.
  3. A branch below the floor is the loudest thing on the panel. That is
     the state that loses money on every completed round trip.
  4. The panel can never take the page down - it is wrapped, like every
     other renderer here.
  5. The server endpoints still exist. Removing a control from a phone
     screen must not remove the ability to set it deliberately.

Run: python3 test_dashboard_governs.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
page = open(os.path.join(HERE, "family_tree_dashboard.html"), encoding="utf-8").read()
router = open(os.path.join(HERE, "routers", "trading_dashboard.py"), encoding="utf-8").read()
checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


# --- the dangerous controls are gone -------------------------------------
GONE = ["renderGridDynamicSpacingBadge", "toggleGridDynamicSpacing",
        "renderGridAvgSwingSpacingBadge", "toggleGridAvgSwingSpacing",
        "renderGridAutoRotateBadge", "toggleGridAutoRotate",
        "renderGridMinRoiFloorBadge", "renderGridSpacingOverrideBadge"]
for name in GONE:
    # The removal note names them on purpose; a DEFINITION or CALL must not exist.
    defined = re.search(rf"^\s*(async\s+)?function\s+{name}\s*\(", page, re.M)
    called = re.search(rf"(?<!// ){name}\s*\(", page)
    ok(f"{name} is gone (no definition)", not defined)
    ok(f"{name} is gone (never called)", not called)

for el in ("grid-dynamic-spacing-badge", "grid-avg-swing-spacing-badge",
           "grid-auto-rotate-badge", "grid-min-roi-floor-badge",
           "grid-spacing-override-badge"):
    ok(f"the {el} container is gone", f'id="{el}"' not in page)

ok("no onclick can still reach a removed toggle",
   "toggleGridAutoRotate(" not in page.replace("// renderGridAutoRotateBadge / toggleGridAutoRotate,", ""))

# --- what replaced them is read-only and measured ------------------------
ok("the governance panel exists", 'id="grid-governs-panel"' in page)
ok("it is rendered from the existing loader, costing no extra request",
   "renderGridGovernsPanel(data)" in page)
panel = page[page.index("function renderGridGovernsPanel"):page.index("function renderGridFeeRateBadge")]
ok("it shows the fee floor", "fee_safe_min_grid_pct" in panel)
ok("it shows the step actually in force", "grid_pct" in panel)
ok("it shows how legs really filled", "fill_mix" in panel and "maker_rate" in panel)
ok("it reports the blended real round trip", "blended_round_trip_fee_rate" in panel)

ok("a below-floor branch is computed, not assumed",
   "b.grid_pct < floor" in panel)
ok("below-floor branches are NAMED, not merely counted",
   "below.map" in panel and "product_id" in panel)
ok("below-floor says plainly that it loses money",
   "lose money on every completed round trip" in panel)
ok("un-measured fills say so rather than showing 0%",
   "not measured yet" in panel)

# --- it contains no switch -----------------------------------------------
ok("the panel has no clickable control at all",
   "onclick" not in panel and "<a " not in panel)
ok("the panel never POSTs", "fetch(" not in panel)

# --- it cannot take the page down ----------------------------------------
ok("the panel is wrapped", "try {" in panel and "catch" in panel)
ok("its failure renders an explanation, not a blank", "could not render" in panel)

# --- deliberate control is still possible server-side --------------------
for ep in ("/grid-status/auto-rotate", "/grid-status/dynamic-spacing",
           "/grid-status/avg-swing-spacing", "/grid-status/spacing-override"):
    ok(f"{ep} still exists server-side", ep in router)

# --- the prose must not describe controls that no longer exist -----------
ok("REGRESSION: the intro no longer points at a removed toggle",
   "see the toggle below" not in page)
ok("it no longer claims idle cash auto-deploys with no click",
   "no manual click ever required" not in page)
ok("it says plainly that cash only moves when you move it",
   "only when you move it" in page)
ok("it records WHY rotation was removed, with the number",
   "-23.8% directional" in page and "+$22.81" in page)
ok("it is honest that the drawdown breaker is untested, not proven",
   "untested insurance" in page)
ok("it states the measured direction: wider beat tighter",
   "$128.30" in page and "$23.24" in page)

# --- hygiene --------------------------------------------------------------
ids = re.findall(r'id="([a-zA-Z0-9_-]+)"', page)
ok("no duplicate element ids", len({i for i in ids if ids.count(i) > 1}) == 0)
ok("braces stay balanced after the removals",
   page.count("{") == page.count("}"))
ok("details tags stay balanced", page.count("<details") == page.count("</details>"))
ok("the status strip survived", 'id="status-strip"' in page)
ok("the branch panel survived", 'id="grid-panel"' in page)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
