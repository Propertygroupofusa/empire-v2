"""Every bars request must ask for history that exists, and use the
newest of it.

THE DEFECT, confirmed live on 28 Sep at 16:36Z from META's own refusal:

    "Only 17 of the required 21 15-min bars are available right now"

Alpaca's /v2/stocks/{symbol}/bars with no `start` returns only the
CURRENT DAY's bars. get_price_momentum() needs 21 of them for a 20-bar
SMA - 5h15m of session - so from the 09:30 ET open until roughly
14:45 ET it returned None for EVERY symbol, every trading day. No entry
could be considered and, until ef86ee9, no held position's stop could
be evaluated either.

This codebase had already met the same bug in get_price_rsi() and
recorded it in a comment - "for roughly the first ~4 hours of every
single trading day ... the scanner skipped every symbol" - and
responded by lowering the bar requirement from 50 to 15. That works
only where the statistic tolerates it. A 20-bar SMA needs 20 bars.
There is no smaller honest number, so the window has to be the thing
that changes.

THE SECOND HALF MATTERS AS MUCH AS THE FIRST. Alpaca returns bars
oldest-first and truncates to `limit` from the START. A wide `start`
with a small `limit` is exactly how a price several days stale gets
served as "current" - which on an exit path is worse than no price at
all, because it looks like the stop ran.
"""
import ast
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import prop_bot  # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prop_bot.py")
with open(SRC, encoding="utf-8") as fh:
    SOURCE = fh.read()
TREE = ast.parse(SOURCE)


def _func(name):
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _bars_urls(fn):
    """Every bars URL built inside a function, as flat text - joined from
    the f-string pieces so a multi-line URL reads as one string."""
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.JoinedStr):
            text = "".join(
                v.value if isinstance(v, ast.Constant) and isinstance(v.value, str)
                else "<expr>"
                for v in node.values)
            if "/bars?" in text:
                out.append(text)
    return out


BAR_FETCHERS = ["get_price_rsi", "get_price_momentum", "get_higher_tf_trend"]


# ── the window ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", BAR_FETCHERS)
def test_every_bars_request_names_a_start(name):
    """No `start` means today only, which is the whole bug."""
    urls = _bars_urls(_func(name))
    assert urls, f"{name} builds no bars URL"
    for u in urls:
        assert "start=" in u, f"{name} requests bars with no start window: {u}"


@pytest.mark.parametrize("name", BAR_FETCHERS)
def test_the_start_is_computed_not_hardcoded(name):
    """A literal date would go stale the day after it was written - this
    codebase's signature bug."""
    src = ast.get_source_segment(SOURCE, _func(name))
    assert "bars_start_iso(" in src, f"{name} does not compute its start window"
    assert not re.search(r"start=\d{4}-\d{2}-\d{2}", src), \
        f"{name} hardcodes a start date"


@pytest.mark.parametrize("name", BAR_FETCHERS)
def test_a_wide_window_is_paired_with_a_limit_that_reaches_today(name):
    """start + a small limit serves the OLDEST bars in the window. That
    is a stale price wearing a current price's name."""
    for u in _bars_urls(_func(name)):
        m = re.search(r"limit=(\d+)", u)
        assert m, f"{name} builds a bars URL with no limit: {u}"
        assert int(m.group(1)) >= 1000, (
            f"{name} asks for a multi-day window but caps at "
            f"limit={m.group(1)} - it would read the oldest bars, not the "
            f"newest")


@pytest.mark.parametrize("name", BAR_FETCHERS)
def test_the_newest_bars_are_the_ones_used(name):
    """The tail slice is what turns a wide window into a current price."""
    src = ast.get_source_segment(SOURCE, _func(name))
    assert re.search(r"bars = bars\[-\d+:\]", src), \
        f"{name} never takes the tail of a multi-day window"


# ── bars_start_iso itself ──────────────────────────────────────────────

def test_start_is_in_the_past():
    start = prop_bot.bars_start_iso(6)
    parsed = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert parsed < datetime.now(timezone.utc)


def test_start_is_the_requested_distance_back():
    start = prop_bot.bars_start_iso(6)
    parsed = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - parsed).total_seconds() / 86400
    assert 5.9 < age_days < 6.1


def test_start_is_in_the_format_alpaca_accepts():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
                        prop_bot.bars_start_iso(1))


def test_start_moves_with_the_clock():
    """A start computed once at import would age into uselessness."""
    a = prop_bot.bars_start_iso(6)
    b = prop_bot.bars_start_iso(3)
    assert a < b


# ── the windows are big enough for what each caller needs ──────────────

def test_the_intraday_window_covers_a_weekend_and_a_holiday():
    """21 15-min bars is 5h15m of session. A Tuesday after a Monday
    holiday needs to reach back past Friday."""
    assert prop_bot.BARS_LOOKBACK_DAYS_INTRADAY >= 5


def test_the_hourly_window_covers_fifty_session_hours():
    """get_higher_tf_trend needs 50 1-hour bars - about 7.7 trading days,
    so at least 11 calendar days, and more to clear a holiday."""
    assert prop_bot.BARS_LOOKBACK_DAYS_HOURLY >= 12


def test_the_intraday_window_holds_more_bars_than_the_tail_takes():
    """6 calendar days of 5-min session bars is ~4 x 78 = 312, well past
    the 50 the tail takes - so the slice never runs short."""
    session_5min_bars_per_day = 78
    trading_days = prop_bot.BARS_LOOKBACK_DAYS_INTRADAY - 2  # worst-case weekend
    assert trading_days * session_5min_bars_per_day > 50


def test_a_twenty_bar_sma_still_requires_twenty_bars():
    """The floor is not the thing to move. get_price_rsi's floor was
    lowered 50 -> 15 to work around this same bug; that option does not
    exist for an SMA20, and must not be reached for again."""
    assert prop_bot.MOMENTUM_SMA_PERIOD == 20
    src = ast.get_source_segment(SOURCE, _func("get_price_momentum"))
    assert "MIN_BARS = MOMENTUM_SMA_PERIOD + 1" in src


# ── the age of the price is visible ────────────────────────────────────

def test_the_momentum_scan_reports_how_old_its_price_is():
    src = ast.get_source_segment(SOURCE, _func("get_price_momentum"))
    assert "bar_age_seconds" in src


def test_the_age_does_not_gate_the_scan():
    """Refusing on an old bar would block exits outside session hours -
    the exact failure ef86ee9 and 37fd02b were spent removing.

    Scoped by AST to the branch that TESTS bar_age_seconds, not by
    slicing the source text: the first version of this test split on a
    string and swept up the function's own outer `except: return None`,
    failing on code it had no opinion about."""
    fn = _func("get_price_momentum")
    checked = [n for n in ast.walk(fn)
               if isinstance(n, ast.If)
               and "bar_age_seconds" in ast.dump(n.test)]
    assert checked, "nothing in get_price_momentum tests bar_age_seconds"
    for branch in checked:
        returns = [n for n in ast.walk(branch) if isinstance(n, ast.Return)]
        assert not returns, \
            "the bar-age check must not be able to refuse a scan"
