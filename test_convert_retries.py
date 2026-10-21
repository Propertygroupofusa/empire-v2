"""A rate-limited USDC -> USD conversion is asked again, not given up on.

Measured 2026-10-21: four coin picker buys in one night stopped on the
conversion that funds them - RAY 03:34Z and USELESS 03:45Z and LCX 08:56Z at
the quote, BAT 09:22Z at the commit - every one "HTTP 429" with an empty
body, and every one bought on a later pass. A 429 is the venue's rate
limiter turning the request away before it does anything with it.

What is pinned here, against fakes - no venue is ever called
(run as written: python3 test_convert_retries.py):
  * a 429 (or a 5xx) on the QUOTE is asked again, a bounded number of times
  * a 429 on the COMMIT is asked again with the SAME trade id - never a
    second quote, so a retry can only ever commit the one amount quoted
  * a 5xx on the commit is reported, not repeated
  * Retry-After is honoured and capped
  * the USD read that confirms a conversion is read again, fresh, before the
    buy is put off
"""
import asyncio
import json
import sys

import pytest

import crypto_btc_compound_bot as engine
import crypto_grid_bot as grid


def run(coro):
    return asyncio.run(coro)


class _Resp:
    def __init__(self, status, payload=None, headers=None):
        self.status, self._payload = status, payload
        self.headers = headers or {}

    async def json(self):
        return self._payload

    async def text(self):
        return "" if self._payload is None else json.dumps(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    """Answers each POST from a queue and records where it went."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, url, headers=None, data=None, timeout=None, json=None):
        self.posts.append(url)
        return self.responses.pop(0)


def _quote(trade_id="T1", amount="15.00"):
    return _Resp(200, {"trade": {"id": trade_id, "status": "TRADE_STATUS_CREATED",
                                 "total": {"value": amount, "currency": "USD"},
                                 "total_fee": {"amount": {"value": "0", "currency": "USD"}}}})


def _commit(status="TRADE_STATUS_COMPLETED"):
    return _Resp(200, {"trade": {"id": "T1", "status": status}})


def _busy(retry_after=None):
    return _Resp(429, None, {"Retry-After": retry_after} if retry_after else None)


QUOTE = "/api/v3/brokerage/convert/quote"
COMMIT = "/api/v3/brokerage/convert/trade/T1"


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    monkeypatch.setattr(engine, "_auth_headers", lambda m, p, b="": {"Authorization": "test"})
    monkeypatch.delenv("GRID_SPEND_USDC", raising=False)
    waits = []

    async def no_wait(seconds):
        waits.append(seconds)

    monkeypatch.setattr(engine.asyncio, "sleep", no_wait)
    return waits


# ------------------------------------------------------------- the quote
def test_a_rate_limited_quote_is_asked_again_and_converts(_fakes):
    s = _Session(_busy(), _quote(), _commit())
    got, err = run(engine.convert_usdc_to_usd(s, 15.0))
    assert (got, err) == (15.0, None)
    assert [u.split("brokerage")[1] for u in s.posts] == [
        "/convert/quote", "/convert/quote", "/convert/trade/T1"]
    assert _fakes == [1.0]


def test_a_venue_hiccup_on_the_quote_is_asked_again(_fakes):
    s = _Session(_Resp(503, {"error": "unavailable"}), _quote(), _commit())
    assert run(engine.convert_usdc_to_usd(s, 15.0)) == (15.0, None)


def test_the_waits_back_off(_fakes):
    s = _Session(_busy(), _busy(), _busy(), _quote(), _commit())
    assert run(engine.convert_usdc_to_usd(s, 15.0)) == (15.0, None)
    assert _fakes == [1.0, 2.0, 4.0]


def test_a_quote_still_refused_after_every_attempt_moves_nothing(_fakes):
    s = _Session(*[_busy() for _ in range(engine.CONVERT_RETRY_ATTEMPTS)])
    got, err = run(engine.convert_usdc_to_usd(s, 15.0))
    assert got is None and "convert quote HTTP 429" in err
    assert len(s.posts) == engine.CONVERT_RETRY_ATTEMPTS
    assert all(u.endswith(QUOTE) for u in s.posts)          # never a commit
    assert len(_fakes) == engine.CONVERT_RETRY_ATTEMPTS - 1  # no wait after the last


def test_a_refusal_the_venue_will_not_reconsider_is_not_repeated(_fakes):
    s = _Session(_Resp(400, {"error": "INVALID_ARGUMENT"}))
    got, err = run(engine.convert_usdc_to_usd(s, 15.0))
    assert got is None and "HTTP 400" in err and len(s.posts) == 1 and _fakes == []


def test_retry_after_is_honoured_and_capped(_fakes):
    s = _Session(_busy("2"), _busy("600"), _quote(), _commit())
    assert run(engine.convert_usdc_to_usd(s, 15.0)) == (15.0, None)
    assert _fakes == [2.0, engine.CONVERT_RETRY_MAX_WAIT_SECONDS]


def test_an_unreadable_retry_after_falls_back_to_the_backoff(_fakes):
    s = _Session(_busy("soon"), _quote(), _commit())
    assert run(engine.convert_usdc_to_usd(s, 15.0)) == (15.0, None)
    assert _fakes == [1.0]


# ------------------------------------------------------------ the commit
def test_a_rate_limited_commit_is_asked_again_for_the_same_trade(_fakes):
    s = _Session(_quote(), _busy(), _commit())
    assert run(engine.convert_usdc_to_usd(s, 15.0)) == (15.0, None)
    assert s.posts[0].endswith(QUOTE)
    assert s.posts[1].endswith(COMMIT) and s.posts[2].endswith(COMMIT)


def test_a_commit_refused_every_time_never_takes_a_second_quote(_fakes):
    s = _Session(_quote(), *[_busy() for _ in range(engine.CONVERT_RETRY_ATTEMPTS)])
    engine._BALANCES_CACHE["by_currency"] = {"USD": 1.0}
    got, err = run(engine.convert_usdc_to_usd(s, 15.0))
    assert got is None and "convert commit HTTP 429" in err
    assert sum(u.endswith(QUOTE) for u in s.posts) == 1
    assert sum(u.endswith(COMMIT) for u in s.posts) == engine.CONVERT_RETRY_ATTEMPTS
    assert engine._BALANCES_CACHE["by_currency"] is None


def test_a_venue_error_on_the_commit_is_reported_not_repeated(_fakes):
    """A 5xx may have reached the trade. Asking again cannot help, so it is
    reported, and the next balance read goes to the venue."""
    s = _Session(_quote(), _Resp(500, {"error": "internal"}))
    engine._BALANCES_CACHE["by_currency"] = {"USD": 1.0}
    got, err = run(engine.convert_usdc_to_usd(s, 15.0))
    assert got is None and "convert commit HTTP 500" in err
    assert len(s.posts) == 2 and _fakes == []
    assert engine._BALANCES_CACHE["by_currency"] is None


def test_last_nights_four_refusals_each_convert_now(_fakes):
    """RAY, USELESS and LCX at the quote, BAT at the commit."""
    for answers in ((_busy(), _quote(), _commit()),
                    (_busy(), _quote(), _commit()),
                    (_busy(), _quote(), _commit()),
                    (_quote(), _busy(), _commit())):
        assert run(engine.convert_usdc_to_usd(_Session(*answers), 15.0)) == (15.0, None)


# ---------------------------------------------- confirming the USD landed
class _Engine:
    """USD reads from a script; a conversion that always lands."""

    def __init__(self, usd_reads):
        self.reads = list(usd_reads)
        self.read_count = 0
        self.dropped = 0
        self.usdc = 3000.0

    async def get_usd_balance(self, session):
        self.read_count += 1
        v = self.reads.pop(0)
        return (v, None) if v is not None else (None, "HTTP 429")

    async def get_usdc_balance(self, session):
        return self.usdc, None

    async def convert_usdc_to_usd(self, session, amount):
        return amount, None

    def invalidate_balance_cache(self, reason=""):
        self.dropped += 1


@pytest.fixture
def grid_waits(monkeypatch):
    waits = []

    async def no_wait(seconds):
        waits.append(seconds)

    monkeypatch.setattr(grid.asyncio, "sleep", no_wait)
    return waits


def test_a_rate_limited_confirmation_is_read_again(monkeypatch, grid_waits):
    # before: $0 USD; after the conversion: 429, then the $16 that landed
    e = _Engine([0.0, None, 16.0])
    monkeypatch.setattr(grid, "engine", e)
    ok, note = run(grid.ensure_usd_for_buy(None, 15.0))
    assert ok is True and "converted" in note
    assert grid_waits == [1.0] and e.dropped == 1


def test_usd_that_shows_a_moment_late_is_waited_for(monkeypatch, grid_waits):
    e = _Engine([0.0, 0.0, 0.0, 16.0])
    monkeypatch.setattr(grid, "engine", e)
    ok, _ = run(grid.ensure_usd_for_buy(None, 15.0))
    assert ok is True and grid_waits == [1.0, 2.0] and e.dropped == 2


def test_usd_that_never_shows_still_puts_the_buy_off(monkeypatch, grid_waits):
    e = _Engine([0.0, None, None, None])
    monkeypatch.setattr(grid, "engine", e)
    ok, note = run(grid.ensure_usd_for_buy(None, 15.0))
    assert ok is False and "buy waits for next cycle" in note
    assert e.read_count == 1 + 1 + len(grid.USDC_CONFIRM_WAITS_SECONDS)


def test_usd_that_is_there_at_once_is_not_read_again(monkeypatch, grid_waits):
    e = _Engine([0.0, 16.0])
    monkeypatch.setattr(grid, "engine", e)
    ok, _ = run(grid.ensure_usd_for_buy(None, 15.0))
    assert ok is True and grid_waits == [] and e.read_count == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
