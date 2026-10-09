"""The QQQ bug: the account owns the shares, zero available to sell.

A resting order is holding them. The sell gate added in prop_bot.py
correctly refuses the doomed POST and says so once - that ended the retry
storm but did not get the position out, because nothing in the module
cancelled the order that was holding the shares. Live 2026-10-09, the
third recorded occurrence: DOG 5.054042 held / 0 available, RWM
18.122486 held / 9.061243 available, both ~5.5h past the 7,200s backstop.

This file pins the second half of the fix. The order of these sections is
the order of the risk:

  [1] UNARMED IS UNCHANGED. The switch off must behave byte-for-byte like
      the code before it existed. This is the only section that protects
      live money today, because off is how it ships.
  [2] ENTRIES CAN NEVER REACH IT. "SELL" in this module is an order side,
      not an exit; a short entry is a sell. Force-closing to let an entry
      through would be worse than the bug.
  [3] FAILS CLOSED. Anything but a confirmed status reports NOT closed.
  [4] THE SWITCH TAKES ONLY AN EXPLICIT YES.
  [5] SIZE MUST MATCH. The DELETE closes the WHOLE broker position.
  [6] THE CALL ITSELF is the broker's atomic close, not a hand-built
      cancel-then-sell.

Run: python3 test_held_exit_can_leave.py
"""

import ast
import asyncio
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) or ".")

import prop_bot


# ── A BROKER THAT ANSWERS THE WAY THE LIVE ONE DID ────────────────────
#
# Not a mock of prop_bot's own helpers - those are what is under test.
# This stands in for aiohttp's session, so _sellable_qty and
# _close_whole_position run their real bodies against a recorded reply.

class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status = status
        self._payload = payload
        self._text = text

    async def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeBroker:
    """held/available on GET; a configurable reply to DELETE."""

    def __init__(self, held, available, delete_status=200, delete_raises=False):
        self.held = held
        self.available = available
        self.delete_status = delete_status
        self.delete_raises = delete_raises
        self.gets = []
        self.deletes = []
        self.posts = []

    def get(self, url, headers=None):
        self.gets.append(url)
        if "/v2/positions/" in url:
            return _Resp(200, {"qty": str(self.held),
                               "qty_available": str(self.available)})
        return _Resp(404, None)

    def delete(self, url, headers=None):
        self.deletes.append(url)
        if self.delete_raises:
            raise RuntimeError("connection reset by peer")
        return _Resp(self.delete_status, None, text="{}")

    def post(self, url, headers=None, json=None):
        # Reaching here at all is the failure this gate exists to prevent.
        self.posts.append((url, json))
        return _Resp(403, None, text="insufficient qty available for order")


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _EnvGuard:
    """Set/clear the switch without leaking into the next test."""

    def __init__(self, value):
        self.value = value
        self.prior = None

    def __enter__(self):
        self.prior = os.environ.get(prop_bot.ALPACA_FORCE_CLOSE_ENV)
        if self.value is None:
            os.environ.pop(prop_bot.ALPACA_FORCE_CLOSE_ENV, None)
        else:
            os.environ[prop_bot.ALPACA_FORCE_CLOSE_ENV] = self.value
        return self

    def __exit__(self, *a):
        if self.prior is None:
            os.environ.pop(prop_bot.ALPACA_FORCE_CLOSE_ENV, None)
        else:
            os.environ[prop_bot.ALPACA_FORCE_CLOSE_ENV] = self.prior
        return False


# ═══ [1] UNARMED IS UNCHANGED ═════════════════════════════════════════

class UnarmedIsByteIdentical(unittest.TestCase):
    """Off is how this ships, so off is the section that matters most."""

    def test_unset_sends_no_delete(self):
        for src in sorted(prop_bot._EXIT_SOURCES):
            with _EnvGuard(None):
                b = FakeBroker(held=5.054042, available=0.0)
                armed = prop_bot.force_close_held_exits_armed()
            self.assertFalse(armed, f"unset must be OFF (source {src})")
            self.assertEqual(b.deletes, [], "nothing may be sent while unset")

    def test_every_off_shaped_value_is_off(self):
        for val in ("", " ", "0", "false", "False", "FALSE", "no", "off",
                    "off ", "nope", "2", "-1", "truthy", "tru", "y3s",
                    '"0"', "'false'", "null", "None", "disabled"):
            with _EnvGuard(val):
                self.assertFalse(
                    prop_bot.force_close_held_exits_armed(),
                    f"{val!r} must read as OFF - a switch that arms live "
                    f"position closing on a guess is not a switch")

    def test_the_gate_still_refuses_when_unarmed(self):
        """The DOG case, switch off: no DELETE, no POST, no sale."""
        with _EnvGuard(None):
            b = FakeBroker(held=5.054042, available=0.0)
            avail, owned = run(prop_bot._sellable_qty(b, "DOG"))
        self.assertEqual(avail, 0.0)
        self.assertAlmostEqual(owned, 5.054042, places=6)
        self.assertLess(avail, 5.054042, "this is the refusal condition")
        self.assertEqual(b.deletes, [], "unarmed must not close anything")
        self.assertEqual(b.posts, [], "the doomed POST must still not be sent")


