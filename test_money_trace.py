"""Where the cash went, from the venue record rather than our own books.

The page showed "-$381.47 cash" and nothing else, and the question it
prompts is "where did that money go". The honest answer has two halves
and neither was on screen: cash that became coin is still held and is not
a loss, while commission is gone for good.

The live 56-day window, read from Coinbase's own fills:

    bought          $49,980.16
    sold            $43,332.16
    commission      $ 1,854.53   <- the only part actually gone
    net cash flow   -$8,502.53   <- became coin, still held
    fills                 1,944

The last two lines of this header used to read "-> ~972 implied round
trips / about 74% never reached the ledger". Both were wrong, and this
file asserted them, which is why the panel went on showing 74% in red for
hours after the figure was retracted everywhere else. Fills are not orders
and orders are not round trips; measured from distinct SELL orders on spot
pairs the gap is 32 of 281 closes, 11.4%. The tests below assert the shape
of an honest answer rather than a specific wrong number.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

HTML = Path(__file__).with_name("family_tree_dashboard.html")

LIVE = {
    "available": True,
    "source": "Coinbase /orders/historical/fills - the exchange's own record",
    "window": {"start": "2026-08-01T00:00:00Z", "end": "2026-09-26T23:59:59Z"},
    "statement": {"fills": 1944, "bought_usd": 49980.16, "sold_usd": 43332.16,
                  "commission_usd": 1854.53, "net_cash_flow_usd": -8502.53,
                  "note": "net_cash_flow_usd is CASH, not profit."},
    "our_ledgers": {"tree_realized_pnl": -508.44, "tree_trades": 167,
                    "grid_realized_pnl": 19.55, "grid_trades": 82,
                    "combined_realized_pnl": -488.89, "combined_trades": 249},
    "truncated": False,
}


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


def render(payload):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    script = "\n".join([
        extract("escText"), extract("renderMoneyTrace"),
        "const el = { innerHTML: '' };",
        "globalThis.document = { getElementById: id => (id === 'money-trace-wrap' ? el : null) };",
        "renderMoneyTrace(%s);" % json.dumps(payload),
        "process.stdout.write(el.innerHTML);"])
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return out.stdout


class MoneyTrace(unittest.TestCase):
    def test_every_live_figure_is_on_screen(self):
        html = render(LIVE)
        for fig in ("$49,980.16", "$43,332.16", "$1,854.53", "$8,502.53"):
            self.assertIn(fig, html, "missing %s" % fig)

    def test_commission_is_the_part_called_gone(self):
        html = render(LIVE)
        i = html.index("Commission")
        self.assertIn("gone", html[i:i + 400])
        self.assertIn("var(--red)", html[i:i + 500])

    def test_cash_becoming_coin_is_NOT_called_a_loss(self):
        html = render(LIVE)
        i = html.index("Net cash flow")
        seg = html[i:i + 400]
        self.assertIn("not a loss", seg)
        self.assertIn("still held", seg)

    # THIS TEST USED TO ASSERT THE BUG.
    #
    # It required "~723" and "74%" on the page - the figures the panel got
    # from fills/2. That arithmetic was retracted on 2026-09-26 (it counted
    # Coinbase event contracts the bots never traded, and counted fills
    # rather than orders), but this test kept demanding it, so the panel
    # went on telling the account owner in red that his books described a
    # quarter of reality. They describe 89% of it.
    #
    # A test that asserts the same arithmetic as the code cannot catch the
    # code being wrong. These assert the SHAPE of an honest answer instead:
    # the panel reads a reconciliation block, and says so when there isn't
    # one rather than computing a number itself.

    def test_the_panel_no_longer_does_its_own_arithmetic(self):
        src = HTML.read_text(encoding="utf-8")
        body = "\n".join(l for l in src.split("\n")
                          if not l.strip().startswith("//"))
        for gone in ("const implied", "const missing", "missPct"):
            self.assertNotIn(gone, body,
                             "%s is live arithmetic the panel must not do" % gone)
        self.assertNotIn("Never reached our ledger", body)

    def test_it_reports_the_order_based_gap_when_given_one(self):
        d = json.loads(json.dumps(LIVE))
        d["reconciliation"] = {
            "closes_at_exchange": 281,
            "our_recorded_round_trips": 249,
            "gap": 32, "gap_pct": 11.4,
            "spot": {"fills": 1019, "orders": 670, "commission_usd": 428.01},
            "event_contracts": {"fills": 932, "products": 107,
                                "commission_usd": 1428.96,
                                "note": "Coinbase event contracts, not spot."},
        }
        html = render(d)
        self.assertIn("281", html)          # closes at the exchange
        self.assertIn("249", html)          # rows we recorded
        self.assertIn("11.4%", html)        # the real gap
        self.assertNotIn("74%", html.replace(
            "said 74% - that counted event contracts", ""))
        self.assertIn("Spot closes at the exchange", html)

    def test_event_contracts_are_shown_as_excluded_not_hidden(self):
        d = json.loads(json.dumps(LIVE))
        d["reconciliation"] = {
            "closes_at_exchange": 281, "our_recorded_round_trips": 249,
            "gap": 32, "gap_pct": 11.4,
            "spot": {"fills": 1019, "orders": 670},
            "event_contracts": {"fills": 932, "products": 107,
                                "commission_usd": 1428.96,
                                "note": "Coinbase event contracts, not spot."},
        }
        html = render(d)
        self.assertIn("Event contracts, excluded", html)
        self.assertIn("932", html)
        self.assertIn("$1,428.96", html)

    def test_with_no_reconciliation_block_it_claims_nothing(self):
        html = render(LIVE)          # the live fixture has no block
        self.assertIn("Reconciliation unavailable", html)
        self.assertNotIn("Never reached our ledger", html)
        self.assertNotIn("74%", html)

    def test_both_ledgers_are_broken_out(self):
        html = render(LIVE)
        self.assertIn("-$508.44", html)
        self.assertIn("$19.55", html)
        self.assertIn("-$488.89", html)

    def test_a_negative_renders_sign_first(self):
        html = render(LIVE)
        self.assertNotIn("$-", html)

    def test_truncation_is_declared_as_a_floor(self):
        t = json.loads(json.dumps(LIVE))
        t["truncated"] = True
        self.assertIn("these totals are a floor", render(t))
        self.assertNotIn("these totals are a floor", render(LIVE))

    def test_an_unavailable_record_says_so_rather_than_showing_zeros(self):
        html = render({"available": False, "error": "Coinbase returned 429"})
        self.assertIn("Could not read the exchange record", html)
        self.assertIn("429", html)
        self.assertNotIn("$0.00", html)

    def test_the_error_text_is_escaped(self):
        html = render({"available": False, "error": '<img src=x onerror="alert(1)">'})
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_the_page_wires_it(self):
        page = HTML.read_text(encoding="utf-8")
        self.assertIn('id="money-trace-wrap"', page)
        self.assertIn("runMoneyTrace()", page)
        self.assertIn("/coinbase-statement?start=", page)


if __name__ == "__main__":
    unittest.main()
