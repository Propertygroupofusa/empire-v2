"""The Alpaca account and the Coinbase account are never combined.

REPLACES test_combined_equity_gap.py, 2026-10-05. That file tested
combine_equity() - that a missing side was never silently added in as
$0.00. The function it guarded no longer exists, because the account
owner ruled out the whole idea it implemented:

    "Alpaca and Coinbase are two different things. I don't want anything
     that has to do with them combined. Make sure nowhere in the codes or
     nowhere anywhere Coinbase and Alpaca is combined. There are never to
     combine those, never to combine the codes. They are separate within
     their own. They have their own, everything is separate."

Deleting the old test would have left nothing standing where a real rule
now is. So it is inverted: instead of checking that the sum handles a
missing side correctly, these check that THERE IS NO SUM.

What was removed, and what each file must stay clear of:
  routers/trading_dashboard.py  combine_equity, COMBINED_GOAL_USD,
                                _log_combined_equity_snapshot_if_due,
                                _project_years_to_goal,
                                _decompose_combined_delta,
                                _build_progress_observations,
                                GET /combined-equity-progress, and the
                                Alpaca equity fetch that sat inside the
                                Coinbase endpoint
  routers/bot_race.py           combined_balance, leader, spread
  capital_census.py             verified_usd_cash (one cash total across
                                both venues)
  family_tree_dashboard.html    the Combined Progress panel
  alpaca_dashboard.html         the same panel
  bot_race_dashboard.html       Total Capital (Both), Average Progress,
                                Alpaca vs Crypto Lead, the leader badges

Each account keeps its own tracking, which is what these also assert:
Alpaca via GET /alpaca-overview, Coinbase via GET /growth-model.
"""
import pathlib
import re
import unittest

HERE = pathlib.Path(__file__).parent


def src(*parts):
    return (HERE.joinpath(*parts)).read_text()


def code_lines(text, comment_markers=("#", "//", "<!--", "*")):
    """Source lines with comments and blank lines dropped.

    Every removal in this change left a comment behind explaining it, and
    those comments necessarily contain the words they are explaining. A
    naive substring search over the raw file matches its own epitaph - the
    exact mistake that let three mutations through an earlier test in this
    codebase. So the assertions below run over CODE only.
    """
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if any(line.startswith(m) for m in comment_markers):
            continue
        line = re.sub(r'/\*.*?\*/', '', line)
        line = re.sub(r'<!--.*?-->', '', line)
        out.append(line)
    return out


class NothingAddsTheTwoAccountsTogether(unittest.TestCase):

    FILES = [
        ("routers", "trading_dashboard.py"),
        ("routers", "bot_race.py"),
        ("capital_census.py",),
        ("family_tree_dashboard.html",),
        ("alpaca_dashboard.html",),
        ("bot_race_dashboard.html",),
    ]

    # one account's figure plus the other's, in either order
    SUM = re.compile(
        r'(alpaca[A-Za-z_]*\s*\+\s*(crypto|coinbase|grid)[A-Za-z_]*'
        r'|(crypto|coinbase|grid)[A-Za-z_]*\s*\+\s*alpaca[A-Za-z_]*)',
        re.IGNORECASE)

    def test_no_file_adds_an_alpaca_figure_to_a_coinbase_one(self):
        for parts in self.FILES:
            with self.subTest(file="/".join(parts)):
                for n, line in enumerate(code_lines(src(*parts)), 1):
                    m = self.SUM.search(line)
                    self.assertIsNone(
                        m, f"{'/'.join(parts)} adds the two accounts: {line[:120]!r}")

    def test_the_removed_symbols_do_not_come_back(self):
        gone = ["combine_equity", "COMBINED_GOAL_USD", "combined_equity",
                "combined_balance", "_log_combined_equity_snapshot_if_due",
                "_decompose_combined_delta", "_project_years_to_goal",
                "loadCombinedProgress", "renderCombinedProgress",
                "buildCombinedEquitySparklineSvg", "combined-equity-progress"]
        for parts in self.FILES:
            text = "\n".join(code_lines(src(*parts)))
            for name in gone:
                with self.subTest(file="/".join(parts), symbol=name):
                    self.assertNotIn(
                        name, text,
                        f"{'/'.join(parts)} brought back {name}")

    def test_the_coinbase_endpoint_does_not_fetch_alpaca(self):
        """get_family_tree_status used to reach into the stock account."""
        text = "\n".join(code_lines(src("routers", "trading_dashboard.py")))
        i = text.index("async def get_family_tree_status")
        j = text.index("\ndef ", i + 10)
        body = text[i:j]
        for probe in ("ALPACA_BASE_URL", "ALPACA_HEADERS", "alpaca_equity"):
            self.assertNotIn(probe, body,
                             f"the Coinbase endpoint still touches {probe}")

    def test_the_census_reports_cash_per_venue_not_as_one_total(self):
        text = "\n".join(code_lines(src("capital_census.py")))
        self.assertIn("verified_usd_cash_by_venue", text)
        self.assertNotIn('"verified_usd_cash"', text)

    def test_the_retired_table_is_documented_but_unused(self):
        """The rows stay; the code that fed them does not."""
        models = src("models.py")
        self.assertIn("class CombinedEquitySnapshot", models,
                      "the table was dropped - its historical rows are real "
                      "and this codebase does not destroy recorded history")
        self.assertIn("RETIRED", models)
        router = "\n".join(code_lines(src("routers", "trading_dashboard.py")))
        self.assertNotIn("CombinedEquitySnapshot", router,
                         "the router is writing or reading the retired table again")


class EachAccountStillTracksItself(unittest.TestCase):
    """Separating them must not have left either side with nothing."""

    def test_alpaca_keeps_its_own_goal_and_progress(self):
        text = src("routers", "trading_dashboard.py")
        i = text.index('@router.get("/alpaca-overview")')
        body = text[i:i + 30000]
        self.assertIn("progress_to_goal_pct", body)
        self.assertIn('"goal"', body)

    def test_coinbase_keeps_its_own_growth_model(self):
        text = src("routers", "trading_dashboard.py")
        self.assertIn('@router.get("/growth-model")', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
