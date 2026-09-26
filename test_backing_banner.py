"""The backing banner must RENDER the gap, not just receive it.

The panel exists because a $381.41 hole was legible only as a minus sign
in a subtitle. A banner that silently fails to draw would put it right
back where it was, so these run the real renderer out of the page under
node with a stubbed DOM.
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

import allocation_backing as A

HTML = Path(__file__).with_name("family_tree_dashboard.html")


def extract(name):
    src = HTML.read_text(encoding="utf-8")
    start = src.index("function %s(" % name)
    i = src.index("{", start)
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
        j += 1
    raise AssertionError("unbalanced braces in %s" % name)


def render(bk):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    script = "\n".join([
        extract("fmtUsd"),
        extract("escText"),
        extract("renderBackingBanner"),
        "const el = { innerHTML: '', style: {} };",
        "globalThis.document = { getElementById: id => (id === 'backing-banner' ? el : null) };",
        "renderBackingBanner(%s);" % json.dumps(bk),
        "process.stdout.write(JSON.stringify({html: el.innerHTML, display: el.style.display}));",
    ])
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError("renderer threw:\n%s" % out.stderr)
    return json.loads(out.stdout)


LIVE = A.backing([
    {"allocated_usd": 69.23, "slices": [{"entry_price": 84063.99, "qty": 8.235e-05},
                                        {"entry_price": 84244.05, "qty": 0.0001187}]},
    {"allocated_usd": 207.69, "slices": [{"entry_price": 4.9698, "qty": 1.392},
                                         {"entry_price": 4.8447, "qty": 14.289}]},
    {"allocated_usd": 69.23}, {"allocated_usd": 69.23},
    {"allocated_usd": 69.23}, {"allocated_usd": 69.23},
], 79.36)


class Banner(unittest.TestCase):
    def test_the_live_hole_is_drawn_with_all_three_figures(self):
        r = render(LIVE)
        self.assertEqual(r["display"], "block")
        for figure in ("$553.84", "$172.43", "$381.41"):
            self.assertIn(figure, r["html"], "missing %s" % figure)
        self.assertIn("68.9%", r["html"])

    def test_it_says_plainly_what_is_wrong(self):
        r = render(LIVE)
        self.assertIn("money that is not there", r["html"])

    def test_a_backed_fleet_shows_nothing_at_all(self):
        bk = A.backing([{"allocated_usd": 100.0,
                         "slices": [{"entry_price": 50.0, "qty": 1.0}]}], 50.0)
        r = render(bk)
        self.assertEqual(r["display"], "none")
        self.assertEqual(r["html"], "")

    def test_drifting_is_shown_but_not_as_an_alarm(self):
        bk = A.backing([{"allocated_usd": 100.0,
                         "slices": [{"entry_price": 50.0, "qty": 1.0}]}], 44.0)
        r = render(bk)
        self.assertEqual(r["display"], "block")
        self.assertIn("drifting", r["html"].lower())
        self.assertNotIn("money that is not there", r["html"])

    def test_unknown_never_renders_as_all_clear(self):
        r = render(A.backing(LIVE and [{"allocated_usd": 1.0}], None))
        self.assertEqual(r["display"], "block")
        self.assertIn("unchecked", r["html"].lower())

    def test_missing_block_hides_rather_than_throwing(self):
        for bad in (None, {}):
            r = render(bad)
            self.assertEqual(r["display"], "none", "%r should hide" % bad)

    def test_the_detail_text_is_escaped(self):
        bk = dict(LIVE)
        bk["detail"] = '<img src=x onerror="alert(1)">'
        r = render(bk)
        self.assertNotIn("<img", r["html"])
        self.assertIn("&lt;img", r["html"])

    def test_the_page_actually_calls_it(self):
        src = HTML.read_text(encoding="utf-8")
        # Called from the stat-strip update, and the element it writes to
        # exists in the markup - either one missing makes the panel invisible.
        self.assertIn("renderBackingBanner(d.allocation_backing)", src)
        self.assertIn('id="backing-banner"', src)

    def test_the_subtitle_no_longer_leads_with_negative_cash(self):
        src = HTML.read_text(encoding="utf-8")
        # The exact string the owner underlined. It may survive only inside
        # the fallback for an older server that sends no backing block.
        self.assertIn("in coin · $", src)
        body = src[src.index("renderBackingBanner(d.allocation_backing)"):]
        first = body[:900]
        self.assertIn("bk.deployed_coin_usd", first)


if __name__ == "__main__":
    unittest.main()
