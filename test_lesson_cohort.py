"""A lesson about a coin the fleet no longer trades is history, not advice.

On 2026-09-26 this panel listed eleven coins - AAVE, ARB, ATOM, BCH, DOGE,
ETC, ETH, LINK, LTC, STX, WIF - every one from the RETIRED cohort, against
a live fleet of BTC, NEAR, BONK, ONDO, FLOKI, TIA. Zero overlap. It read
"DOGE earns +12.80 ... Keep trading it" about a coin the fleet does not
hold, under a 2.00% step and market fallback that no longer exist, while
the current cohort's own record was {"trades": 0}.
"""

import ast
import json
import shutil
import subprocess
import unittest
from pathlib import Path

import grid_learning

HTML = Path(__file__).with_name("family_tree_dashboard.html")
ROUTER = Path(__file__).with_name("routers") / "trading_dashboard.py"

RETIRED = ["AAVE-USD", "ARB-USD", "ATOM-USD", "BCH-USD", "DOGE-USD", "ETC-USD",
           "ETH-USD", "LINK-USD", "LTC-USD", "STX-USD", "WIF-USD"]
FLEET = ["BTC-USD", "NEAR-USD", "BONK-USD", "ONDO-USD", "FLOKI-USD", "TIA-USD"]


def extract_js(name):
    page = HTML.read_text(encoding="utf-8")
    start = page.index("function %s(" % name)
    i = page.index("{", start)
    depth, j = 0, i
    while j < len(page):
        if page[j] == "{":
            depth += 1
        elif page[j] == "}":
            depth -= 1
            if depth == 0:
                return page[start:j + 1]
        j += 1
    raise AssertionError("unbalanced braces in %s" % name)


def render(payload):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    script = "\n".join([
        extract_js("fmtUsd"), extract_js("escText"), extract_js("renderLessons"),
        "const el = { innerHTML: '' };",
        "globalThis.document = { getElementById: id => (id === 'lessons-wrap' ? el : null) };",
        "renderLessons(%s);" % json.dumps(payload),
        "process.stdout.write(el.innerHTML);",
    ])
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return out.stdout


def lesson(pid, pnl, trades=15, in_fleet=None, note=None):
    d = {"product_id": pid, "total_pnl": pnl, "trades": trades, "wins": 11,
         "losses": 4, "win_rate": 73.0, "verdict": "earning",
         "lesson": f"{pid.replace('-USD','')} earns: {pnl:+.2f} over {trades} closed "
                   f"round trips, 73% green. Keep trading it."}
    if in_fleet is not None:
        d["in_fleet"] = in_fleet
        if in_fleet is False:
            d["retired_note"] = note or (
                f"The fleet does not trade {pid.replace('-USD','')} any more. This is "
                f"history from a retired configuration - not advice about what to trade now.")
    return d


