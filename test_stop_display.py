"""The stop has to be visible where the other triggers are.

The proximity panel showed a branch's buy and sell triggers and said
nothing about the level that force-sells it. On the live fleet that level
ranged from 4.53% to 18.49% depending on the coin, and on one
configuration it can be absent entirely.
"""

import json
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


def render(row):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    script = "\n".join([
        extract("fmtUsd"), extract("fmtCoinPrice"), extract("escText"),
        extract("stopLine"),
        "process.stdout.write(stopLine(%s));" % json.dumps(row),
    ])
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return out.stdout


# NEAR as the live endpoint returns it.
NEAR = {
    "cur": 4.8288,
    "b": {"product_id": "NEAR-USD", "stop_pct": 0.184887, "stop_source": "adaptive",
          "stop_daily_vol_pct": 7.3955, "open_slices": 2,
          "stop_reason": "18.49% stop for NEAR-USD, 2.50x its own 7.40% daily volatility",
          "slices": [{"entry_price": 4.9698}, {"entry_price": 4.8447}]},
}


class StopLine(unittest.TestCase):
    def test_it_shows_the_price_the_stop_sits_at(self):
        html = render(NEAR)
        # The NEAREST stop is the highest of the per-slice levels -
        # 4.9698 * (1 - 0.184887) = 4.0510 - because that is the one price
        # reaches first. Rendered through fmtCoinPrice, which is 2dp above
        # a dollar.
        self.assertIn("stop $4.05", html)
        # and NOT the lower, further-away slice's level
        self.assertNotIn("$3.94", html)

    def test_it_shows_how_much_room_is_left(self):
        html = render(NEAR)
        self.assertIn("% of room", html)
        self.assertIn("16.1", html)  # (4.8288 - 4.0510) / 4.8288

    def test_an_adaptive_stop_names_the_volatility_it_came_from(self):
        html = render(NEAR)
        self.assertIn("18.49%", html)
        self.assertIn("7.40% daily vol", html)

    def test_a_fixed_stop_says_fixed_rather_than_inventing_a_volatility(self):
        row = json.loads(json.dumps(NEAR))
        row["b"]["stop_source"] = "fixed"
        row["b"]["stop_pct"] = 0.08
        row["b"]["stop_reason"] = "8.00% fixed stop for NEAR-USD"
        html = render(row)
        self.assertIn("fixed", html)
        # Assert on the VISIBLE text, not the tooltip - the reason string
        # is server-composed and may legitimately mention anything.
        visible = html.split("</span>")[-2]
        self.assertNotIn("daily vol", visible)
        self.assertIn("8.00%", visible)

    def test_no_stop_at_all_is_shown_as_a_warning_not_a_blank(self):
        row = json.loads(json.dumps(NEAR))
        row["b"]["stop_pct"] = 0
        html = render(row)
        self.assertIn("no stop on this branch", html)
        self.assertIn("without limit", html)
        self.assertIn("var(--red)", html)

    def test_a_flat_branch_shows_nothing(self):
        row = json.loads(json.dumps(NEAR))
        row["b"]["open_slices"] = 0
        self.assertEqual(render(row), "")

    def test_an_older_server_with_no_stop_field_renders_nothing(self):
        row = json.loads(json.dumps(NEAR))
        del row["b"]["stop_pct"]
        self.assertEqual(render(row), "")

    def test_the_reason_is_escaped_into_the_tooltip(self):
        row = json.loads(json.dumps(NEAR))
        row["b"]["stop_reason"] = '"><img src=x onerror="alert(1)">'
        html = render(row)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_the_panel_actually_calls_it(self):
        self.assertIn("${stopLine(r)}", HTML.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
