"""A branch reference is a coin price, not a dollar balance.

On the live page FLOKI (reference $0.00002965) and BONK ($0.00000376)
both rendered as "flat (ref $0.00)" - two of six branches showing no
reference at all, while the chart directly underneath printed the real
figure. fmtUsd is a balance formatter fixed at 2dp; fmtCoinPrice exists
for exactly this and was already used to fix the identical bug in the
proximity panel.
"""

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


def fmt(fn, value):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    script = extract("fmtUsd") + "\n" + extract("fmtCoinPrice") + \
        "\nprocess.stdout.write(String(%s(%r)));" % (fn, value)
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return out.stdout


class ReferencePrice(unittest.TestCase):
    def test_the_live_sub_cent_cases_no_longer_vanish(self):
        # FLOKI and BONK, exactly as the endpoint returns them.
        self.assertEqual(fmt("fmtUsd", 0.00002965), "$0.00")
        self.assertEqual(fmt("fmtUsd", 0.00000376), "$0.00")
        self.assertNotEqual(fmt("fmtCoinPrice", 0.00002965), "$0.00")
        self.assertNotEqual(fmt("fmtCoinPrice", 0.00000376), "$0.00")

    def test_the_digits_are_actually_readable(self):
        self.assertIn("2965", fmt("fmtCoinPrice", 0.00002965).replace(",", ""))
        self.assertIn("376", fmt("fmtCoinPrice", 0.00000376).replace(",", ""))

    def test_a_normal_price_is_unaffected(self):
        self.assertEqual(fmt("fmtCoinPrice", 84244.05), "$84,244.05")
        self.assertEqual(fmt("fmtCoinPrice", 4.8447), "$4.84")

    def test_the_branch_card_uses_the_price_formatter(self):
        page = HTML.read_text(encoding="utf-8")
        # The two reference renderings on the branch card header.
        refs = re.findall(r"\(ref \$\{(\w+)\(b\.reference_price\)\}", page)
        self.assertEqual(len(refs), 2, "expected both branch-card references, got %s" % refs)
        self.assertTrue(all(r == "fmtCoinPrice" for r in refs),
                        "a reference rendered with %s is a coin price in a balance "
                        "formatter" % set(refs))

    def test_no_reference_anywhere_still_uses_the_balance_formatter(self):
        page = HTML.read_text(encoding="utf-8")
        self.assertNotIn("fmtUsd(b.reference_price)", page)


if __name__ == "__main__":
    unittest.main()
