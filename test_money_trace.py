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
    fills                 1,944  -> ~972 implied round trips
    our books               249  -> about 74% never reached the ledger
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

    def test_the_reconciliation_gap_is_computed_and_shown(self):
        html = render(LIVE)
        # 1944 fills -> 972 implied round trips, 249 recorded, ~723 missing
        self.assertIn("~723", html)
        self.assertIn("74%", html)
        self.assertIn("Never reached our ledger", html)

    def test_a_clean_ledger_shows_no_gap_row(self):
        clean = json.loads(json.dumps(LIVE))
        clean["statement"]["fills"] = 500
        clean["our_ledgers"]["combined_trades"] = 250
        html = render(clean)
        self.assertNotIn("Never reached our ledger", html)

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
