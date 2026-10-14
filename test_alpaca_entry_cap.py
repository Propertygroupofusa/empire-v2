"""The stock bot may open only so many positions per trading day.

The owner, 2026-10-14: "It was making money then... go back to this date."
Alpaca realised P&L by week: +$6.44 and +$1.84 while it traded a handful of
times, then -$27.37 on 128 sales the week of 28 Sep. The fix that keeps the
no-loss protections is a ceiling on ENTRIES, not a rollback.

Pinned here: the counting rules (behavioural, against a dict standing in for
the database) and the wiring (structural - execute_futures_trade needs the
broker to run). Exits are never counted and never refused.
"""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import alpaca_entry_cap as cap

ET = ZoneInfo("America/New_York")
SRC = open("prop_bot.py", encoding="utf-8").read()
FN = SRC[SRC.index("async def execute_futures_trade("):]
FN = FN[:FN.index("\n# The last kill-condition reason written")]


def run(coro):
    return asyncio.run(coro)


class Store:
    def __init__(self, fail_load=False, fail_save=False):
        self.d, self.fail_load, self.fail_save = {}, fail_load, fail_save

    async def load(self, key):
        if self.fail_load:
            raise RuntimeError("db down")
        return self.d.get(key)

    async def save(self, key, value):
        if self.fail_save:
            raise RuntimeError("db down")
        self.d[key] = value


def counter(**kw):
    s = Store(**kw)
    return cap.EntryCounter(s.load, s.save), s


MON = datetime(2026, 10, 14, 10, 0, tzinfo=ET)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(cap.MAX_ENTRIES_ENV, raising=False)


# ------------------------------------------------------------ the setting
def test_default_is_three():
    assert cap.max_entries_per_day() == 3


@pytest.mark.parametrize("raw,want", [("5", 5), (" 2 ", 2), ('"4"', 4), ("1", 1)])
def test_a_positive_number_sets_it(monkeypatch, raw, want):
    monkeypatch.setenv(cap.MAX_ENTRIES_ENV, raw)
    assert cap.max_entries_per_day() == want


@pytest.mark.parametrize("raw", ["off", "OFF", "unlimited", "none", "false"])
def test_only_an_explicit_word_switches_it_off(monkeypatch, raw):
    monkeypatch.setenv(cap.MAX_ENTRIES_ENV, raw)
    assert cap.max_entries_per_day() is None


@pytest.mark.parametrize("raw", ["", "0", "-3", "three", "3x", "nan"])
def test_a_typo_never_removes_the_guard(monkeypatch, raw):
    monkeypatch.setenv(cap.MAX_ENTRIES_ENV, raw)
    assert cap.max_entries_per_day() == cap.DEFAULT_MAX_ENTRIES


# --------------------------------------------------------------- counting
def test_the_fourth_entry_of_the_day_is_refused():
    c, s = counter()
    got = [run(c.reserve(MON))[0] for _ in range(5)]
    assert got == [True, True, True, False, False]
    assert s.d[cap.day_key(MON)] == 3.0


def test_the_refusal_says_how_many_were_used():
    c, _ = counter()
    for _ in range(3):
        run(c.reserve(MON))
    ok, used, limit, why = run(c.reserve(MON))
    assert (ok, used, limit) == (False, 3, 3) and "3 of 3" in why


def test_a_rejected_order_gives_its_slot_back():
    c, s = counter()
    for _ in range(3):
        run(c.reserve(MON))
    run(c.release(MON))
    assert s.d[cap.day_key(MON)] == 2.0
    assert run(c.reserve(MON))[0] is True


def test_release_never_goes_below_zero():
    c, s = counter()
    run(c.release(MON))
    assert s.d[cap.day_key(MON)] == 0.0


def test_a_new_trading_day_starts_at_zero():
    c, _ = counter()
    for _ in range(3):
        run(c.reserve(MON))
    tue = datetime(2026, 10, 15, 9, 31, tzinfo=ET)
    assert run(c.reserve(tue))[:2] == (True, 1)


def test_the_day_turns_over_at_midnight_eastern_not_utc():
    c, _ = counter()
    for _ in range(3):
        run(c.reserve(datetime(2026, 10, 14, 19, 59, tzinfo=ET)))
    # 20:30 Eastern is 00:30 UTC the next day: still the same trading day.
    late = datetime(2026, 10, 14, 20, 30, tzinfo=ET)
    assert run(c.reserve(late))[0] is False


def test_the_count_survives_a_restart():
    """A deploy builds a new counter over the same database."""
    s = Store()
    first = cap.EntryCounter(s.load, s.save)
    for _ in range(3):
        run(first.reserve(MON))
    second = cap.EntryCounter(s.load, s.save)
    assert run(second.reserve(MON))[0] is False