class Tagging(unittest.TestCase):
    """grid_learning marks each lesson, without needing a database."""

    def test_the_signature_takes_the_fleet(self):
        sig = ast.parse(Path(grid_learning.__file__).read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(sig)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_all_lessons")
        names = [a.arg for a in fn.args.args]
        self.assertIn("fleet_products", names)

    def test_an_unknown_fleet_tags_nothing(self):
        # Not knowing is not evidence. None must not mark everything retired.
        src = ast.get_source_segment(
            Path(grid_learning.__file__).read_text(encoding="utf-8"),
            next(n for n in ast.walk(ast.parse(
                Path(grid_learning.__file__).read_text(encoding="utf-8")))
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_all_lessons"))
        self.assertIn("None if fleet is None", src)

    def test_the_router_passes_the_live_fleet(self):
        src = ROUTER.read_text(encoding="utf-8")
        self.assertIn("get_all_lessons(fleet)", src)
        self.assertIn("if b.active", src)

    def test_an_unreadable_fleet_falls_back_to_none_not_empty(self):
        src = ROUTER.read_text(encoding="utf-8")
        i = src.index("fleet unreadable for lesson tagging")
        self.assertIn("fleet = None", src[max(0, i - 600):i])


class Panel(unittest.TestCase):
    def test_the_live_screenshot_case_is_called_out(self):
        payload = {
            "lessons": [lesson(p, 1.0, in_fleet=False) for p in RETIRED],
            "lesson_count": len(RETIRED), "fleet_known": True,
            "in_fleet_count": 0, "retired_count": len(RETIRED),
            "all_lessons_are_retired": True,
            "enforcement_active": False,
            "min_trades_for_a_verdict": 10, "min_trades_to_block": 25, "note": "",
        }
        html = render(payload)
        self.assertIn("Every lesson here is from coins the fleet no longer trades", html)
        self.assertIn("no current advice on this panel", html)

    def test_a_retired_coin_carries_its_warning(self):
        payload = {"lessons": [lesson("DOGE-USD", 12.80, in_fleet=False)],
                   "lesson_count": 1, "fleet_known": True, "in_fleet_count": 0,
                   "retired_count": 1, "all_lessons_are_retired": True,
                   "enforcement_active": False, "min_trades_for_a_verdict": 10,
                   "min_trades_to_block": 25, "note": ""}
        html = render(payload)
        self.assertIn("does not trade DOGE any more", html)
        self.assertIn("not advice about what to trade now", html)

    def test_retired_and_live_are_separated(self):
        payload = {
            "lessons": [lesson("DOGE-USD", 12.80, in_fleet=False),
                        lesson("NEAR-USD", 0.5, in_fleet=True)],
            "lesson_count": 2, "fleet_known": True, "in_fleet_count": 1,
            "retired_count": 1, "all_lessons_are_retired": False,
            "enforcement_active": False, "min_trades_for_a_verdict": 10,
            "min_trades_to_block": 25, "note": "",
        }
        html = render(payload)
        self.assertIn("Coins the fleet trades now", html)
        self.assertIn("Retired", html)
        self.assertLess(html.index("Coins the fleet trades now"), html.index("NEAR"))
        # and the banner only fires when EVERYTHING is retired
        self.assertNotIn("Every lesson here is from coins", html)

    def test_the_header_counts_both_groups(self):
        payload = {
            "lessons": [lesson("DOGE-USD", 12.80, in_fleet=False),
                        lesson("NEAR-USD", 0.5, in_fleet=True)],
            "lesson_count": 2, "fleet_known": True, "in_fleet_count": 1,
            "retired_count": 1, "all_lessons_are_retired": False,
            "enforcement_active": False, "min_trades_for_a_verdict": 10,
            "min_trades_to_block": 25, "note": "",
        }
        html = render(payload)
        self.assertIn("1 the fleet still trades", html)
        self.assertIn("1 retired", html)

    def test_an_older_server_without_tags_still_renders_every_lesson(self):
        payload = {"lessons": [lesson("DOGE-USD", 12.80), lesson("NEAR-USD", 0.5)],
                   "lesson_count": 2, "enforcement_active": False,
                   "min_trades_for_a_verdict": 10, "min_trades_to_block": 25, "note": ""}
        html = render(payload)
        self.assertIn("DOGE", html)
        self.assertIn("NEAR", html)
        # no fleet knowledge means no claim either way
        self.assertNotIn("the fleet still trades", html)
        self.assertNotIn("does not trade", html)

    def test_the_retired_note_is_escaped(self):
        bad = lesson("DOGE-USD", 1.0, in_fleet=False,
                     note='"><img src=x onerror="alert(1)">')
        payload = {"lessons": [bad], "lesson_count": 1, "fleet_known": True,
                   "in_fleet_count": 0, "retired_count": 1,
                   "all_lessons_are_retired": True, "enforcement_active": False,
                   "min_trades_for_a_verdict": 10, "min_trades_to_block": 25, "note": ""}
        html = render(payload)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_no_lessons_at_all_still_says_so(self):
        html = render({"lessons": [], "lesson_count": 0, "enforcement_active": False,
                       "min_trades_for_a_verdict": 10, "min_trades_to_block": 25})
        self.assertIn("Nothing learned yet", html)


if __name__ == "__main__":
    unittest.main()