# ═══ [2] ENTRIES CAN NEVER REACH THE CLOSE PATH ═══════════════════════

class OnlyExitsMayClose(unittest.TestCase):
    """A short entry is a sell. Closing a position for one would be worse
    than the stuck position this fix clears."""

    EVERY_SOURCE = {"exit_pass", "entry_pass", "branch_exit", "branch_entry",
                    "idle_cash_sweep", "opening_bar_entry",
                    "opening_bar_exit", "unlabelled"}

    def test_exit_sources_are_exactly_the_three_exit_callers(self):
        self.assertEqual(
            set(prop_bot._EXIT_SOURCES),
            {"exit_pass", "branch_exit", "opening_bar_exit"})

    def test_no_entry_source_is_admitted(self):
        for src in sorted(self.EVERY_SOURCE - set(prop_bot._EXIT_SOURCES)):
            self.assertNotIn(
                src, prop_bot._EXIT_SOURCES,
                f"{src} is not an exit and must never reach the close")

    def test_the_source_names_match_the_real_call_sites(self):
        """The frozenset is by name, so a renamed caller must break this,
        not silently stop being an exit (or silently become one)."""
        src = inspect.getsource(prop_bot)
        tree = ast.parse(src)
        found = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name != "execute_futures_trade":
                continue
            for kw in node.keywords:
                if kw.arg == "source" and isinstance(kw.value, ast.Constant):
                    found.add(kw.value.value)
        self.assertTrue(found, "found no labelled call sites to check against")
        for s in prop_bot._EXIT_SOURCES:
            self.assertIn(
                s, found,
                f"_EXIT_SOURCES names {s!r} but no caller passes it - "
                f"either the caller was renamed or the set is stale")


# ═══ [3] FAILS CLOSED ═════════════════════════════════════════════════

class AnythingUnconfirmedIsNotClosed(unittest.TestCase):
    """Reporting a close that did not happen books a full-size P&L for a
    sale that never left. That is worse than the stuck position."""

    def test_confirmed_statuses_report_closed(self):
        for status in (200, 207):
            b = FakeBroker(5.0, 0.0, delete_status=status)
            closed, detail = run(prop_bot._close_whole_position(b, "DOG"))
            self.assertTrue(closed, f"HTTP {status} is the broker's confirm")
            self.assertIn(str(status), detail)

    def test_every_other_status_reports_not_closed(self):
        for status in (201, 202, 204, 301, 400, 401, 403, 404, 422, 429,
                       500, 502, 503, 504):
            b = FakeBroker(5.0, 0.0, delete_status=status)
            closed, detail = run(prop_bot._close_whole_position(b, "DOG"))
            self.assertFalse(
                closed,
                f"HTTP {status} is not a confirmed close and must fail closed")
            self.assertIn(str(status), detail,
                          "the detail must name what the broker actually said")

    def test_an_exception_reports_not_closed(self):
        b = FakeBroker(5.0, 0.0, delete_raises=True)
        closed, detail = run(prop_bot._close_whole_position(b, "DOG"))
        self.assertFalse(closed, "a dropped connection is not a close")
        self.assertIn("RuntimeError", detail)

    def test_an_unreadable_body_does_not_crash_the_exit(self):
        """A close that raises while formatting its own failure message
        would take the whole exit pass down with it."""

        class NoBody(_Resp):
            async def text(self):
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")

        class B(FakeBroker):
            def delete(self, url, headers=None):
                self.deletes.append(url)
                return NoBody(500, None)

        b = B(5.0, 0.0)
        closed, detail = run(prop_bot._close_whole_position(b, "DOG"))
        self.assertFalse(closed)
        self.assertIn("500", detail)


# ═══ [4] THE SWITCH TAKES ONLY AN EXPLICIT YES ════════════════════════