def test_switched_off_nothing_is_counted_or_refused(monkeypatch):
    monkeypatch.setenv(cap.MAX_ENTRIES_ENV, "off")
    c, s = counter()
    got = [run(c.reserve(MON))[0] for _ in range(20)]
    assert all(got) and s.d == {}


def test_concurrent_entries_cannot_slip_past_the_ceiling():
    """The opening-bar symbols run as separate tasks."""
    c, s = counter()

    async def burst():
        return await asyncio.gather(*[c.reserve(MON) for _ in range(12)])

    res = run(burst())
    assert sum(1 for r in res if r[0]) == 3
    assert s.d[cap.day_key(MON)] == 3.0


def test_an_unreadable_count_refuses_rather_than_waves_through():
    c, _ = counter(fail_load=True)
    ok, used, limit, why = run(c.reserve(MON))
    assert ok is False and "unreadable" in why


def test_a_count_that_cannot_be_saved_refuses_the_entry():
    c, _ = counter(fail_save=True)
    ok, _, _, why = run(c.reserve(MON))
    assert ok is False and "could not be saved" in why


# --------------------------------------------------------------- the wiring
def test_the_entry_sources_are_exactly_the_four_that_open_a_position():
    assert cap.ENTRY_SOURCES == {"entry_pass", "branch_entry", "idle_cash_sweep", "opening_bar_entry"}


def test_no_exit_source_is_ever_counted():
    import prop_bot
    assert not (cap.ENTRY_SOURCES & prop_bot._EXIT_SOURCES)


def test_every_order_call_site_is_a_known_entry_or_exit():
    """A new call site must be looked at against the ceiling, not discovered
    after it has opened a fourth position."""
    import prop_bot
    labelled = set()
    for line in SRC.splitlines():
        if "execute_futures_trade(" in line and "async def" not in line:
            for name in cap.ENTRY_SOURCES | prop_bot._EXIT_SOURCES:
                if f'source="{name}"' in line:
                    labelled.add(name)
    # opening_bar_entry's call spans lines, so look for its label directly.
    assert 'source="opening_bar_entry"' in SRC
    labelled.add("opening_bar_entry")
    assert labelled == cap.ENTRY_SOURCES | prop_bot._EXIT_SOURCES


def test_the_ceiling_is_taken_after_the_venue_minimum_and_before_the_send():
    i_min = FN.index("MIN_ORDER_NOTIONAL_USD)")
    i_cap = FN.index("_ENTRY_COUNTER.reserve()")
    i_send = FN.index("session.post(")
    assert i_min < i_cap < i_send


def test_only_entry_sources_reach_the_counter():
    i = FN.index("_ENTRY_COUNTER.reserve()")
    guard = FN[max(0, i - 200):i]
    assert "source in alpaca_entry_cap.ENTRY_SOURCES" in guard


def test_only_an_explicit_venue_rejection_gives_the_slot_back():
    assert FN.count("_ENTRY_COUNTER.release()") == 1
    i = FN.index("_ENTRY_COUNTER.release()")
    assert "else:" in FN[i - 120:i]                      # the non-200 branch
    tail = FN[FN.index("except Exception as e:"):]
    assert "release" not in tail.split("return False")[0].replace("No release here", "")


def test_an_accepted_order_keeps_its_slot_even_if_a_later_line_raises():
    i = FN.index("if r.status in (200, 201):")
    nxt = FN[i:i + 400]
    assert "_slot_taken = False" in nxt
    assert nxt.index("_slot_taken = False") < nxt.index("broadcast_signal_to_subscribers")


# ------------------------------------------- the early halt, once it is used up
def test_once_used_up_the_cycle_halts_entries_with_a_quiet_reason(monkeypatch):
    import prop_bot
    c, s = counter()
    monkeypatch.setattr(prop_bot, "_ENTRY_COUNTER", c)
    assert run(prop_bot.daily_entry_ceiling_reason()) is None
    for _ in range(3):
        run(c.reserve())
    why = run(prop_bot.daily_entry_ceiling_reason())
    assert why.startswith(prop_bot.ENTRY_CEILING_PREFIX) and "3 of 3" in why and "exits are unaffected" in why


def test_an_unreadable_count_does_not_halt_the_cycle_early(monkeypatch):
    """The per-order gate refuses in that case; the cycle itself carries on."""
    import prop_bot
    c, _ = counter(fail_load=True)
    monkeypatch.setattr(prop_bot, "_ENTRY_COUNTER", c)
    assert run(prop_bot.daily_entry_ceiling_reason()) is None


