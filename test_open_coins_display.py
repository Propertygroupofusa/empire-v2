"""The Coinbase page says what the open coins are, not a loss.

The owner sent four parts of the page back as "fix this" on 2026-10-15:

  * "$-575.38" - the minus after the dollar sign;
  * "The two added together: $-127.36" - banked cash plus a price mark,
    read as the account's result;
  * "Total right now across all 19 branches ... $-575.38" - read as money
    gone, when nothing had been sold;
  * fifteen branch rows reading "↓ 0.00% buy at ..." and "Nearest: XLM
    needs 0.00% to buy" while the bot was refusing every one of those buys
    on purpose, plus the same red "no stop" warning on every row.

These run the page's own functions in node against the live numbers from
that day.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

HTML = Path(__file__).with_name("family_tree_dashboard.html")
PAGE = HTML.read_text(encoding="utf-8")


def extract(name, kind="function"):
    start = PAGE.index("%s %s" % (kind, name))
    if kind == "const":
        i = PAGE.index("{", start)
    else:
        i = PAGE.index("{", PAGE.index(")", start))
    depth, j = 0, i
    while j < len(PAGE):
        if PAGE[j] == "{":
            depth += 1
        elif PAGE[j] == "}":
            depth -= 1
            if depth == 0:
                end = j + 1
                if kind == "const":
                    end = PAGE.index(";", end) + 1
                return PAGE[start:end]
        j += 1
    raise AssertionError("unbalanced braces in %s" % name)


def run(expr, prelude=""):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    script = "\n".join([
        "const document = {getElementById: () => null};",
        extract("fmtUsd"), extract("fmtCoinPrice"), extract("escText"),
        extract("PROX_REFUSALS", "const"),
        "let proxReasons = {}; let proxReasonsOk = true;",
        extract("proxUnderLine"), extract("stopLine"),
        extract("renderGridRealizedTotalBanner"), extract("renderGridCloseAllBanner"),
        prelude,
        "process.stdout.write(String(%s));" % expr,
    ])
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return out.stdout


HISTORY = {"total_realized_pnl": 448.02, "total_trade_count": 293, "overall_win_rate": 88}
STATUS = {"total_unrealized_net_usd": -575.38}


class Money(unittest.TestCase):
    def test_a_negative_reads_minus_dollar(self):
        self.assertEqual(run("fmtUsd(-575.38)"), "-$575.38")
        self.assertEqual(run("fmtUsd(1234.5)"), "$1,234.50")
        self.assertEqual(run("fmtUsd(-0.004)"), "$0.00")

    def test_the_card_never_adds_banked_to_the_open_mark(self):
        html = run("renderGridRealizedTotalBanner(%s, %s)" % (json.dumps(HISTORY), json.dumps(STATUS)))
        self.assertIn("+$448.02", html)
        self.assertNotIn("added together", html)
        self.assertNotIn("127.36", html)      # 448.02 - 575.38
        self.assertNotIn("$-", html)
        self.assertIn("$575.38 under what was paid", html)
        self.assertIn("nothing has been sold", html)

    def test_an_unknown_mark_is_said_and_not_filled_in(self):
        html = run("renderGridRealizedTotalBanner(%s, {total_unrealized_net_usd: null})"
                   % json.dumps(HISTORY))
        self.assertIn("price unknown", html)

    def test_the_bottom_box_says_held_not_lost(self):
        html = run("renderGridCloseAllBanner({branches_with_open_slices: 19, total_unrealized_net_usd: -575.38})")
        self.assertIn("Coins held in 19 branches", html)
        self.assertIn("$575.38 under what was paid", html)
        self.assertIn("nothing is lost", html)
        self.assertNotIn("$-", html)
        self.assertIn("disabled", html)

    def test_the_close_all_button_still_turns_on_only_above_cost(self):
        html = run("renderGridCloseAllBanner({branches_with_open_slices: 2, total_unrealized_net_usd: 12.5})")
        self.assertIn("$12.50 over what was paid", html)
        self.assertNotIn(" disabled", html.split("grid-close-all-btn")[1].split(">")[0])


def row(**kw):
    base = {"b": {"product_id": "XLM-USD", "open_slices": 4, "stop_pct": 0.0, "slices": []},
            "cur": 0.1816, "openSlices": 4, "levels": 10, "under": True, "underPct": 7.17,
            "full": False, "refusal": None, "blocked": False}
    base.update(kw)
    return json.dumps(base)


class Rows(unittest.TestCase):
    def test_a_full_branch_says_why_it_is_not_buying(self):
        out = run("proxUnderLine(%s)" % row(openSlices=10, levels=10, full=True, blocked=True))
        self.assertIn("not buying - all rungs are full (10 of 10 rungs)", out)

    def test_an_over_full_branch_does_not_say_5_of_3(self):
        out = run("proxUnderLine(%s)" % row(openSlices=5, levels=3, full=True, blocked=True))
        self.assertIn("5 slices held on 3 rungs", out)
        self.assertNotIn("5 of 3", out)

    def test_a_logged_refusal_is_shown_in_plain_words(self):
        for kind, words in [("INVENTORY_DEPTH", "deeper than any that has recovered"),
                            ("EXITING", "marked to be exited"),
                            ("UNBACKED", "more coin than the wallet holds"),
                            ("CASH_RESERVE", "reserve floor")]:
            out = run("proxUnderLine(%s)" % row(refusal=kind, blocked=True))
            self.assertIn("not buying - ", out, kind)
            self.assertIn(words, out, kind)

    def test_no_reason_is_invented(self):
        out = run("proxUnderLine(%s)" % row())
        self.assertIn("a buy is due", out)
        self.assertIn("no reason to refuse", out)
        out = run("proxUnderLine(%s)" % row(), prelude="proxReasonsOk = false;")
        self.assertIn("could not be read", out)

    def test_a_row_above_its_buy_line_gets_no_extra_line(self):
        self.assertEqual(run("proxUnderLine(%s)" % row(under=False)), "")

    def test_the_no_stop_warning_is_said_once_not_per_row(self):
        self.assertIn("no stop on this branch", run("stopLine(%s)" % row()))
        self.assertEqual(run("stopLine(%s)" % row(noStopNoted=True)), "")


class Wiring(unittest.TestCase):
    def test_the_reasons_come_from_a_read(self):
        body = extract("refreshProxReasons")
        self.assertIn("apiGet('/family-tree-status/activity-feed", body)
        self.assertNotIn("POST", body)
        self.assertNotIn("fetch(", body)

    def test_the_panel_no_longer_prints_needs_0_00(self):
        body = extract("renderGridProximity")
        self.assertIn("proxUnderLine(r)", body)
        self.assertIn("a buy is due", body)
        self.assertIn("is at its ${closest.nextAction} line", body)
        self.assertNotIn("fetch(", body)

    def test_the_sum_is_gone_from_the_page(self):
        self.assertNotIn("The two added together", PAGE)
        self.assertNotIn("Total right now across all", PAGE)

    def test_the_old_study_is_labelled_as_the_old_study(self):
        self.assertIn("the FIRST study, not today's", PAGE)


if __name__ == "__main__":
    unittest.main()
