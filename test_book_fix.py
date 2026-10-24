"""The one-time XLM/XRP book correction: it writes off only what Coinbase no
longer holds on those two branches, can reach no other, and runs once.

Run as written: python3 test_book_fix.py

The fixture is the live book as served 2026-10-24: XLM-USD 2,730.52237212
tracked against 1,672.87001782 owned, XRP-USD 855.86121 against
813.807894, and ETH-USD 0.19440136 against a census reading of 0.0 - the ETH
is staked, and a staked balance is invisible to this key. The ETH tests are
the reason this module exists instead of the fleet-wide reconcile.

These drive the REAL book_fix and the REAL startup_fix.apply_reconcile
against a fake session that answers select(X).where(X.col == v) by reading
the statement, so the modules' own queries run unmodified.
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, ".")
import book_fix  # noqa: E402
import startup_fix  # noqa: E402
from models import CryptoGridSlice, TradingBotState  # noqa: E402

startup_fix.READY_TRIES = 2
startup_fix.READY_SLEEP_SECONDS = 0.0


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class FakeDB:
    def __init__(self, store, log):
        self.store, self.log = store, log

    async def execute(self, stmt):
        ent = stmt.column_descriptions[0]["entity"]
        w = stmt.whereclause
        col, val = w.left.key, w.right.value
        self.log.append(("select", ent.__name__, col, val))
        return FakeResult([r for r in self.store.get(ent.__name__, [])
                           if getattr(r, col, None) == val])

    def add(self, row):
        self.log.append(("add", type(row).__name__))
        self.store.setdefault(type(row).__name__, []).append(row)

    async def delete(self, row):
        self.log.append(("delete", type(row).__name__, getattr(row, "id", None)))
        rows = self.store.get(type(row).__name__, [])
        if row in rows:
            rows.remove(row)

    async def commit(self):
        self.log.append(("commit",))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeGrid:
    def __init__(self, status, store):
        self.status, self.store = status, store
        self.status_calls = 0
        self.log, self.activity = [], []

    async def get_grid_status(self):
        self.status_calls += 1
        return self.status

    def get_session_factory(self):
        store, log = self.store, self.log
        return lambda: FakeDB(store, log)

    async def _log_activity_safe(self, *a, **k):
        self.activity.append(a)


XLM = [(129, 997.7255959, 0.222544, "2026-09-30T14:30:04.875945Z"),
       (132, 1055.1606138, 0.222677, "2026-10-01T08:15:24.733875Z"),
       (158, 329.97757659, 0.214954, "2026-10-05T14:57:35.597981Z"),
       (172, 347.65858583, 0.204022, "2026-10-07T02:03:31.849437Z")]
XRP = [(10, 16.849838483774, 1.5383, "2026-09-27T12:01:28.870262Z"),
       (11, 86.675767622267, 1.5383, "2026-09-27T12:01:28.870289Z"),
       (12, 86.675767622267, 1.5383, "2026-09-27T12:01:28.870300Z"),
       (58, 27.784321423897, 1.5282, "2026-09-27T13:08:32.190968Z"),
       (59, 27.784321423897, 1.5282, "2026-09-27T13:08:32.191053Z"),
       (60, 27.784321423898, 1.5282, "2026-09-27T13:08:32.191068Z"),
       (139, 155.940461, 1.4982, "2026-10-02T17:17:25.979385Z"),
       (182, 139.637552, 1.4678, "2026-10-07T08:14:18.408935Z"),
       (190, 142.126066, 1.4421, "2026-10-07T13:11:08.991934Z"),
       (196, 144.602793, 1.4174, "2026-10-07T17:43:06.236576Z")]
ETH = [(137, 0.0949319, 2699.83, "2026-10-02T15:34:24.510569Z"),
       (179, 0.04940363, 2609.12, "2026-10-07T04:47:10.703844Z"),
       (186, 0.05006583, 2574.61, "2026-10-07T11:35:25.690551Z")]
OWNED = {"XLM": 1672.87001782, "XRP": 813.807894, "ETH": 0.0, "BTC": 0.0182432}


def _branch(pid, bot, price, rows):
    return {"product_id": pid, "bot_name": bot, "current_price": price,
            "slices": [{"id": i, "qty": q, "entry_price": p, "opened_at": t}
                       for i, q, p, t in rows]}


def live_status():
    return {"branches": [_branch("XLM-USD", "crypto_grid_10", 0.16932, XLM),
                         _branch("ETH-USD", "crypto_grid_8", 2342.16, ETH),
                         _branch("XRP-USD", "crypto_grid_6", 1.2733, XRP)]}


def fresh_store():
    rows = []
    for i, q, _p, _t in XLM + XRP + ETH:
        r = CryptoGridSlice()
        r.id, r.qty = i, q
        rows.append(r)
    return {"CryptoGridSlice": rows, "TradingBotState": []}


def census(**override):
    owned = dict(OWNED, **override)
    return {"available": True, "held_including_zero": owned}


def run(coro):
    return asyncio.run(coro)


def qty(store, sid):
    for r in store["CryptoGridSlice"]:
        if r.id == sid:
            return r.qty
    return None


def boot(g, c, arm=True):
    async def _c():
        return c
    return run(book_fix.run_at_boot(g, arm=arm, census_fn=_c))


def writes(g):
    return [x for x in g.log if x[0] in ("delete", "add") and x[1] == "CryptoGridSlice"]


class Arming(unittest.TestCase):
    def tearDown(self):
        os.environ.pop(book_fix.ARM_ENV, None)

    def test_unarmed_it_reads_and_writes_nothing(self):
        g = FakeGrid(live_status(), fresh_store())
        rep = boot(g, census(), arm=False)
        self.assertEqual(rep["reason"], "NOT_ARMED")
        self.assertEqual(g.log, [])
        self.assertEqual(g.status_calls, 0)

    def test_only_an_explicit_yes_arms_it(self):
        for v in ("yes", "true", "1", "on", "YES", '"yes"'):
            os.environ[book_fix.ARM_ENV] = v
            self.assertTrue(book_fix.armed(), v)
        for v in ("", "no", "0", "off", "XLM-USD", "maybe"):
            os.environ[book_fix.ARM_ENV] = v
            self.assertFalse(book_fix.armed(), v)

    def test_its_variable_is_its_own(self):
        self.assertEqual(book_fix.ARM_ENV, "BOOK_FIX_XLM_XRP")
        self.assertNotEqual(book_fix.ARM_ENV, startup_fix.TICKET_ENV)


class TheLiveBook(unittest.TestCase):
    def setUp(self):
        self.store = fresh_store()
        self.g = FakeGrid(live_status(), self.store)
        self.rep = boot(self.g, census())

    def test_xlm_is_brought_down_to_what_is_owned(self):
        self.assertIsNone(qty(self.store, 129))          # oldest, removed whole
        self.assertAlmostEqual(qty(self.store, 132), 995.2338554, places=6)
        self.assertEqual(qty(self.store, 158), 329.97757659)
        self.assertEqual(qty(self.store, 172), 347.65858583)
        left = sum(q for q in (qty(self.store, i) for i, *_ in XLM) if q)
        self.assertAlmostEqual(left, OWNED["XLM"], places=6)

    def test_xrp_is_brought_down_to_what_is_owned(self):
        self.assertIsNone(qty(self.store, 10))
        self.assertAlmostEqual(qty(self.store, 11), 61.472290, places=5)
        left = sum(q for q in (qty(self.store, i) for i, *_ in XRP) if q)
        self.assertAlmostEqual(left, OWNED["XRP"], places=6)

    def test_staked_eth_is_never_touched(self):
        for i, q, _p, _t in ETH:
            self.assertEqual(qty(self.store, i), q)
        touched = [x for x in self.g.log if x[0] == "delete" and x[2] in (137, 179, 186)]
        self.assertEqual(touched, [])
        self.assertNotIn("ETH-USD", [p["product_id"] for p in self.rep["products"]])

    def test_eth_is_out_of_reach_even_without_the_ceiling(self):
        # The $400 ceiling would also refuse a fleet-wide plan, because ETH's
        # basis alone is ~$515. That is a second guard, not the first: with
        # the ceiling lifted, ETH must still be unreachable.
        old = book_fix.MAX_BASIS_USD
        book_fix.MAX_BASIS_USD = 1e9
        try:
            store = fresh_store()
            g = FakeGrid(live_status(), store)
            rep = boot(g, census())
        finally:
            book_fix.MAX_BASIS_USD = old
        self.assertTrue(rep["settled"], rep)
        for i, q, _p, _t in ETH:
            self.assertEqual(qty(store, i), q)

    def test_it_settles_and_marks_itself_done(self):
        self.assertTrue(self.rep["settled"], self.rep)
        self.assertEqual(self.rep["rows_written"], 4)
        mark = [r for r in self.store["TradingBotState"] if r.bot_name == book_fix.MARKER_KEY]
        self.assertEqual(mark[0].base_capital, startup_fix.DONE)

    def test_it_never_runs_twice(self):
        before = list(self.g.log)
        rep = boot(self.g, census())
        self.assertEqual(rep["reason"], "ALREADY_DONE")
        self.assertEqual(writes(self.g), [x for x in before
                                          if x[0] in ("delete", "add") and x[1] == "CryptoGridSlice"])

    def test_the_write_is_announced_in_the_activity_log(self):
        kinds = [a[2] for a in self.g.activity if len(a) > 2]
        self.assertEqual(kinds.count("RECONCILE"), 2)


class Refusals(unittest.TestCase):
    def test_a_zero_reading_is_not_written_off(self):
        store = fresh_store()
        g = FakeGrid(live_status(), store)
        rep = boot(g, census(XLM=0.0))
        xlm = [p for p in rep["products"] if p["product_id"] == "XLM-USD"][0]
        self.assertEqual(xlm["decision"], "REFUSED_ZERO_OWNED")
        for i, q, _p, _t in XLM:
            self.assertEqual(qty(store, i), q)
        self.assertIsNone(qty(store, 10))                 # XRP still corrected
        self.assertFalse(rep["settled"])
        mark = [r for r in store["TradingBotState"] if r.bot_name == book_fix.MARKER_KEY]
        self.assertNotEqual(mark[0].base_capital, startup_fix.DONE)

    def test_a_gap_larger_than_measured_is_refused(self):
        store = fresh_store()
        g = FakeGrid(live_status(), store)
        # 5% more XLM missing than measured: something else has changed.
        rep = boot(g, census(XLM=OWNED["XLM"] - 0.05 * book_fix.MEASURED_SHORT_UNITS["XLM-USD"]))
        xlm = [p for p in rep["products"] if p["product_id"] == "XLM-USD"][0]
        self.assertEqual(xlm["decision"], "REFUSED_LARGER_THAN_MEASURED")
        for i, q, _p, _t in XLM:
            self.assertEqual(qty(store, i), q)
        self.assertFalse(rep["settled"])

    def test_a_smaller_gap_writes_off_only_the_smaller_gap(self):
        store = fresh_store()
        g = FakeGrid(live_status(), store)
        boot(g, census(XLM=OWNED["XLM"] + 1000.0))
        left = sum(q for q in (qty(store, i) for i, *_ in XLM) if q)
        self.assertAlmostEqual(left, OWNED["XLM"] + 1000.0, places=6)

    def test_coin_missing_from_the_reading_is_not_a_zero(self):
        store = fresh_store()
        g = FakeGrid(live_status(), store)
        c = census()
        del c["held_including_zero"]["XRP"]
        rep = boot(g, c)
        xrp = [p for p in rep["products"] if p["product_id"] == "XRP-USD"][0]
        self.assertEqual(xrp["decision"], "REFUSED_NOT_IN_WALLET_READING")
        for i, q, _p, _t in XRP:
            self.assertEqual(qty(store, i), q)

    def test_an_unreadable_wallet_writes_nothing(self):
        store = fresh_store()
        g = FakeGrid(live_status(), store)
        rep = boot(g, {"available": False})
        self.assertEqual(rep["status"], "WALLET_UNREADABLE")
        self.assertEqual(writes(g), [])
        self.assertFalse(rep["settled"])

    def test_a_fleet_that_never_arrives_writes_nothing(self):
        store = fresh_store()
        g = FakeGrid({"branches": []}, store)
        rep = boot(g, census())
        self.assertEqual(rep["status"], "FLEET_NOT_READ")
        self.assertEqual(writes(g), [])
        self.assertFalse(rep["settled"])

    def test_the_ceiling_is_a_real_bound(self):
        old = book_fix.MAX_BASIS_USD
        book_fix.MAX_BASIS_USD = 100.0
        try:
            store = fresh_store()
            g = FakeGrid(live_status(), store)
            rep = boot(g, census())
        finally:
            book_fix.MAX_BASIS_USD = old
        self.assertEqual(rep["rows_written"], 0)
        self.assertEqual(rep["reconcile"]["status"], "REFUSED_OVER_CEILING")
        self.assertFalse(rep["settled"])

    def test_books_that_already_match_settle_with_nothing_written(self):
        store = fresh_store()
        g = FakeGrid(live_status(), store)
        rep = boot(g, census(XLM=sum(q for _i, q, *_ in XLM), XRP=sum(q for _i, q, *_ in XRP)))
        self.assertEqual(rep["rows_written"], 0)
        self.assertTrue(rep["settled"])


class StopTradingAndPreview(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("STOP_TRADING", None)

    def test_it_runs_while_stop_trading_is_set(self):
        os.environ["STOP_TRADING"] = "true"
        store = fresh_store()
        g = FakeGrid(live_status(), store)
        rep = boot(g, census())
        self.assertTrue(rep["settled"], rep)
        self.assertIsNone(qty(store, 129))

    def test_the_preview_writes_nothing_and_prices_the_plan(self):
        store = fresh_store()
        g = FakeGrid(live_status(), store)

        async def _c():
            return census()
        p = run(book_fix.preview(g, census_fn=_c))
        self.assertEqual(g.log, [])
        self.assertTrue(p["writes_nothing"])
        self.assertEqual([r["decision"] for r in p["products"]], ["WRITE", "WRITE"])
        self.assertAlmostEqual(p["cost_basis_it_would_remove_usd"], 300.07, places=2)
        self.assertLess(p["cost_basis_it_would_remove_usd"], book_fix.MAX_BASIS_USD)


class Source(unittest.TestCase):
    src = open("book_fix.py").read()

    def test_it_has_no_order_or_cash_path(self):
        for bad in ("place_", "market_market_ioc", "/orders", "allocated_usd =",
                    "withdraw_from_grid_branch", "apply_rightsize", "apply_levels"):
            self.assertNotIn(bad, self.src, bad)

    def test_it_names_two_products_and_no_others(self):
        self.assertEqual(book_fix.PRODUCTS, ("XLM-USD", "XRP-USD"))
        self.assertEqual(set(book_fix.MEASURED_SHORT_UNITS), set(book_fix.PRODUCTS))

    def test_the_boot_hook_is_gated_on_its_own_switch(self):
        main = open("main.py").read()
        i = main.index("import book_fix")
        self.assertIn("if book_fix.armed():", main[i:i + 400])

    def test_the_report_endpoint_is_a_get(self):
        dash = open("routers/trading_dashboard.py").read()
        self.assertIn('@router.get("/book-fix")', dash)


if __name__ == "__main__":
    unittest.main(verbosity=2)