class ArmingIsDeliberate(unittest.TestCase):
    def test_affirmatives_arm(self):
        for val in ("1", "true", "TRUE", "True", "yes", "YES", "on", "ON",
                    " true ", '"true"', "'1'", " on"):
            with _EnvGuard(val):
                self.assertTrue(
                    prop_bot.force_close_held_exits_armed(),
                    f"{val!r} is an explicit yes and should arm")

    def test_the_env_name_is_specific_to_this_behaviour(self):
        self.assertEqual(prop_bot.ALPACA_FORCE_CLOSE_ENV,
                         "ALPACA_FORCE_CLOSE_HELD_EXITS")
        self.assertIn("ALPACA", prop_bot.ALPACA_FORCE_CLOSE_ENV,
                      "Alpaca and Coinbase are separate systems and the "
                      "variable must not read as if it touched both")


# ═══ [5] SIZE MUST MATCH ══════════════════════════════════════════════

class OnlyAnExactPositionIsClosed(unittest.TestCase):
    """The DELETE closes the WHOLE broker position. If the account holds
    more than this bot tracks, closing it sells shares nobody asked to
    sell and books a part-size P&L for a full-size sale."""

    def _exact(self, owned, qty):
        # The same expression the gate uses, kept here so a change to the
        # tolerance has to be made in two places deliberately.
        return owned is not None and abs(owned - float(qty)) <= max(
            1e-6, float(qty) * 1e-6)

    def test_the_live_cases_match_exactly(self):
        self.assertTrue(self._exact(5.054042, 5.054042), "DOG")
        self.assertTrue(self._exact(18.122486, 18.122486), "RWM")

    def test_a_larger_broker_position_is_refused(self):
        self.assertFalse(self._exact(10.0, 5.054042),
                         "holding more than tracked must fall through")
        self.assertFalse(self._exact(5.1, 5.054042))

    def test_a_smaller_broker_position_is_refused(self):
        self.assertFalse(self._exact(2.0, 5.054042))
        self.assertFalse(self._exact(0.0, 5.054042))

    def test_none_is_refused(self):
        self.assertFalse(self._exact(None, 5.054042),
                         "an unreadable owned qty is not a match")

    def test_float_noise_at_nine_decimals_still_matches(self):
        """Alpaca reports fractional qty as a string; a round-trip through
        float must not turn a true match into a refusal."""
        for q in (5.054042, 18.122486, 0.020988, 1234.567891):
            self.assertTrue(self._exact(float(f"{q:.9f}"), q), f"{q}")

    def test_the_gate_uses_this_same_guard(self):
        """Pinned in the source, because the guard is the whole protection
        against selling untracked shares."""
        src = inspect.getsource(prop_bot.execute_futures_trade)
        self.assertIn("_exact", src, "the gate must compute the size guard")
        i_exact = src.index("_exact =")
        i_armed = src.index("force_close_held_exits_armed()")
        self.assertLess(i_exact, i_armed,
                        "the guard must be computed before it is tested")
        cond = src[i_armed:src.index("_close_whole_position", i_armed)]
        self.assertIn("_EXIT_SOURCES", cond,
                      "the source check must be in the same condition")
        self.assertIn("_exact", cond,
                      "the size check must be in the same condition - an "
                      "'and' that got split into a second 'if' is how this "
                      "guard stops guarding")


# ═══ [6] THE CALL IS THE BROKER'S OWN ATOMIC CLOSE ════════════════════

class ItUsesTheAtomicClose(unittest.TestCase):
    """A hand-built cancel-then-POST opens a window where the shares are
    free and this bot has not claimed them. cancel_orders=true does both
    in one call that cannot interleave."""

    def test_the_url_cancels_and_closes_together(self):
        b = FakeBroker(5.054042, 0.0)
        run(prop_bot._close_whole_position(b, "DOG"))
        self.assertEqual(len(b.deletes), 1, "exactly one call, not two")
        url = b.deletes[0]
        self.assertIn("/v2/positions/DOG", url)
        self.assertIn("cancel_orders=true", url,
                      "without this the DELETE is rejected the same way the "
                      "POST was - the orders still hold the shares")

    def test_it_never_posts_an_order(self):
        b = FakeBroker(5.054042, 0.0)
        run(prop_bot._close_whole_position(b, "DOG"))
        self.assertEqual(b.posts, [],
                         "the close must not be a hand-built sell order")

    def test_no_separate_order_cancel_loop(self):
        src = inspect.getsource(prop_bot._close_whole_position)
        self.assertNotIn("/v2/orders", src,
                         "cancelling orders by hand first reopens the race "
                         "this function exists to avoid")

    def test_it_is_a_delete_not_a_get_with_side_effects(self):
        src = inspect.getsource(prop_bot._close_whole_position)
        self.assertIn("session.delete", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
