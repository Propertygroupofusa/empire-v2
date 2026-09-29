#!/usr/bin/env python3
"""The dashboard card for the adopted catastrophe stop.

WHY THERE IS A CARD. The switch was env-only, so throwing it meant a Railway
visit and a restart - and the page that SHOWS the gap (fourteen branches, $6,271
of adopted coin with no stop at any price) could not close it. The owner runs
maker-only, the spacing override and the net-edge gate from this page; this was
the odd one out, and it is the one that decides whether that coin has a stop.

WHAT IS ASSERTED. The card is rendered in node with a stub DOM, against
fixtures, and the output is read - not grepped for in the source. Four
properties, each of which this page has already been burned by once:

  * the button is SUPPRESSED when an environment variable is deciding, because
    a button that silently cannot win is the failure the maker-only card had;
  * the cushion is computed and shown, because "armed" alone does not tell the
    owner whether anything is about to be sold;
  * success is never claimed from the POST returning 200 - the server is
    re-read and the mode checked;
  * nothing renders as undefined, NaN or [object Object].
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).parent
_failures = []
_passes = 0


def ok(label, condition, detail=""):
    global _passes
    if condition:
        _passes += 1
        print(f"  ok   {label}")
    else:
        _failures.append(f"{label}{(' - ' + detail) if detail else ''}")
        print(f"  FAIL {label}{(' - ' + detail) if detail else ''}")


def _script():
    """Every inline <script> from the dashboard, concatenated."""
    html = (HERE / "family_tree_dashboard.html").read_text()
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    return "\n".join(blocks)


BRANCHES = [
    # ZEC: 17% down against a 35% stop - the real live shape.
    {"product_id": "ZEC-USD", "stop_loss_pct_override": 0, "current_price": 1372,
     "stop_pct": 0.35, "slices": [{"entry_price": 1659}, {"entry_price": 1650}]},
    # SOL: the thinnest cushion on the live fleet.
    {"product_id": "SOL-USD", "stop_loss_pct_override": 0, "current_price": 117,
     "stop_pct": 0.20, "slices": [{"entry_price": 124}]},
    # A branch whose volatility could not be read: stop 0 while armed.
    {"product_id": "BAD-USD", "stop_loss_pct_override": 0, "current_price": 10,
     "stop_pct": 0, "slices": [{"entry_price": 11}]},
    # Not adopted - must be ignored entirely.
    {"product_id": "NEAR-USD", "stop_loss_pct_override": None, "current_price": 2,
     "stop_pct": 0.19, "slices": [{"entry_price": 3}]},
]
# Past its stop: 50% below entry against a 35% stop.
PAST_STOP = dict(BRANCHES[0], current_price=800)

HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const store = {};
global.document = {
  getElementById: (id) => (store[id] = store[id] || {innerHTML:'', className:'', appendChild(){}}),
  querySelector: () => null,
  createElement: () => ({style:{}}),
  addEventListener: () => {},
};
global.window = {};
global.sessionStorage = {getItem: () => '', setItem(){}, removeItem(){}};
global.fetch = () => Promise.reject(new Error('no network here'));
global.setTimeout = () => 0; global.clearTimeout = () => {};
global.API_BASE = '/api';
let evalError = null;
try { eval(src); } catch (e) { evalError = e.message; }
const out = {evalError, rendered: {}};
if (!evalError) {
  for (const name of Object.keys(cases)) {
    store['grid-adopted-stop-toggle'] = {innerHTML:'', className:'', appendChild(){}};
    try {
      renderAdoptedStopToggle(cases[name]);
      out.rendered[name] = {
        html: store['grid-adopted-stop-toggle'].innerHTML,
        cls: store['grid-adopted-stop-toggle'].className,
      };
    } catch (e) { out.rendered[name] = {threw: e.message}; }
  }
  out.hasToggle = typeof toggleAdoptedStop === 'function';
  out.hasCushions = typeof adoptedStopCushions === 'function';
}
console.log(JSON.stringify(out));
process.exit(0);
"""


