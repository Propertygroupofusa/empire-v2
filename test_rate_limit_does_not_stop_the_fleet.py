"""Coinbase rate-limits. Neither read may turn that into a wrong answer.

MEASURED 2026-10-09 03:36Z, from the live log:

    [CENSUS] accounts HTTP 429 - attempt 1/3, waiting 0.5s
    [CENSUS] accounts HTTP 429 - attempt 2/3, waiting 1.5s
    [WARNING] HTTP 429 fetching USD:
    [WARNING] HTTP 429 fetching all balances:
    [PRICES] best_bid_ask HTTP 429 for 20 product(s)

Twelve /account-census requests inside two minutes - the dashboard, open
browser tabs, both hourly guards, and an operator re-checking a balance.
Each one made a fresh paginated accounts walk plus a price fetch,
because the endpoint called bare census() while census_cached() sat
beside it, written and tested, used by exactly one internal caller.

`HTTP 429 fetching USD` is the one that costs money. get_usd_balance
returned (None, ...) on it, get_real_free_cash_usd passes None through,
and every caller correctly reads None as "do not deploy". So polling a
read-only dashboard page could stop the fleet from buying.

[1] THE ENDPOINT SHARES ONE READING. N callers in the TTL window cost
    one venue read, not N.
[2] THE COMPARISON IS PER-CALLER. The cache is shared; tracked_usd is
    not, and callers pass different ones.
[3] THE WALLET READ RETRIES instead of reporting an empty wallet.
[4] THE RETRY REPLACES THE REQUEST, it does not add one. The first
    draft of the fix put a retry loop above the original call and would
    have made two requests per page - double load on the endpoint that
    was already refusing. This section exists because that nearly
    shipped.
[5] IT STILL FAILS CLOSED when the venue never answers.

Run: python3 -m unittest test_rate_limit_does_not_stop_the_fleet
"""

import asyncio
import inspect
import unittest

import account_census as ac


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Resp:
    def __init__(self, status, payload=None, text="", headers=None):
        self.status = status
        self._payload = payload or {}
        self._text = text
        self.headers = headers or {}

    async def json(self):
        return self._payload

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


ONE_PAGE = {"accounts": [
    {"currency": "USD", "available_balance": {"value": "102.50"},
     "hold": {"value": "0"}},
], "has_next": False}


class _CountingSession:
    """Counts every outbound GET and replays scripted statuses."""

    def __init__(self, statuses=None, payload=None):
        self.calls = []
        self.statuses = list(statuses or [])
        self.payload = payload if payload is not None else ONE_PAGE

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(url)
        status = self.statuses.pop(0) if self.statuses else 200
        if status == 200:
            return _Resp(200, self.payload)
        return _Resp(status, None, text=f"rate limited ({status})")


# ═══ [1] ONE READING, SHARED ══════════════════════════════════════════

class TheEndpointSharesOneReading(unittest.TestCase):

    def setUp(self):
        ac._CENSUS_CACHE.update(census=None, at=0.0)
        self._census = ac.census
        self.reads = {"n": 0}

        async def fake_census(session, tracked_usd=None):
            self.reads["n"] += 1
            return ac.apply_tracked({
                "available": True, "total_usd": 6630.95,
                "cash_usd": 403.76, "coin_usd": 6227.19,
                "holdings": [], "assets_held": 61,
            }, tracked_usd)

        ac.census = fake_census

    def tearDown(self):
        ac.census = self._census
        ac._CENSUS_CACHE.update(census=None, at=0.0)

    def test_twelve_callers_cost_one_venue_read(self):
        """The live number: twelve requests in two minutes."""
        for _ in range(12):
            r = run(ac.census_cached(None, tracked_usd=100.0))
            self.assertTrue(r["available"])
        self.assertEqual(self.reads["n"], 1,
                         "twelve callers inside the TTL must share ONE read")

    def test_the_first_is_fresh_and_the_rest_say_they_are_not(self):
        a = run(ac.census_cached(None, tracked_usd=100.0))
        b = run(ac.census_cached(None, tracked_usd=100.0))
        self.assertFalse(a["stale"])
        self.assertEqual(a["age_seconds"], 0.0)
        self.assertTrue(b["stale"], "a shared reading must admit its age")
        self.assertGreaterEqual(b["age_seconds"], 0.0)

    def test_past_the_ttl_it_reads_again(self):
        run(ac.census_cached(None, tracked_usd=100.0))
        run(ac.census_cached(None, tracked_usd=100.0, max_age_seconds=0))
        self.assertEqual(self.reads["n"], 2)

    def test_the_ttl_is_short_enough_to_be_honest(self):
        self.assertLessEqual(ac.CENSUS_TTL_SECONDS, 60.0,
                             "a balance page may not be minutes stale")
        self.assertGreaterEqual(ac.CENSUS_TTL_SECONDS, 15.0,
                                "too short and the burst is not absorbed")