def test_switched_off_the_cycle_is_never_halted_by_it(monkeypatch):
    import prop_bot
    monkeypatch.setenv(cap.MAX_ENTRIES_ENV, "off")
    assert run(prop_bot.daily_entry_ceiling_reason()) is None


def test_the_cycle_checks_the_ceiling_last_so_pause_and_market_closed_keep_their_label():
    i = SRC.index("entries_halted = await market_entry_block_reason(session)")
    j = SRC.index("entries_halted = await daily_entry_ceiling_reason()")
    assert i < j < i + 600


def test_the_ceiling_is_reported_quietly_like_market_closed():
    assert "[ENTRY CEILING]" in SRC and '"entry ceiling"' in SRC


# ------------------------------------------------------------- the swing bot
SWING = open("alpaca_swing_bot.py", encoding="utf-8").read()
PLACE = SWING[SWING.index("async def place_order("):]
PLACE = PLACE[:PLACE.index("\nasync def run_intraday_check(")]


def test_the_swing_bot_buys_count_against_the_same_ceiling():
    assert "from prop_bot import _ENTRY_COUNTER" in PLACE
    assert PLACE.index("_ENTRY_COUNTER.reserve()") < PLACE.index("session.post(")


def test_the_swing_bot_only_counts_buys_never_sells():
    b = PLACE.index('if side == "buy":')
    i = PLACE.index("_ENTRY_COUNTER.reserve()")
    p = PLACE.index("url = f")
    assert b < i < p


class _R:
    def __init__(self, status, body):
        self.status, self._b = status, body

    async def json(self):
        return self._b

    async def text(self):
        return str(self._b)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _S:
    def __init__(self, *responses):
        self.responses, self.posts = list(responses), 0

    def post(self, url, headers=None, json=None):
        self.posts += 1
        return self.responses.pop(0)


@pytest.fixture
def swing(monkeypatch):
    import alpaca_swing_bot as sw
    import prop_bot
    c, s = counter()
    monkeypatch.setattr(prop_bot, "_ENTRY_COUNTER", c)

    async def not_paused():
        return False
    monkeypatch.setattr(prop_bot, "are_alpaca_entries_paused", not_paused)
    monkeypatch.setattr(sw, "get_headers", lambda: {})
    monkeypatch.setattr(sw, "get_base_url", lambda: "https://x")
    return sw, c, s


def test_the_swing_bot_cannot_buy_a_fourth_time_today(swing):
    sw, c, s = swing
    ok = _R(200, {"id": "o1"})
    sess = _S(ok, ok, ok, ok)
    got = [run(sw.place_order(sess, "SLV", 1, "buy")) for _ in range(4)]
    assert [bool(g) for g in got] == [True, True, True, False]
    assert sess.posts == 3                                  # the fourth never left


def test_the_swing_bot_sells_after_the_ceiling_is_reached(swing):
    sw, c, s = swing
    for _ in range(3):
        run(c.reserve())
    sess = _S(_R(200, {"id": "o9"}))
    assert run(sw.place_order(sess, "SLV", 1, "sell"))["id"] == "o9"
    assert sess.posts == 1 and s.d[cap.day_key()] == 3.0    # exits are not counted


def test_a_venue_rejection_does_not_use_up_the_swing_bots_slot(swing):
    sw, c, s = swing
    sess = _S(_R(403, {"message": "no"}), _R(200, {"id": "o2"}))
    assert run(sw.place_order(sess, "SLV", 1, "buy")) is None
    assert s.d[cap.day_key()] == 0.0
    assert run(sw.place_order(sess, "SLV", 1, "buy"))["id"] == "o2"
    assert s.d[cap.day_key()] == 1.0


def test_both_bots_draw_on_one_allowance(swing):
    sw, c, s = swing
    run(c.reserve())          # prop_bot's entries
    run(c.reserve())
    sess = _S(_R(200, {"id": "a"}), _R(200, {"id": "b"}))
    assert run(sw.place_order(sess, "NVDA", 1, "buy"))["id"] == "a"
    assert run(sw.place_order(sess, "NVDA", 1, "buy")) is None   # 3 of 3 used
    assert sess.posts == 1


def test_the_swing_bot_gives_the_slot_back_only_on_an_explicit_rejection():
    assert PLACE.count("_counter.release()") == 1
    i = PLACE.index("_counter.release()")
    assert "r.status not in (200, 201)" in PLACE[:i]


def test_the_swing_bot_still_has_exactly_one_order_function():
    assert SWING.count('/v2/orders"') == 1


def test_a_refused_entry_returns_false_like_any_other_refusal():
    i = FN.index("_ENTRY_COUNTER.reserve()")
    block = FN[i:i + 900]
    assert "return False" in block and block.index("return False") < block.index("_slot_taken =")
