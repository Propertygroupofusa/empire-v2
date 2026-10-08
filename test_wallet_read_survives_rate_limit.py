"""The accounts read refuses often. It must stop becoming UNKNOWN.

THE MEASURED WEATHER. account_census already recorded it: four of ten
consecutive calls came back `available: false, error: "accounts HTTP
429"` on 2026-09-28 13:53Z. A 40% refusal rate is normal on this
endpoint, so any caller that turns a refusal into "unknown" is unknown
about half the time.

WHAT THAT COST. 2026-10-08 12:44:58Z: the grid's buy gate reads this
map. One refusal made the gate pass, and crypto_grid_16 bought 1.05 more
LINK into a branch already 5.88 units short.

THREE LAYERS, and this module tests the first two:
  1. fetch_balances retries a 429 instead of surrendering on the first.
  2. wallet_owned_units serves the last real reading, with its age, when
     the venue will not answer at all - the doctrine account_census
     already wrote down for census_cached.
  3. the gate remembers a confirmed shortfall - test_backing_gate_
     remembers.py.
Layer 3 is the backstop. Layers 1 and 2 are why it should almost never
be needed.
"""
import asyncio
import unittest

import account_census as ac
import crypto_grid_bot as g


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


class _Session:
    """Replays a scripted list of responses and records the calls."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, _url, **_kw):
        self.calls += 1
        return self._responses.pop(0) if self._responses else _Resp(500)


OK_BODY = {"accounts": [
    {"currency": "LINK",
     "available_balance": {"value": "4.48"},
     "hold": {"value": "0"}}]}


class TestTheAccountsReadRetriesARateLimit(unittest.TestCase):

    def setUp(self):
        self._slept = []
        self._orig_sleep = ac.asyncio.sleep
        # No venue credentials in a test process, and _auth_headers
        # raises without them - which would mask the retry entirely.
        self._orig_headers = ac._auth_headers
        ac._auth_headers = lambda *_a, **_k: {}

        async def _no_wait(secs):
            self._slept.append(secs)
        ac.asyncio.sleep = _no_wait

    def tearDown(self):
        ac.asyncio.sleep = self._orig_sleep
        ac._auth_headers = self._orig_headers

    def test_a_429_then_a_200_is_a_readable_balance(self):
        s = _Session([_Resp(429, text="rate limited"), _Resp(200, OK_BODY)])
        out = run(ac.fetch_balances(s))
        self.assertTrue(out.get("available"),
                        'a single 429 still refused the whole read')
        self.assertEqual(out["held_including_zero"]["LINK"], 4.48)
        self.assertEqual(s.calls, 2)
        self.assertTrue(self._slept, 'it retried without waiting at all')

    def test_it_honours_retry_after(self):
        s = _Session([_Resp(429, text="slow down", headers={"Retry-After": "2"}),
                      _Resp(200, OK_BODY)])
        run(ac.fetch_balances(s))
        self.assertEqual(self._slept[0], 2.0)

    def test_retry_after_is_clamped_to_the_budget(self):
        """A venue asking for five minutes must not stall a trading cycle."""
        s = _Session([_Resp(429, headers={"Retry-After": "300"}),
                      _Resp(200, OK_BODY)])
        run(ac.fetch_balances(s))
        self.assertLessEqual(self._slept[0], ac.ACCOUNTS_RETRY_BUDGET_SECONDS)

    def test_a_garbage_retry_after_falls_back_to_the_backoff(self):
        s = _Session([_Resp(429, headers={"Retry-After": "soon"}),
                      _Resp(200, OK_BODY)])
        run(ac.fetch_balances(s))
        self.assertEqual(self._slept[0], ac.ACCOUNTS_RETRY_BACKOFF_SECONDS[0])

    def test_it_gives_up_and_still_reports_the_refusal(self):
        s = _Session([_Resp(429, text="no"), _Resp(429, text="no"),
                      _Resp(429, text="no")])
        out = run(ac.fetch_balances(s))
        self.assertFalse(out.get("available"))
        self.assertIn("429", out["error"])
        self.assertEqual(out["attempts"], ac.ACCOUNTS_RETRY_ATTEMPTS)
        self.assertEqual(s.calls, ac.ACCOUNTS_RETRY_ATTEMPTS,
                         'the retry budget is not bounded')

    def test_an_auth_failure_is_not_retried(self):
        """A 401 will not reconsider. Retrying it only burns cycle time."""
        s = _Session([_Resp(401, text="unauthorized"), _Resp(200, OK_BODY)])
        out = run(ac.fetch_balances(s))
        self.assertFalse(out.get("available"))
        self.assertIn("401", out["error"])
        self.assertEqual(s.calls, 1)
        self.assertEqual(self._slept, [])

    def test_it_still_never_raises(self):
        class _Boom:
            calls = 0

            def get(self, *_a, **_k):
                raise RuntimeError("socket gone")
        out = run(ac.fetch_balances(_Boom()))
        self.assertFalse(out.get("available"))
        self.assertIn("RuntimeError", out["error"])


class TestAStaleReadingBeatsNoReading(unittest.TestCase):
    """Layer 2. Not a fallback value - a real measurement with an age."""

    def setUp(self):
        self._orig = ac.fetch_balances
        g._WALLET_UNITS_CACHE.update(units=None, at=0.0)

    def tearDown(self):
        ac.fetch_balances = self._orig
        g._WALLET_UNITS_CACHE.update(units=None, at=0.0)

    def _refuse(self):
        async def _f(_s):
            return {"available": False, "error": "accounts HTTP 429"}
        ac.fetch_balances = _f

    def test_a_refusal_serves_the_last_real_reading(self):
        import time
        g._WALLET_UNITS_CACHE.update(units={"LINK": 4.48}, at=time.time() - 120)
        self._refuse()
        got = run(g.wallet_owned_units())
        self.assertEqual(got, {"LINK": 4.48},
                         'a 429 threw away a two-minute-old real reading')

    def test_past_the_ceiling_it_is_refused(self):
        import time
        g._WALLET_UNITS_CACHE.update(
            units={"LINK": 4.48},
            at=time.time() - g.WALLET_UNITS_STALE_CEILING_SECONDS - 1)
        self._refuse()
        self.assertIsNone(run(g.wallet_owned_units()),
                          'an old reading masqueraded as a current one')

    def test_with_no_cached_reading_it_is_still_none(self):
        self._refuse()
        self.assertIsNone(run(g.wallet_owned_units()))

    def test_an_exception_takes_the_same_path(self):
        import time
        g._WALLET_UNITS_CACHE.update(units={"LINK": 4.48}, at=time.time() - 30)

        async def _boom(_s):
            raise RuntimeError("socket gone")
        ac.fetch_balances = _boom
        self.assertEqual(run(g.wallet_owned_units()), {"LINK": 4.48})

    def test_a_good_read_still_wins_and_refreshes_the_cache(self):
        import time
        g._WALLET_UNITS_CACHE.update(units={"LINK": 1.0}, at=time.time() - 120)

        async def _ok(_s):
            return {"available": True,
                    "available_units": {"LINK": 9.34},
                    "held_including_zero": {"LINK": 9.34}}
        ac.fetch_balances = _ok
        self.assertEqual(run(g.wallet_owned_units()), {"LINK": 9.34})
        self.assertEqual(g._WALLET_UNITS_CACHE["units"], {"LINK": 9.34})

    def test_the_ceiling_is_a_real_bound(self):
        self.assertGreater(g.WALLET_UNITS_STALE_CEILING_SECONDS,
                           g.WALLET_UNITS_TTL_SECONDS)
        self.assertLessEqual(g.WALLET_UNITS_STALE_CEILING_SECONDS, 3600.0)


class TestTheThreeLayersTogether(unittest.TestCase):
    """The 2026-10-08 12:44:58Z buy, with every layer in place."""

    def setUp(self):
        self._orig = ac.fetch_balances
        g._CONFIRMED_SHORT.clear()
        g._WALLET_UNITS_CACHE.update(units=None, at=0.0)

    def tearDown(self):
        ac.fetch_balances = self._orig
        g._CONFIRMED_SHORT.clear()
        g._WALLET_UNITS_CACHE.update(units=None, at=0.0)

    def test_the_link_buy_is_refused_on_every_layer(self):
        import time
        slices = [{"qty": 1.036, "entry_price": 13.1} for _ in range(10)]

        # Layer 2: the venue refuses, the cache still holds the reading
        # that proves the shortfall.
        g._WALLET_UNITS_CACHE.update(units={"LINK": 4.48}, at=time.time() - 90)

        async def _refuse(_s):
            return {"available": False, "error": "accounts HTTP 429"}
        ac.fetch_balances = _refuse

        units = run(g.wallet_owned_units())
        self.assertEqual(units, {"LINK": 4.48})
        ok, why = run(g.branch_backing_verdict("LINK-USD", slices, 12.96, units))
        self.assertFalse(ok)
        self.assertIn("reconcile-slices", why)

        # Layer 3: even with nothing to serve at all, the remembered
        # shortfall refuses it.
        g._WALLET_UNITS_CACHE.update(units=None, at=0.0)
        self.assertIsNone(run(g.wallet_owned_units()))
        ok2, why2 = run(g.branch_backing_verdict("LINK-USD", slices, 12.96, None))
        self.assertFalse(ok2)
        self.assertIn("measured short", why2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