# ═══ [2] THE COMPARISON IS PER-CALLER ═════════════════════════════════

class TheSharedReadingCarriesNoCallersComparison(unittest.TestCase):
    """The cache is shared; tracked_usd is not. /account-census passes the
    real bot-facing figure and trading_dashboard's internal caller passes
    0.0 - and the reconciliation sentence reads '$X belongs to no branch,
    so nothing monitors, prices, or stops it'. Nobody may read a borrowed
    version of that."""

    def setUp(self):
        ac._CENSUS_CACHE.update(census=None, at=0.0)
        self._census = ac.census

        async def fake_census(session, tracked_usd=None):
            return ac.apply_tracked(
                {"available": True, "total_usd": 1000.0}, tracked_usd)

        ac.census = fake_census

    def tearDown(self):
        ac.census = self._census
        ac._CENSUS_CACHE.update(census=None, at=0.0)

    def test_two_callers_get_their_own_figures(self):
        a = run(ac.census_cached(None, tracked_usd=250.0))   # fresh read
        b = run(ac.census_cached(None, tracked_usd=0.0))     # from cache
        self.assertEqual(a["tracked_usd"], 250.0)
        self.assertEqual(a["untracked_usd"], 750.0)
        self.assertEqual(b["tracked_usd"], 0.0)
        self.assertEqual(b["untracked_usd"], 1000.0,
                         "the cached reading must not carry 750.0 over")
        self.assertIn("$0.00", b["reconciliation"])
        self.assertIn("$250.00", a["reconciliation"])

    def test_a_caller_passing_none_gets_no_comparison_at_all(self):
        run(ac.census_cached(None, tracked_usd=250.0))
        c = run(ac.census_cached(None, tracked_usd=None))
        for k in ("tracked_usd", "untracked_usd", "tracked_share_pct",
                  "reconciliation"):
            self.assertNotIn(k, c, f"{k} leaked from another caller")

    def test_the_stored_copy_holds_no_comparison(self):
        run(ac.census_cached(None, tracked_usd=250.0))
        stored = ac._CENSUS_CACHE["census"]
        for k in ("tracked_usd", "untracked_usd", "tracked_share_pct",
                  "reconciliation"):
            self.assertNotIn(k, stored,
                             "the shared cache must store the ACCOUNT, not "
                             "one caller's view of it")

    def test_apply_tracked_clears_before_it_sets(self):
        d = {"total_usd": 1000.0, "tracked_usd": 999.0,
             "untracked_usd": 1.0, "tracked_share_pct": 99.9,
             "reconciliation": "stale sentence"}
        out = ac.apply_tracked(d, 100.0)
        self.assertEqual(out["tracked_usd"], 100.0)
        self.assertEqual(out["untracked_usd"], 900.0)
        self.assertNotIn("stale sentence", out["reconciliation"])

    def test_a_zero_total_does_not_divide_by_zero(self):
        out = ac.apply_tracked({"total_usd": 0.0}, 100.0)
        self.assertIsNone(out["tracked_share_pct"])
        self.assertIn("no priced total", out["reconciliation"])


# ═══ [3][4][5] THE WALLET READ ════════════════════════════════════════

