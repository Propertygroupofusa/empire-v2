"""Right-sizing must never cut a branch below the coin it holds.

Real SQLite throughout. The thing being asserted is a floor enforced at
write time against fresh rows, which a mocked session could not test.
"""
import os
import tempfile
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import branch_rightsize as R


class _Grid:
    def __init__(self, sf, branches=None):
        self.get_session_factory = sf
        self._branches = branches or []
        self.logged = []

    async def get_grid_status(self):
        return {"branches": self._branches}

    async def _log_activity_safe(self, *a, **k):
        self.logged.append(a)


async def _db():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from models import Base
    path = tempfile.mktemp(suffix=".db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return (lambda: async_sessionmaker(engine, expire_on_commit=False)), path


async def _seed(sf, bot, product, alloc, levels, slices):
    """slices: list of (qty, entry_price)."""
    from models import CryptoGridBranch, CryptoGridSlice
    async with sf()() as db:
        db.add(CryptoGridBranch(bot_name=bot, product_id=product,
                                allocated_usd=alloc, num_levels=levels,
                                grid_pct=0.03, reference_price=1.0))
        for q, e in slices:
            db.add(CryptoGridSlice(bot_name=bot, product_id=product,
                                   qty=q, entry_price=e))
        await db.commit()


async def _alloc(sf, bot):
    from sqlalchemy import select

    from models import CryptoGridBranch
    async with sf()() as db:
        b = (await db.execute(select(CryptoGridBranch).where(
            CryptoGridBranch.bot_name == bot))).scalars().first()
        return None if b is None else round(float(b.allocated_usd), 2)


async def _sell_one(sf, bot):
    """Simulate a slice selling between preview and execute."""
    from sqlalchemy import select

    from models import CryptoGridSlice
    async with sf()() as db:
        s = (await db.execute(select(CryptoGridSlice).where(
            CryptoGridSlice.bot_name == bot))).scalars().first()
        await db.delete(s)
        await db.commit()


def D(product, alloc, levels, slices):
    return {"bot_name": "b_" + product, "product_id": product,
            "allocated_usd": alloc, "num_levels": levels,
            "slices": [{"qty": q, "entry_price": e} for q, e in slices]}


class TestPureMath(unittest.TestCase):
    def test_coin_basis_is_qty_times_entry(self):
        self.assertEqual(R.coin_basis(
            [{"qty": 2.0, "entry_price": 10.0},
             {"qty": 0.5, "entry_price": 100.0}]), 70.0)

    def test_the_real_xrp_case(self):
        """The branch this was built for: $2,228.05 against $653.60 of coin
        on a 3-level grid holding 7 slices."""
        b = D("XRP-USD", 2228.05, 3, [(653.60 / 7 / 2.0, 2.0)] * 7)
        amount, why = R.freeable(b)
        self.assertIsNone(why)
        self.assertAlmostEqual(amount, 2228.05 - 653.60, places=1)

    def test_a_branch_with_a_free_rung_is_left_alone(self):
        b = D("APE-USD", 246.97, 3, [(10.0, 8.257)])
        amount, why = R.freeable(b)
        self.assertEqual(amount, 0.0)
        self.assertIn("free rung", why)

    def test_a_flat_branch_is_refused_and_points_at_withdraw(self):
        b = D("QNT-USD", 160.65, 3, [])
        amount, why = R.freeable(b)
        self.assertEqual(amount, 0.0)
        self.assertIn("flat", why)
        self.assertIn("withdraw", why)

    def test_a_branch_almost_fully_in_coin_frees_only_the_sliver(self):
        """ZEC: $2,271.29 allocated against $2,196.76 of coin, 6 slices on a
        3-level grid. Only $74.53 is headroom, and the $2,196.76 of basis
        must be untouchable - this is the branch where cutting to a round
        number would eat a real position."""
        b = D("ZEC-USD", 2271.29, 3, [(2196.76 / 6, 1.0)] * 6)
        amount, why = R.freeable(b)
        self.assertIsNone(why)
        self.assertAlmostEqual(amount, 74.53, places=1)
        self.assertGreaterEqual(round(2271.29 - amount, 2), 2196.76)

    def test_floor_is_never_below_the_row_deletion_floor(self):
        """Small coin basis must not let the branch be emptied to zero.

        The slice is $2.00, above the engine's $1.00 dust floor, so it still
        FILLS the rung - the original fixture used $0.01, which the
        2026-10-04 dust change correctly reclassified as a remnant that
        leaves the rung free."""
        b = D("X-USD", 40.0, 1, [(2.0, 1.0)])
        self.assertEqual(R.floor_for(b), R.MIN_BRANCH_USD)
        amount, _ = R.freeable(b)
        self.assertEqual(amount, 25.0)
        self.assertEqual(40.0 - amount, R.MIN_BRANCH_USD)

    def test_headroom_under_the_minimum_is_refused(self):
        b = D("X-USD", 105.0, 1, [(100.0, 1.0)])
        amount, why = R.freeable(b)
        self.assertEqual(amount, 0.0)
        self.assertIn("minimum", why)


class TestAgreesWithTheEngine(unittest.TestCase):
    """This module and the engine must answer "is this branch full" the same
    way. For a few hours on 2026-10-04 they did not, and this module offered
    BCH-USD's $68.21 and LINK-USD's $45.99 as budget they could not spend -
    moments after the engine had freed them to spend it."""

    def test_the_real_bch_shape_is_no_longer_a_rightsize_target(self):
        bch = D("BCH-USD", 171.29, 3,
                [(0.12644109, 339.58), (0.18181268, 330.78), (2.2e-07, 309.32)])
        amount, why = R.freeable(bch)
        self.assertEqual(amount, 0.0)
        self.assertIn("free rung", why)
        self.assertIn("remnant", why)

    def test_the_real_link_shape_is_no_longer_a_target_either(self):
        link = D("LINK-USD", 137.04, 3,
                 [(0.01, 14.343), (2.99, 15.212), (3.1, 14.651)])
        amount, _ = R.freeable(link)
        self.assertEqual(amount, 0.0)

    def test_a_branch_full_of_real_slices_is_still_a_target(self):
        """XRP: 7 real slices on 3 levels. Genuinely stuck, still freeable."""
        xrp = D("XRP-USD", 2228.05, 3, [(100.0, 0.934)] * 7)
        amount, why = R.freeable(xrp)
        self.assertIsNone(why)
        self.assertGreater(amount, 1500)

    def test_it_frees_nothing_if_the_engine_predicate_cannot_be_imported(self):
        """Fail CLOSED. Returning the raw list instead would reproduce the
        very bug this agreement was added to prevent."""
        import builtins
        real = builtins.__import__

        def boom(name, *a, **k):
            if name == "crypto_grid_bot":
                raise ImportError("simulated")
            return real(name, *a, **k)
        xrp = D("XRP-USD", 2228.05, 3, [(100.0, 0.934)] * 7)
        builtins.__import__ = boom
        try:
            amount, why = R.freeable(xrp)
        finally:
            builtins.__import__ = real
        self.assertEqual(amount, 0.0, "must free nothing on an unknown rung count")
        self.assertIn("free rung", why)
        # And it still frees normally once the import works again.
        self.assertGreater(R.freeable(xrp)[0], 1500)

    def test_a_branch_whose_rungs_are_only_dust_is_refused(self):
        """All three "rungs" are remnants, so the branch has free rungs and
        will buy - its budget is not stranded."""
        b = D("X-USD", 500.0, 3, [(1e-07, 1.0)] * 3)
        amount, why = R.freeable(b)
        self.assertEqual(amount, 0.0)
        self.assertIn("free rung", why)


class TestApply(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sf, _ = await _db()

    async def test_dry_run_changes_nothing(self):
        await _seed(self.sf, "b1", "XRP-USD", 2228.05, 3, [(100.0, 0.934)] * 7)
        g = _Grid(self.sf)
        r = await R.apply_one(g, "b1", dry_run=True)
        self.assertTrue(r["ok"])
        self.assertTrue(r["dry_run"])
        self.assertGreater(r["freed_usd"], 1500)
        self.assertEqual(await _alloc(self.sf, "b1"), 2228.05)
        self.assertEqual(g.logged, [])

    async def test_live_run_lowers_allocation_to_the_floor(self):
        await _seed(self.sf, "b1", "XRP-USD", 2228.05, 3, [(100.0, 0.934)] * 7)
        g = _Grid(self.sf)
        r = await R.apply_one(g, "b1", dry_run=False)
        self.assertTrue(r["ok"])
        basis = round(7 * 100.0 * 0.934, 2)
        self.assertEqual(r["coin_basis_usd"], basis)
        self.assertEqual(await _alloc(self.sf, "b1"), basis)
        self.assertEqual(r["freed_usd"], round(2228.05 - basis, 2))
        self.assertEqual(len(g.logged), 1)

    async def test_it_never_cuts_below_coin_basis(self):
        await _seed(self.sf, "b1", "X-USD", 1000.0, 2, [(400.0, 1.0)] * 2)
        g = _Grid(self.sf)
        r = await R.apply_one(g, "b1", amount_usd=999.0, dry_run=False)
        self.assertTrue(r["ok"])
        self.assertTrue(r["clamped_to_floor"])
        self.assertEqual(await _alloc(self.sf, "b1"), 800.0)
        self.assertEqual(r["freed_usd"], 200.0)

    async def test_a_smaller_requested_amount_is_honoured(self):
        await _seed(self.sf, "b1", "X-USD", 1000.0, 2, [(400.0, 1.0)] * 2)
        g = _Grid(self.sf)
        r = await R.apply_one(g, "b1", amount_usd=50.0, dry_run=False)
        self.assertEqual(r["freed_usd"], 50.0)
        self.assertFalse(r["clamped_to_floor"])
        self.assertEqual(await _alloc(self.sf, "b1"), 950.0)

    async def test_the_floor_is_recomputed_at_write_time(self):
        """THE RACE. A plan says $600 is free. A slice sells before execute,
        which RAISES the basis share per remaining slice... so instead:
        a slice is removed, basis falls, and the branch is no longer parked.
        The write must refuse rather than act on the stale plan."""
        await _seed(self.sf, "b1", "X-USD", 1000.0, 3, [(100.0, 1.0)] * 3)
        g = _Grid(self.sf)
        pre = await R.apply_one(g, "b1", dry_run=True)
        self.assertTrue(pre["ok"])
        await _sell_one(self.sf, "b1")          # now 2 slices of 3 - not parked
        post = await R.apply_one(g, "b1", amount_usd=pre["freed_usd"],
                                 dry_run=False)
        self.assertFalse(post["ok"])
        self.assertIn("free rung", post["reason"])
        self.assertEqual(await _alloc(self.sf, "b1"), 1000.0)

    async def test_a_flat_branch_is_refused_at_write_time(self):
        await _seed(self.sf, "b1", "X-USD", 1000.0, 3, [])
        g = _Grid(self.sf)
        r = await R.apply_one(g, "b1", dry_run=False)
        self.assertFalse(r["ok"])
        self.assertIn("flat", r["reason"])
        self.assertEqual(await _alloc(self.sf, "b1"), 1000.0)

    async def test_an_unknown_branch_is_refused_not_created(self):
        g = _Grid(self.sf)
        r = await R.apply_one(g, "nope", dry_run=False)
        self.assertFalse(r["ok"])
        self.assertIn("no branch", r["reason"])
        self.assertIsNone(await _alloc(self.sf, "nope"))

    async def test_the_branch_row_always_survives(self):
        """A drained row gets deleted and takes its coin out of the fleet."""
        await _seed(self.sf, "b1", "X-USD", 60.0, 1, [(1.5, 1.0)])
        g = _Grid(self.sf)
        r = await R.apply_one(g, "b1", amount_usd=10_000.0, dry_run=False)
        self.assertTrue(r["ok"])
        self.assertEqual(await _alloc(self.sf, "b1"), R.MIN_BRANCH_USD)
        self.assertIsNotNone(await _alloc(self.sf, "b1"))

    async def test_no_slice_is_ever_touched(self):
        from sqlalchemy import select

        from models import CryptoGridSlice
        await _seed(self.sf, "b1", "X-USD", 1000.0, 2, [(400.0, 1.0)] * 2)
        g = _Grid(self.sf)
        await R.apply_one(g, "b1", dry_run=False)
        async with self.sf()() as db:
            rows = (await db.execute(select(CryptoGridSlice).where(
                CryptoGridSlice.bot_name == "b1"))).scalars().all()
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(float(r.qty) for r in rows), [400.0, 400.0])


class TestPlan(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sf, _ = await _db()

    async def test_plan_totals_only_the_parked_branches(self):
        g = _Grid(self.sf, [
            D("XRP-USD", 2228.05, 3, [(100.0, 0.934)] * 7),   # parked
            D("APE-USD", 246.97, 3, [(10.0, 8.257)]),         # free rung
            D("QNT-USD", 160.65, 3, []),                      # flat
        ])
        p = await R.plan(g)
        self.assertTrue(p["read_only"])
        by = {r["product_id"]: r for r in p["branches"]}
        self.assertGreater(by["XRP-USD"]["freeable_usd"], 1500)
        self.assertEqual(by["APE-USD"]["freeable_usd"], 0.0)
        self.assertEqual(by["QNT-USD"]["freeable_usd"], 0.0)
        self.assertEqual(p["total_freeable_usd"],
                         by["XRP-USD"]["freeable_usd"])

    async def test_plan_warns_that_total_allocated_will_fall(self):
        g = _Grid(self.sf, [D("XRP-USD", 2228.05, 3, [(100.0, 0.934)] * 7)])
        p = await R.plan(g)
        self.assertIn("TOTAL ALLOCATED", p["detail"])
        self.assertIn("the money is not gone", p["detail"])
        self.assertIn("no coin", p["detail"].lower())

    async def test_plan_reports_the_floor_per_branch(self):
        g = _Grid(self.sf, [D("XRP-USD", 2228.05, 3, [(100.0, 0.934)] * 7)])
        r = (await R.plan(g))["branches"][0]
        self.assertEqual(r["floor_usd"], r["coin_basis_usd"])
        self.assertAlmostEqual(r["allocated_usd"] - r["freeable_usd"],
                               r["floor_usd"], places=2)
        self.assertTrue(r["parked"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
