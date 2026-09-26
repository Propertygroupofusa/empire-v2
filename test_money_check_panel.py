"""The idle-money panel must not repeat the error it exists to catch.

The panel's own comment says an earmark is not an investment. One level
down it summed allocated_usd over branches holding a slice and printed
that as "genuinely in coin". On the live fleet, 2026-09-26:

    reported "genuinely in coin"   $276.92   <- allocation
    actually in coin               $ 93.07   <- entry x qty

A 3-level branch with one slice open has spent a third of its allocation.
It also rendered negative money as "$-381.47" and put a green tick on a
wallet overdrawn against its own branch reserves.
"""

import ast
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

BOT = Path(__file__).with_name("crypto_grid_bot.py")
HTML = Path(__file__).with_name("family_tree_dashboard.html")
SRC = BOT.read_text(encoding="utf-8")
TREE = ast.parse(SRC)
FN = next(n for n in ast.walk(TREE)
          if isinstance(n, ast.AsyncFunctionDef) and "idle" in n.name.lower()
          or isinstance(n, ast.AsyncFunctionDef) and "money" in n.name.lower())
FN_SRC = ast.get_source_segment(SRC, FN) or ""


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


class DeployedIsCoin(unittest.TestCase):
    def test_deployed_is_summed_from_slices_not_allocations(self):
        # The old line summed allocated_usd over branches with open slices.
        # Checked on the AST so the comment quoting the old behaviour
        # cannot satisfy it.
        bad = []
        for node in ast.walk(FN):
            if not isinstance(node, ast.Assign):
                continue
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if "deployed" not in targets:
                continue
            dumped = ast.dump(node.value)
            if "allocated_usd" in dumped:
                bad.append(node.lineno)
        self.assertFalse(bad, "deployed is assigned from allocated_usd at line(s) %s" % bad)

    def test_it_uses_entry_price_times_qty(self):
        self.assertIn("slice_cost", FN_SRC)
        self.assertIn("allocation_backing", FN_SRC,
                      "the cost definition must be shared, not re-implemented")

    def test_the_allocation_behind_slices_is_kept_but_named(self):
        self.assertIn("earmarked_behind_slices", FN_SRC)
        self.assertIn("earmarked_behind_slices_usd", FN_SRC,
                      "the gap between coin and earmark is the point - expose it")

    def test_unpriced_slices_are_counted_not_dropped(self):
        self.assertIn("unpriced_slices", FN_SRC)


class NegativeCash(unittest.TestCase):
    def test_negative_free_cash_is_its_own_finding(self):
        self.assertIn("cash_overdrawn", FN_SRC)
        self.assertIn("free_cash < 0", FN_SRC)

    def test_that_finding_is_marked_bad(self):
        # It must not fall through to cash_committed, which is an "ok".
        idx = FN_SRC.index("cash_overdrawn")
        block = FN_SRC[idx:idx + 900]
        self.assertIn('"severity": "bad"', block)

    def test_negative_cash_is_not_added_into_idle(self):
        self.assertIn("max(0.0, free_cash or 0.0)", FN_SRC,
                      "adding a negative free_cash to an earmark describes nothing")


class MoneyFormatting(unittest.TestCase):
    def test_the_server_has_one_money_formatter(self):
        self.assertIn("def _money(", FN_SRC)
        self.assertIn('"-$" if v < 0 else "$"', FN_SRC)

    def test_no_raw_dollar_interpolation_of_a_signed_figure_remains(self):
        # f"${free_cash:,.2f}" is the construct that produced "$-381.47".
        offenders = re.findall(r'\$\{(free_cash|deployable|deployed|total_idle)[^}]*:,\.2f\}', FN_SRC)
        self.assertFalse(offenders, "raw signed interpolation still present: %s" % offenders)

    def test_the_formatter_actually_puts_the_sign_first(self):
        ns = {}
        body = FN_SRC[FN_SRC.index("def _money("):]
        body = body[:body.index("\n    status =")]
        exec(body.replace("\n    ", "\n"), ns)
        self.assertEqual(ns["_money"](-381.47), "-$381.47")
        self.assertEqual(ns["_money"](381.47), "$381.47")
        self.assertEqual(ns["_money"](0), "$0.00")
        self.assertEqual(ns["_money"](None), "unknown")


class PanelIcons(unittest.TestCase):
    def setUp(self):
        self.node = shutil.which("node")
        if not self.node:
            raise unittest.SkipTest("node is not installed")

    def icon(self, finding, actionable):
        script = extract_js("moneyIcon") + (
            "\nprocess.stdout.write(moneyIcon(%s, %s));"
            % (json.dumps(finding), "true" if actionable else "false"))
        out = subprocess.run([self.node, "-e", script], capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            raise AssertionError(out.stderr)
        return out.stdout

    def test_a_bad_finding_never_shows_a_green_tick(self):
        self.assertNotEqual(self.icon({"severity": "bad"}, False), "✅")
        self.assertEqual(self.icon({"severity": "bad"}, False), "⚠️")

    def test_bad_beats_actionable(self):
        self.assertEqual(self.icon({"severity": "bad"}, True), "⚠️")

    def test_an_ok_finding_still_ticks(self):
        self.assertEqual(self.icon({"severity": "ok"}, False), "✅")

    def test_an_actionable_finding_is_a_bolt(self):
        self.assertEqual(self.icon({"severity": "warn"}, True), "⚡")

    def test_a_finding_with_no_severity_from_an_older_server_still_renders(self):
        self.assertEqual(self.icon({}, False), "✅")
        self.assertEqual(self.icon({}, True), "⚡")


class PanelHeader(unittest.TestCase):
    def test_the_header_prints_real_cash_not_a_composite(self):
        page = HTML.read_text(encoding="utf-8")
        head = page[page.index("actually in coin") - 400:page.index("actually in coin") + 400]
        self.assertIn("free_cash_usd", head,
                      "the header must show real cash, not earmark plus negative cash")
        self.assertNotIn("fmtUsd(d.idle_usd)", head)

    def test_every_figure_in_the_header_is_sign_safe(self):
        page = HTML.read_text(encoding="utf-8")
        i = page.index("actually in coin")
        head = page[i - 400:i + 400]
        self.assertNotIn("fmtUsd(", head, "fmtUsd renders -1 as $-1.00 here; use fmtSignedUsd")


if __name__ == "__main__":
    unittest.main()
