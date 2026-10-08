"""The startup ticket does the reconcile and nothing else.

WHY THIS IS PINNED. LEVELS carried {"XRP-USD": 10, "LINK-USD": 6} from a
plan measured 2026-10-03. By 2026-10-08 the fleet had moved past both:
XRP-USD was already at 10, so writing 10 was a no-op, and LINK-USD had
been raised to 10 - so writing 6 would have LOWERED it, removing four
empty rungs the owner never agreed to give up and parking it at 6 slices
against 6 levels. And because the set-levels button is unreachable in the
same way the reconcile button is, that reduction could not have been
undone from the dashboard.

So arming the ticket must not change a single level. These tests fail if
anyone puts an entry back without reading the above.
"""
import asyncio
import unittest

import startup_fix as sfx


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Grid:
    """Records whether the fleet was ever asked for."""

    def __init__(self, branches=None):
        self.status_calls = 0
        self._branches = branches or []

    async def get_grid_status(self):
        self.status_calls += 1
        return {"branches": self._branches}

    def get_session_factory(self):
        raise AssertionError('the levels step must not open a session with '
                             'nothing requested')

    async def _log_activity_safe(self, *_a, **_k):
        return None


LINK_AT_TEN = {"product_id": "LINK-USD", "bot_name": "crypto_grid_16",
               "num_levels": 10, "allocated_usd": 137.25,
               "slices": [{"qty": 1.0, "entry_price": 13.0} for _ in range(6)]}


class TestTheTicketRequestsNoLevelChange(unittest.TestCase):

    def test_levels_is_empty(self):
        self.assertEqual(sfx.LEVELS, {},
                         'a level change was put back into the startup ticket - '
                         'read this module docstring before doing that')

    def test_nothing_requested_is_settled_and_writes_nothing(self):
        g = _Grid([LINK_AT_TEN])
        out = run(sfx.apply_levels(g))
        self.assertTrue(out["settled"], 'an empty request must settle, or the '
                                        'ticket is never marked done and retries')
        self.assertEqual(out["rows_written"], 0)
        self.assertEqual(out["applied"], [])
        self.assertEqual(out["status"], "NOTHING_REQUESTED")

    def test_it_does_not_wait_five_minutes_to_decide_that(self):
        """status_with tests `want and want <= seen`, so an empty request can
        never satisfy it and would burn the whole readiness window.

        The window is shortened for the duration of this one test. Without
        that, a regression here does not FAIL - it HANGS for five minutes
        and then fails, which is how a slow suite teaches people to skip
        it. The real values are asserted by the test below."""
        tries, nap = sfx.READY_TRIES, sfx.READY_SLEEP_SECONDS
        sfx.READY_TRIES, sfx.READY_SLEEP_SECONDS = 1, 0.0
        try:
            g = _Grid([LINK_AT_TEN])
            out = run(sfx.apply_levels(g))
            self.assertEqual(g.status_calls, 0,
                             'the empty request still polled the fleet - that is '
                             'READY_TRIES x READY_SLEEP_SECONDS of boot time')
            self.assertTrue(out["settled"])
        finally:
            sfx.READY_TRIES, sfx.READY_SLEEP_SECONDS = tries, nap

    def test_the_readiness_window_is_still_long_enough_to_matter(self):
        """Guards the premise of the test above: if these ever became small,
        the short-circuit would stop being load-bearing and this test should
        be re-read rather than deleted."""
        self.assertGreaterEqual(sfx.READY_TRIES * sfx.READY_SLEEP_SECONDS, 60.0)


class TestTheGuardsAreStillThereForARealRequest(unittest.TestCase):
    """Emptying LEVELS must not disarm the function itself - a future,
    deliberate level change still has to pass every check."""

    def test_an_explicit_request_still_refuses_below_open_slices(self):
        g = _Grid([LINK_AT_TEN])
        out = run(sfx.apply_levels(g, wanted={"LINK-USD": 3}))
        self.assertEqual(out["rows_written"], 0)
        self.assertIn("LINK-USD", out["refused"] or [])

    def test_an_explicit_no_change_request_is_settled(self):
        g = _Grid([LINK_AT_TEN])
        out = run(sfx.apply_levels(g, wanted={"LINK-USD": 10}))
        self.assertEqual(out["rows_written"], 0)
        self.assertTrue(out["settled"], 'already-at-target must settle')

    def test_six_against_six_open_slices_would_have_been_written(self):
        """The exact harm that was removed: 6 is NOT below 6 open slices, so
        the old LEVELS entry would have passed the guard and lowered LINK
        from 10 to 6. This asserts the danger was real, on the live shape."""
        import branch_levels
        rep = branch_levels.plan(LINK_AT_TEN, 6)
        self.assertTrue(rep["ok"], 'if this is False the old entry was harmless '
                                   'and this test can be simplified')
        self.assertEqual(rep["levels_before"], 10)
        self.assertEqual(rep["levels_after"], 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