class TheWalletReadSurvivesARateLimit(unittest.TestCase):

    def setUp(self):
        import crypto_coinbase_bot as cb
        self.cb = cb
        self._sleep = asyncio.sleep
        async def no_sleep(_s):
            return None
        asyncio.sleep = no_sleep
        self._supp = cb.get_supplemental_capital
        async def no_supp():
            return 0.0
        cb.get_supplemental_capital = no_supp
        self._auth = cb._auth_headers
        cb._auth_headers = lambda *a, **k: {}

    def tearDown(self):
        asyncio.sleep = self._sleep
        self.cb.get_supplemental_capital = self._supp
        self.cb._auth_headers = self._auth

    def test_a_clean_read_makes_exactly_one_request(self):
        """[4] The retry must REPLACE the request, never add one."""
        s = _CountingSession(statuses=[200])
        bal, err = run(self.cb.get_usd_balance(s))
        self.assertIsNone(err)
        self.assertAlmostEqual(bal, 102.50, places=2)
        self.assertEqual(len(s.calls), 1,
                         "an un-refused read must cost ONE request - the "
                         "first draft of this fix made two")

    def test_a_429_then_success_returns_the_balance(self):
        """[3] The rate limit must not read as an empty wallet."""
        s = _CountingSession(statuses=[429, 200])
        bal, err = run(self.cb.get_usd_balance(s))
        self.assertIsNone(err, f"a retried 429 must not surface: {err}")
        self.assertAlmostEqual(bal, 102.50, places=2)
        self.assertEqual(len(s.calls), 2)

    def test_two_refusals_then_success(self):
        s = _CountingSession(statuses=[429, 503, 200])
        bal, err = run(self.cb.get_usd_balance(s))
        self.assertIsNone(err)
        self.assertAlmostEqual(bal, 102.50, places=2)

    def test_it_never_exceeds_the_attempt_budget(self):
        s = _CountingSession(statuses=[429, 429, 429, 429, 429])
        run(self.cb.get_usd_balance(s))
        self.assertLessEqual(len(s.calls), self.cb._ACCOUNTS_RETRY_ATTEMPTS,
                             "retrying harder than the budget makes the "
                             "rate limit worse, not better")

    def test_it_fails_closed_when_the_venue_never_answers(self):
        """[5] No cached balance is served. None means do not deploy."""
        s = _CountingSession(statuses=[429, 429, 429])
        bal, err = run(self.cb.get_usd_balance(s))
        self.assertIsNone(bal, "a stale balance is a number the bot might "
                               "SPEND - refusing to deploy is the safe end")
        self.assertIn("429", err)

    def test_a_401_is_not_retried(self):
        """A status the venue will not reconsider is final on first look."""
        s = _CountingSession(statuses=[401, 200])
        bal, err = run(self.cb.get_usd_balance(s))
        self.assertIsNone(bal)
        self.assertIn("401", err)
        self.assertEqual(len(s.calls), 1, "retrying a 401 only burns cycles")

    def test_the_retry_statuses_match_the_census_module(self):
        self.assertEqual(tuple(self.cb._ACCOUNTS_RETRY_STATUSES),
                         tuple(ac.ACCOUNTS_RETRY_STATUSES))
        self.assertEqual(self.cb._ACCOUNTS_RETRY_ATTEMPTS,
                         ac.ACCOUNTS_RETRY_ATTEMPTS)

    def test_retry_after_is_honoured_but_clamped(self):
        class _H:
            headers = {"Retry-After": "900"}
        self.assertLessEqual(
            self.cb._retry_after_seconds(_H(), 0.5),
            self.cb._ACCOUNTS_RETRY_BUDGET_SECONDS,
            "a Retry-After of 900 must not stall a trading cycle")

    def test_a_junk_retry_after_falls_back(self):
        class _H:
            headers = {"Retry-After": "soon"}
        self.assertEqual(self.cb._retry_after_seconds(_H(), 0.5), 0.5)

    def test_a_missing_header_falls_back(self):
        class _H:
            headers = {}
        self.assertEqual(self.cb._retry_after_seconds(_H(), 1.5), 1.5)


# ═══ [6] THE SECOND ACCOUNTS WALK, REMOVED BY INJECTION ═══════════════

