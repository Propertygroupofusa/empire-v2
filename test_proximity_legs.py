"""Both triggers, not just the nearer one.

The panel printed a single percentage - whichever trigger was closer - so
a branch holding slices looked like it had one thing it could do. Live BTC
on 2026-09-26 needed a 2.38% FALL to buy and a 2.41% RISE to sell: two
live triggers in opposite directions, one of them not on screen. The
account owner had to ask what the numbers under the dot were.
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

HTML = Path(__file__).with_name("family_tree_dashboard.html")


def extract(name):
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


def render(row, move_cls="", arrow="↓"):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    script = "\n".join([
        extract("fmtCoinPrice"), extract("bothLegs"),
        "process.stdout.write(bothLegs(%s, %s, %s));"
        % (json.dumps(row), json.dumps(move_cls), json.dumps(arrow)),
    ])
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return out.stdout


# Live BTC: holds slices, both triggers active, sell marginally further.
BTC = {"cur": 84140.32, "buyAt": 82137.949, "sellAt": 86165.59, "nextAction": "buy"}
# Live TIA: flat, so there is nothing to sell.
TIA = {"cur": 0.5107, "buyAt": 0.50154, "sellAt": None, "nextAction": "buy"}
# Live NEAR: holds slices, sell much further away.
NEAR = {"cur": 4.8473, "buyAt": 4.7235825, "sellAt": 5.094045, "nextAction": "buy"}


def pcts(html):
    return [float(x) for x in re.findall(r"([\d.]+)%", html)]


class BothLegs(unittest.TestCase):
    def test_a_branch_with_slices_shows_two_distances(self):
        got = pcts(render(BTC))
        self.assertEqual(len(got), 2, got)
        self.assertAlmostEqual(got[0], 2.38, places=2)   # fall to buy
        self.assertAlmostEqual(got[1], 2.41, places=2)   # rise to sell

    def test_both_prices_are_named(self):
        html = render(BTC)
        self.assertIn("buy at", html)
        self.assertIn("sell at", html)
        self.assertIn("82,137.95", html)
        self.assertIn("86,165.59", html)

    def test_the_directions_are_distinguishable(self):
        html = render(BTC)
        self.assertIn("↓", html)   # fall to buy
        self.assertIn("↑", html)   # rise to sell

    def test_the_nearer_leg_is_listed_first(self):
        sell_first = dict(NEAR, nextAction="sell")
        html = render(sell_first)
        self.assertLess(html.index("sell at"), html.index("buy at"))
        html = render(NEAR)   # nextAction buy
        self.assertLess(html.index("buy at"), html.index("sell at"))

    def test_a_flat_branch_says_so_rather_than_leaving_a_blank(self):
        html = render(TIA)
        self.assertIn("no slice to sell", html)
        self.assertEqual(len(pcts(html)), 1, "a flat branch has exactly one distance")

    def test_a_distance_never_renders_negative(self):
        # Price already past a trigger: the honest reading is zero to go,
        # not a negative number that looks like a different direction.
        past = {"cur": 90000.0, "buyAt": 82137.949, "sellAt": 86165.59,
                "nextAction": "sell"}
        self.assertTrue(all(p >= 0 for p in pcts(render(past))), render(past))

    def test_near_shows_the_real_asymmetry(self):
        got = pcts(render(NEAR))
        self.assertAlmostEqual(got[0], 2.55, places=2)
        self.assertAlmostEqual(got[1], 5.09, places=2)
        self.assertGreater(got[1], got[0] * 1.9,
                           "NEAR's sell is nearly twice as far as its buy - the "
                           "asymmetry the single-number panel hid")

    def test_the_panel_calls_it(self):
        page = HTML.read_text(encoding="utf-8")
        self.assertIn("${bothLegs(r, moveCls, arrow)}", page)

    def test_the_caption_explains_the_dot(self):
        page = HTML.read_text(encoding="utf-8")
        self.assertIn("that dot's price", page)
        self.assertIn("must FALL to buy", page)
        self.assertIn("must RISE to sell", page)


if __name__ == "__main__":
    unittest.main()
