"""The cumulative realized-P&L curve has to say what it is drawing.

Two numbers on the dashboard looked contradictory to the account owner:
the header's "+$20.16 taken plus still open" and this chart's "+$19.55
all-time realized". They are not contradictory - the difference is the
open slices, which this chart deliberately excludes - but nothing on the
chart said so, and its left edge started at -$1.14 with no explanation.

These tests EXECUTE the real renderer out of the HTML (node, stub DOM)
rather than reading its source, because the last time a panel broke it
was source-reading tests that missed it.
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

HTML = Path(__file__).with_name("family_tree_dashboard.html")


def extract(name: str) -> str:
    """Pull one top-level `function name(...) { ... }` out of the page."""
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
    raise AssertionError("unbalanced braces reading %s" % name)


def build_fixture():
    """50 trades summing to +20.69, on a ledger of 82 worth +19.55.

    Those are the real shapes the live endpoint returns: the 32 trades
    the feed does not send sum to -1.14, which is where the line starts.
    """
    # Deliberately peaks and then gives some back, so the top axis label
    # is NOT the same number as the line's end - which is the real shape,
    # and the one that made the two screens look contradictory.
    pnls = [0.41] * 48 + [1.20]
    pnls.append(round(20.69 - sum(pnls), 2))
    trades = [
        {
            "product_id": "NEAR-USD",
            "pnl": p,
            "closed_at": "2026-09-%02dT0%d:00:00Z" % (1 + i // 8, i % 8),
        }
        for i, p in enumerate(pnls)
    ]
    return {
        "total_trade_count": 82,
        "total_realized_pnl": 19.55,
        "recent_trades": trades,
    }, pnls


def series(pnls, total=19.55):
    """The cumulative the chart should be drawing, rebuilt independently."""
    run = total - round(sum(pnls), 2)
    ys = [run]
    for p in pnls:
        run += p
        ys.append(run)
    return ys


def render(history):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    script = "\n".join([
        extract("fmtUsd"),
        extract("fmtSignedUsd"),
        extract("renderGridPnlCurve"),
        "const el = { innerHTML: '' };",
        "globalThis.document = { getElementById: id => (id === 'grid-pnl-curve' ? el : null) };",
        "renderGridPnlCurve(%s);" % json.dumps(history),
        "process.stdout.write(el.innerHTML);",
    ])
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError("renderer threw:\n%s" % out.stderr)
    return out.stdout


class CurveLabels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.history, cls.pnls = build_fixture()
        cls.html = render(cls.history)

    def test_renderer_did_not_fall_into_its_own_catch(self):
        self.assertNotIn("Could not draw", self.html)
        self.assertIn("<svg", self.html)

    def test_line_ends_on_the_all_time_realized_figure(self):
        # Rebuild the geometry independently and check the last plotted
        # point is the all-time total, not the window's own sum (+20.69).
        W, H, padL, padR, padT, padB = 320, 150, 40, 10, 10, 20
        ys = series(self.pnls)
        lo, hi = min(ys + [0]), max(ys + [0])
        pad = (hi - lo) * 0.1
        lo, hi = lo - pad, hi + pad
        expect_y = padT + (1 - (ys[-1] - lo) / (hi - lo)) * (H - padT - padB)

        # The stroked line only - the area path behind it closes back down
        # to the baseline and would otherwise be read as the last point.
        line = re.search(r'<path d="([^"]+)" fill="none"', self.html)
        self.assertIsNotNone(line, "stroked line path not found")
        pts = re.findall(r"[ML]([\d.]+),(-?[\d.]+)", line.group(1))
        self.assertEqual(len(pts), len(ys), "plotted point count changed")
        self.assertAlmostEqual(float(pts[-1][1]), expect_y, delta=0.15)

        # And that end is the all-time figure, not the window's own sum.
        self.assertAlmostEqual(ys[-1], 19.55, delta=0.005)
        self.assertNotAlmostEqual(round(sum(self.pnls), 2), 19.55, delta=0.005)

    def test_axis_labels_are_the_real_series_bounds(self):
        ys = series(self.pnls)

        def money(v):
            v = round(v, 2)
            return ("-$%.2f" % abs(v)) if v < 0 else ("$%.2f" % v)

        # The bottom label is a real running total (the trades the feed
        # does not send), not a fudge, and the top is the series' peak -
        # which is above where the line ends.
        self.assertIn(">%s<" % money(min(ys + [0])), self.html)
        self.assertIn(">%s<" % money(max(ys + [0])), self.html)
        self.assertGreater(max(ys), ys[-1], "fixture must peak above its end")

    def test_caption_explains_the_left_edge(self):
        # Without this the reader cannot tell why a chart of profit opens
        # below zero.
        self.assertIn("32 earlier", self.html.replace("\n", " "))
        self.assertIn("-$1.14", self.html)

    def test_caption_says_open_slices_are_excluded(self):
        # This is the whole reason the chart's end (+19.55) sits below the
        # header's figure. Assert on the rendered caption, not on source,
        # so an explanatory comment cannot satisfy it.
        caption = self.html.split("</svg>", 1)[1]
        flat = " ".join(caption.split())
        self.assertIn("Closed money only", flat)
        self.assertIn("open slices are not in this line", flat)
        self.assertIn("+$19.55", flat)

    def test_short_ledger_does_not_claim_missing_trades(self):
        hist = dict(self.history)
        hist["total_trade_count"] = len(hist["recent_trades"])
        hist["total_realized_pnl"] = round(sum(self.pnls), 2)
        html = render(hist)
        flat = " ".join(html.split("</svg>", 1)[1].split())
        self.assertIn("All 50 closed round trips", flat)
        self.assertNotIn("earlier", flat)


if __name__ == "__main__":
    unittest.main()