class TheBalanceIsInjectedNotCached(unittest.TestCase):
    """/account-census used to read the venue TWICE: once for the census
    and once more inside get_real_free_cash_usd for the same USD number.

    A module-level cache would have fixed the page and broken the
    trading loop, which calls the same function and must size against a
    live balance. Injection has no such reach."""

    def test_the_signature_takes_an_injected_balance(self):
        import inspect
        import crypto_grid_bot as g
        sig = inspect.signature(g.get_real_free_cash_usd)
        self.assertIn("usd_balance", sig.parameters)
        self.assertIsNone(sig.parameters["usd_balance"].default,
                          "the default must be None so an un-passed "
                          "caller behaves exactly as before")

    def test_an_injected_balance_skips_the_venue_entirely(self):
        import crypto_grid_bot as g
        src = inspect.getsource(g.get_real_free_cash_usd)
        i_guard = src.index("if usd_balance is None:")
        i_fetch = src.index("get_usd_balance")
        self.assertLess(i_guard, i_fetch,
                        "the venue call must sit INSIDE the None branch")

    def test_it_tests_is_none_not_falsiness(self):
        """A wallet holding 0.00 is a real reading. `if not usd_balance`
        would re-fetch on a genuinely empty wallet - the one case where
        the answer matters most."""
        import crypto_grid_bot as g
        src = inspect.getsource(g.get_real_free_cash_usd)
        self.assertIn("if usd_balance is None:", src)
        self.assertNotIn("if not usd_balance", src)

    def test_the_trading_loop_path_is_unchanged(self):
        """No argument -> fresh read. That is the whole safety argument
        for injection over caching."""
        import crypto_grid_bot as g
        src = inspect.getsource(g.get_real_free_cash_usd)
        self.assertIn("async with engine.aiohttp.ClientSession() as session:",
                      src)
        self.assertIn("await engine.get_usd_balance(session)", src)


class TheEndpointFeedsTheRightNumber(unittest.TestCase):
    """Both traps, pinned. Either one silently produces a wrong figure
    on a page whose whole job is to be the honest one."""

    ENDPOINT = None

    @staticmethod
    def _code_only(text):
        """Strip comments and docstrings before asserting on the code.

        The first run of this class failed three ways, and all three
        were the test's fault: the endpoint's own comments explain why
        NOT to use cash_usd and why NOT to call get_real_free_cash_usd()
        bare, so searching the raw text found the warnings against the
        very things it was checking for. A test that trips on the
        comment explaining it is testing the prose, not the program.
        """
        out = []
        for line in text.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            out.append(line)
        body = "\n".join(out)
        # Drop the function docstring too.
        if body.count('"""') >= 2:
            a = body.index('"""')
            b = body.index('"""', a + 3)
            body = body[:a] + body[b + 3:]
        return body

    def setUp(self):
        import io as _io
        self.src = _io.open("routers/trading_dashboard.py",
                            encoding="utf-8").read()
        i = self.src.index('@router.get("/account-census")')
        j = self.src.index('@router.get("/coinbase-statement")')
        self.body = self._code_only(self.src[i:j])

    def test_it_does_not_hand_over_cash_usd(self):
        """cash_usd sums the STABLE set. On this account it reads $403.76
        against a $102.50 USD wallet, because $301.26 is USDC - the gap
        currently keeping the fleet parked."""
        # Matched as a dict ACCESS, not as a bare substring: the
        # function name get_real_free_cash_usd contains "cash_usd", so
        # a substring assertion fails on the correct code. Caught on the
        # second run of this test.
        for access in ('["cash_usd"]', ".get(\"cash_usd\")",
                       "['cash_usd']", ".get('cash_usd')"):
            self.assertNotIn(access, self.body,
                             "cash_usd sums the STABLE set and includes "
                             "USDC; get_usd_balance matches USD alone")

    def test_it_reads_available_units_not_units(self):
        self.assertIn('_row.get("available_units")', self.body)
        self.assertNotIn('_row.get("units")', self.body,
                         "units is available + hold - the OWNED vs "
                         "AVAILABLE mistake, a third time")

    def test_it_matches_on_the_usd_row_specifically(self):
        self.assertIn('_row.get("asset") == "USD"', self.body)

    def test_it_passes_the_balance_in(self):
        self.assertIn("get_real_free_cash_usd(usd_balance=", self.body)
        self.assertNotIn("get_real_free_cash_usd()", self.body,
                         "an un-passed call here is the second accounts "
                         "walk this change exists to remove")

    def test_a_refused_census_does_not_call_the_venue_again(self):
        i_guard = self.body.index('if not out.get("available"):')
        i_call = self.body.index("get_real_free_cash_usd(")
        self.assertLess(i_guard, i_call,
                        "on a refusal, return before making another call "
                        "to the endpoint that just refused")

    def test_the_comparison_is_applied_after_the_read(self):
        self.assertIn("account_census.apply_tracked(out, tracked)", self.body)
        self.assertIn("tracked_usd=None", self.body,
                      "the shared reading is fetched WITHOUT a caller's "
                      "comparison baked into it")


if __name__ == "__main__":
    unittest.main(verbosity=2)
