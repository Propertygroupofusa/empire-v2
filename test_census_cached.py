"""A reading with an age is not a fallback literal.

The venue rate-limits. Measured 2026-09-28 13:53Z, ten consecutive
/account-census calls: four came back `available: false, error:
"accounts HTTP 429"`. A caller that reads the census on every page load
would therefore render "unavailable" ~40% of the time.

census_cached serves the LAST REAL READING with stale=True and
age_seconds set, so a page can say "as of 40s ago" instead of going
blank - and returns the refusal untouched when it has never had a
reading, so "unavailable" still means unavailable.

The distinction this file exists to protect: a measured number carrying
its own age is honest; a number that was never measured (the 483.00
literal this replaced) is not, however confident it looks.
"""
import asyncio
import account_census as ac


def _reset():
    ac._CENSUS_CACHE.update(census=None, at=0.0)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class _Census:
    """Stands in for account_census.census with a scripted result list."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    async def __call__(self, session, tracked_usd=None):
        self.calls += 1
        return self.results[min(self.calls - 1, len(self.results) - 1)]


GOOD = {"available": True, "total_usd": 10897.37, "cash_usd": 2374.82, "coin_usd": 8522.55}
GOOD2 = {"available": True, "total_usd": 11000.00, "cash_usd": 2374.82, "coin_usd": 8625.18}
R429 = {"available": False, "error": "accounts HTTP 429", "detail": ""}


def _patch(fake):
    ac.census = fake


def _restore(orig):
    ac.census = orig


def test_a_fresh_read_is_marked_fresh():
    _reset(); orig = ac.census; _patch(_Census(GOOD))
    try:
        r = _run(ac.census_cached(None))
        assert r["available"] and r["total_usd"] == 10897.37
        assert r["stale"] is False and r["age_seconds"] == 0.0
    finally:
        _restore(orig)


def test_a_429_serves_the_last_real_reading_and_says_how_old_it_is():
    _reset(); orig = ac.census; fake = _Census(GOOD, R429)
    _patch(fake)
    try:
        _run(ac.census_cached(None))                       # primes the cache
        r = _run(ac.census_cached(None, max_age_seconds=0))  # forces a re-read, which 429s
        assert fake.calls == 2
        assert r["available"] is True
        assert r["total_usd"] == 10897.37        # the real number, not a literal
        assert r["stale"] is True
        assert r["age_seconds"] >= 0.0
        assert r["stale_reason"] == "accounts HTTP 429"
    finally:
        _restore(orig)


def test_with_no_reading_ever_the_refusal_passes_straight_through():
    # This is the case that must NOT be papered over. No cache, no
    # number - the caller shows "unavailable" and that is correct.
    _reset(); orig = ac.census; _patch(_Census(R429))
    try:
        r = _run(ac.census_cached(None))
        assert r["available"] is False
        assert r["error"] == "accounts HTTP 429"
        assert "total_usd" not in r
    finally:
        _restore(orig)


def test_a_reading_too_old_to_describe_the_account_is_not_served():
    _reset(); orig = ac.census; _patch(_Census(GOOD, R429))
    try:
        _run(ac.census_cached(None))
        r = _run(ac.census_cached(None, max_age_seconds=0, max_stale_seconds=-1))
        assert r["available"] is False, "an hour-old book must not be served as the account"
    finally:
        _restore(orig)


def test_inside_the_ttl_the_venue_is_not_called_again():
    # The whole point: one upstream call shared across callers, so
    # adding a reader does not add rate-limit pressure.
    _reset(); orig = ac.census; fake = _Census(GOOD, GOOD2)
    _patch(fake)
    try:
        a = _run(ac.census_cached(None))
        b = _run(ac.census_cached(None))
        c = _run(ac.census_cached(None))
        assert fake.calls == 1, fake.calls
        assert b["total_usd"] == a["total_usd"] == 10897.37
        assert c["stale"] is True
    finally:
        _restore(orig)


def test_a_later_good_read_replaces_the_cached_one():
    _reset(); orig = ac.census; fake = _Census(GOOD, GOOD2)
    _patch(fake)
    try:
        _run(ac.census_cached(None))
        r = _run(ac.census_cached(None, max_age_seconds=0))
        assert fake.calls == 2
        assert r["total_usd"] == 11000.00
        assert r["stale"] is False and r["age_seconds"] == 0.0
    finally:
        _restore(orig)


def test_an_exception_is_treated_as_a_refusal_not_a_crash():
    _reset(); orig = ac.census

    async def boom(session, tracked_usd=None):
        raise RuntimeError("socket closed")

    ac.census = boom
    try:
        r = _run(ac.census_cached(None))
        assert r["available"] is False
        assert "RuntimeError" in r["error"]
    finally:
        _restore(orig)


def test_the_cached_copy_cannot_be_mutated_by_a_caller():
    """Every caller gets its own dict; mutating one must not poison the
    store or the next reader.

    The first draft of this test mutated the return of the FIRST call,
    which travels the fresh path and is a copy either way - so it passed
    against a deliberately broken `out = cached` and proved nothing. A
    surviving mutant is the test's fault, not the mutant's. It now
    mutates a value returned from the CACHED path, which is the only one
    that can alias the stored dict.
    """
    _reset(); orig = ac.census; _patch(_Census(GOOD))
    try:
        _run(ac.census_cached(None))            # primes the cache
        b = _run(ac.census_cached(None))        # served FROM the cache
        assert b["stale"] is True, "second call must come from cache for this test to mean anything"
        b["total_usd"] = 1.23
        b["injected"] = "should not survive"
        c = _run(ac.census_cached(None))
        assert c["total_usd"] == 10897.37, "a caller's edit reached the stored book"
        assert "injected" not in c
        assert ac._CENSUS_CACHE["census"]["total_usd"] == 10897.37
    finally:
        _restore(orig)


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS {name}")
            except AssertionError as e:
                fails += 1; print(f"  FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