def render():
    if not shutil.which("node"):
        ok("node is available to render the card", False,
           "the card cannot be verified without it, and an unverified panel is "
           "not a passing one")
        return None
    cases = {
        "off": {"adopted_stop_mode": "off", "adopted_stop_source": "database",
                "branches": [dict(b, stop_pct=0 if b["stop_loss_pct_override"] == 0
                                  else b["stop_pct"]) for b in BRANCHES]},
        "armed": {"adopted_stop_mode": "arm", "adopted_stop_source": "database",
                  "branches": BRANCHES},
        "env": {"adopted_stop_mode": "arm",
                "adopted_stop_source": "environment GRID_ADOPTED_STOP_MODE (wins over the database)",
                "branches": BRANCHES},
        "selling": {"adopted_stop_mode": "arm", "adopted_stop_source": "database",
                    "branches": [PAST_STOP]},
        "empty": {"adopted_stop_mode": "off", "adopted_stop_source": "database",
                  "branches": []},
        "null_branches": {"adopted_stop_mode": "arm", "adopted_stop_source": "database"},
    }
    with tempfile.TemporaryDirectory() as d:
        js, cj, hj = (pathlib.Path(d) / n for n in ("dash.js", "cases.json", "h.js"))
        js.write_text(_script())
        cj.write_text(json.dumps(cases))
        hj.write_text(HARNESS)
        r = subprocess.run(["node", str(hj), str(js), str(cj)],
                           capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        ok("the harness ran", False, (r.stderr or "")[:300])
        return None
    return json.loads(r.stdout)


OUT = render()


def test_the_whole_dashboard_script_still_evaluates():
    """An HTML edit has no ast.parse. If the script does not evaluate, every
    panel on the page is down, not just this one."""
    ok("the dashboard script evaluates", OUT and not OUT.get("evalError"),
       (OUT or {}).get("evalError") or "harness did not run")
    if OUT and not OUT.get("evalError"):
        ok("renderAdoptedStopToggle exists", "off" in (OUT.get("rendered") or {}))
        ok("toggleAdoptedStop exists", OUT.get("hasToggle") is True)
        ok("adoptedStopCushions exists", OUT.get("hasCushions") is True)


def _h(name):
    return ((OUT or {}).get("rendered") or {}).get(name, {}).get("html", "")


def test_no_state_throws_or_leaks_a_placeholder():
    for name in ("off", "armed", "env", "selling", "empty", "null_branches"):
        cell = ((OUT or {}).get("rendered") or {}).get(name, {})
        ok(f"{name}: does not throw", "threw" not in cell, cell.get("threw", ""))
        h = cell.get("html", "")
        bad = re.findall(r"undefined|NaN|\[object \w+\]", h)
        ok(f"{name}: renders no placeholder", not bad, str(bad))


def test_the_button_is_suppressed_when_the_environment_decides():
    """The exact failure the maker-only card already had: a button that cannot
    win, offered anyway."""
    ok("off: offers a button", "<button" in _h("off"))
    ok("armed: offers a button", "<button" in _h("armed"))
    ok("ENV: offers NO button", "<button" not in _h("env"), _h("env")[:160])
    ok("ENV: names the switch that is deciding",
       "GRID_ADOPTED_STOP_MODE" in _h("env"))
    ok("ENV: says how to hand control back",
       "remove the variable" in _h("env").lower())


def test_the_card_says_whether_anything_would_sell_before_it_is_tapped():
    ok("armed: reports a would-sell count", "would sell right now" in _h("armed"))
    ok("armed: that count is zero on the live shape",
       re.search(r"<b>0</b>\s*would sell right now", _h("armed")) is not None,
       _h("armed")[:200])
    ok("armed: shows the thinnest cushion in points",
       re.search(r"<b>[\d.]+ pts</b>", _h("armed")) is not None)
    ok("armed: names which coin is thinnest", "SOL" in _h("armed"))
    ok("armed: no red warning when nothing would sell",
       "will be sold on the next cycle" not in _h("armed"))
    ok("armed: an unsized branch is reported, not hidden",
       "volatility could not be read" in _h("armed"))


def test_a_branch_past_its_stop_is_named_in_red_before_the_tap():
    h = _h("selling")
    ok("selling: the count is 1", re.search(r"<b>1</b>\s*would sell right now", h)
       is not None, h[:200])
    ok("selling: it warns explicitly", "will be sold on the next cycle" in h)
    ok("selling: it names the branch", "ZEC-USD" in h)
    ok("selling: and is coloured red", "#ef4444" in h)


def test_a_non_adopted_branch_is_never_counted():
    """NEAR has its own adaptive stop and no override of 0. Counting it would
    overstate the gap and, once armed, claim a stop this switch did not give."""
    for name in ("off", "armed"):
        ok(f"{name}: NEAR is not in the card", "NEAR" not in _h(name), _h(name)[:200])
    ok("the adopted count is 3, not 4",
       re.search(r"<b>3</b>\s*adopted branch", _h("armed")) is not None,
       _h("armed")[:220])


def test_the_toggle_does_not_trust_the_post():
    """Read from the source for this one, because it is about what the function
    DOES after a 200 and the harness cannot make a network call. Narrowed to the
    function body so the assertion cannot be satisfied by other code."""
    src = _script()
    m = re.search(r"async function toggleAdoptedStop\(enable\) \{(.*?)\n\}", src, re.S)
    ok("toggleAdoptedStop was located", m is not None)
    if not m:
        return
    body = re.sub(r"(?m)^\s*//.*$", "", m.group(1))
    ok("it POSTs to the adopted-stop endpoint",
       "'/grid-status/adopted-stop'" in body)
    ok("it sends writeHeaders(), so the owner's token flows",
       "writeHeaders()" in body)
    ok("it hardcodes no token", "x-dashboard-token" not in body)
    ok("it RE-READS /grid-status rather than trusting the POST",
       "'/grid-status'" in body and "adopted_stop_mode" in body)
    ok("and throws when the server disagrees with what was asked",
       "after the write" in body)
    ok("it requires a second tap before acting", "dataset.armed" in body)
    ok("the confirmation is in-page, never a native confirm()",
       "confirm(" not in body)


def test_zz_nothing_above_failed():
    assert not _failures, f"{len(_failures)} checks failed: {_failures}"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for t in TESTS:
        print(f"\n== {t.__name__}")
        try:
            t()
        except BaseException as e:
            ok(f"{t.__name__} ran to completion", False,
               f"raised {type(e).__name__}: {e}")
    print()
    if _failures:
        print(f"{len(_failures)} FAILED, {_passes} passed:")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"all {_passes} adopted-stop card checks passed")
