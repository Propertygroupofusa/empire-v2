#!/usr/bin/env python3
"""The scheduler's four gates, and the ordering that stops a duplicate.

WHAT IS BEING PROTECTED

An upload is public, attributed to the owner, and cannot be taken back
cleanly. So every gate here fails CLOSED, and the tests are written to prove
the refusals rather than the happy path:

  * armed mode is the exact word "arm" and nothing else;
  * a channel's own publishing_enabled still governs after arming;
  * an unset daily limit is not "unlimited", it is no;
  * a content_key already present is never posted again, whatever became of
    the first attempt;
  * an unreadable ledger refuses, because that is the one state in which a
    duplicate could go out silently.

And the ordering: the row is CLAIMED before the upload is attempted. A test
simulates the crash that ordering exists for - YouTube accepted the video,
we never recorded success - and proves the next pass does not post it again.

Run: python3 test_youtube_scheduler_worker.py
"""
import asyncio
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("YOUTUBE_CLIENT_ID", "cid")
os.environ.setdefault("YOUTUBE_CLIENT_SECRET", "csec")

import sqlalchemy.ext.asyncio as sa_async

import database
import youtube_channels as yc
import youtube_scheduler_worker as w

_failures = []
_passes = 0


def ok(label, condition, detail=""):
    global _passes
    if condition:
        _passes += 1
        print(f"  ok   {label}")
    else:
        _failures.append(f"{label}{(' - ' + detail) if detail else ''}")
        print(f"  FAIL {label}{(' - ' + detail) if detail else ''}")


TOKEN = "1//token-SECRET"


async def fresh():
    eng = sa_async.create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    factory = sa_async.async_sessionmaker(eng, expire_on_commit=False)
    database.get_session_factory = lambda: factory
    yc.get_session_factory = lambda: factory
    return factory


def run(body):
    async def _w():
        sf = await fresh()
        return await body(sf)
    return asyncio.run(_w())


def arm():
    os.environ["YOUTUBE_SCHEDULER"] = "arm"


def disarm():
    os.environ.pop("YOUTUBE_SCHEDULER", None)


async def _ready_channel(sf, label="news", limit=2):
    await yc.add_channel(label, TOKEN, daily_upload_limit=limit)
    await yc.set_publishing(label, True)


def test_the_mode_is_one_exact_word():
    # Note what is NOT in this list: "ARM " with a trailing space. Surrounding
    # whitespace IS stripped, matching resting_stops.normalise_mode and
    # adaptive_stop.adopted_mode, whose docstring gives the reason - a trailing
    # space on a Railway variable is an accident, not a different intention.
    # What must never arm it is a DIFFERENT VALUE that someone assumed meant
    # yes. My first version of this test expected "ARM " to be refused and the
    # code was right to accept it.
    for raw in ("1", "true", "on", "armed", "yes", "arm the scheduler", "",
                None):
        if raw is None:
            disarm()
        else:
            os.environ["YOUTUBE_SCHEDULER"] = raw
        ok(f"{raw!r} does not arm the scheduler", w.is_armed() is False,
           f"current_mode()={w.current_mode()}")
    os.environ["YOUTUBE_SCHEDULER"] = "arm"
    ok("only the exact word 'arm' arms it", w.is_armed() is True)
    os.environ["YOUTUBE_SCHEDULER"] = "ARM"
    ok("and it is case-insensitive on that one word", w.is_armed() is True)
    os.environ["YOUTUBE_SCHEDULER"] = "  arm  "
    ok("surrounding whitespace is stripped, as the other switches do",
       w.is_armed() is True)
    disarm()


def test_disarmed_refuses_everything():
    async def body(sf):
        await _ready_channel(sf)
        disarm()
        allowed, why = await w.may_upload(sf, "news", "topic-1")
        ok("a disarmed scheduler refuses", allowed is False)
        ok("and names the switch", "YOUTUBE_SCHEDULER" in why, why)
        r = await w.run_once(sf)
        ok("run_once reports not armed", r["armed"] is False, str(r))
        ok("and lists no channels", r["channels"] == [], str(r))
    run(body)


def test_arming_is_not_permission_for_a_given_channel():
    """THE GATE THAT MATTERS MOST. Arming starts the loop; it does not
    authorise any particular channel to post."""
    async def body(sf):
        arm()
        await yc.add_channel("news", TOKEN, daily_upload_limit=2)
        # publishing_enabled is still False.
        allowed, why = await w.may_upload(sf, "news", "topic-1")
        ok("an armed loop still refuses a publishing-off channel",
           allowed is False, why)
        ok("and says which gate stopped it", "publishing is OFF" in why, why)
        ok("run_once lists no enabled channels",
           (await w.run_once(sf))["channels"] == [])
        await yc.set_publishing("news", True)
        allowed2, why2 = await w.may_upload(sf, "news", "topic-1")
        ok("once enabled it is allowed", allowed2 is True, why2)
        disarm()
    run(body)


def test_an_unset_daily_limit_is_not_unlimited():
    async def body(sf):
        arm()
        await yc.add_channel("news", TOKEN)          # no limit given
        await yc.set_publishing("news", True)
        allowed, why = await w.may_upload(sf, "news", "topic-1")
        ok("an unset daily limit refuses", allowed is False, why)
        ok("and says why that is not 'unlimited'",
           "unanswered question" in why, why)
        for bad in (0, -3):
            async with sf() as db:
                from models import YouTubeChannel
                from sqlalchemy import select
                row = (await db.execute(select(YouTubeChannel))).scalars().first()
                row.daily_upload_limit = bad
                await db.commit()
            a2, w2 = await w.may_upload(sf, "news", "topic-1")
            ok(f"a limit of {bad} refuses", a2 is False, w2)
        disarm()
    run(body)


def test_the_daily_limit_is_actually_enforced():
    async def body(sf):
        arm()
        await _ready_channel(sf, limit=2)
        for i in range(2):
            allowed, why = await w.may_upload(sf, "news", f"topic-{i}")
            ok(f"upload {i + 1} of 2 is allowed", allowed is True, why)
            got = await w.claim(sf, "news", f"topic-{i}", f"Title {i}", "private")
            ok(f"and claiming {i + 1} returns a row", got is not None)
        allowed, why = await w.may_upload(sf, "news", "topic-3")
        ok("the third is refused", allowed is False, why)
        ok("and the count is reported", "2 of 2" in why, why)
        disarm()
    run(body)


def test_a_claimed_but_unsettled_upload_still_consumes_its_slot():
    """An attempt whose outcome is unknown has spent a slot. Counting only
    successes would let a channel retry past its own limit on exactly the
    day something is going wrong."""
    async def body(sf):
        arm()
        await _ready_channel(sf, limit=1)
        await w.claim(sf, "news", "topic-1", "T", "private")   # never settled
        allowed, why = await w.may_upload(sf, "news", "topic-2")
        ok("an unsettled claim still counts against the limit",
           allowed is False, why)
        ok("and it is reported as used", "1 of 1" in why, why)
        disarm()
    run(body)


def test_the_same_content_is_never_posted_twice():
    async def body(sf):
        arm()
        await _ready_channel(sf, limit=10)
        await w.claim(sf, "news", "topic-1", "T", "private")
        await w.settle(sf, 1, True, video_id="vid123")
        allowed, why = await w.may_upload(sf, "news", "topic-1")
        ok("a successfully posted key is refused", allowed is False, why)
        ok("and says it is a duplicate", "never posted twice" in why, why)
        ok("claiming it again returns None",
           await w.claim(sf, "news", "topic-1", "T", "private") is None)
        disarm()
    run(body)


def test_a_failed_upload_is_also_not_retried_blindly():
    """A failure might have been a failure AFTER YouTube accepted it. The
    row stands and the key is not reused until a person decides."""
    async def body(sf):
        arm()
        await _ready_channel(sf, limit=10)
        rid = await w.claim(sf, "news", "topic-1", "T", "private")
        await w.settle(sf, rid, False, error="timeout")
        allowed, why = await w.may_upload(sf, "news", "topic-1")
        ok("a failed key is still not reposted automatically",
           allowed is False, why)
        ok("because the first attempt's outcome cannot be assumed",
           "never posted twice" in why, why)
        disarm()
    run(body)


def test_the_crash_window_the_claim_ordering_exists_for():
    """Simulates: YouTube accepted the video, the process died before the
    row could be settled. The next pass must NOT post it again."""
    async def body(sf):
        arm()
        await _ready_channel(sf, limit=10)
        await w.claim(sf, "news", "weekly-1", "Weekly", "private")
        # ... crash here. Nothing settles the row.
        allowed, why = await w.may_upload(sf, "news", "weekly-1")
        ok("the next pass does not repost it", allowed is False, why)
        ok("the claim alone was enough to prevent the duplicate",
           "never posted twice" in why, why)
        disarm()
    run(body)


def test_an_unreadable_ledger_refuses():
    async def body(sf):
        arm()
        await _ready_channel(sf)

        def boom():
            raise RuntimeError("database gone")

        allowed, why = await w.may_upload(boom, "news", "topic-1")
        ok("an unreadable ledger refuses", allowed is False, why)
        ok("and says a duplicate is the risk it is avoiding",
           "duplicate" in why, why)
        disarm()
    run(body)


def test_privacy_defaults_to_private_and_public_must_be_written_down():
    ok("the default is private", w.DEFAULT_PRIVACY == "private")
    for profile, want in (
        (None, "private"),
        ({}, "private"),
        ({"privacy": "nonsense"}, "private"),
        ({"privacy": "PUBLIC"}, "public"),
        ({"privacy": "unlisted"}, "unlisted"),
    ):
        got, why = w.resolve_privacy(profile)
        ok(f"{profile!r} -> {want}", got == want, f"got {got} ({why})")
    bad, why = w.resolve_privacy({"privacy": "worldwide"})
    ok("an unrecognised value falls back to private, not through to public",
       bad == "private", why)
    ok("and says what it rejected", "worldwide" in why, why)


def test_run_once_reports_without_posting():
    async def body(sf):
        arm()
        await _ready_channel(sf, limit=3)
        r = await w.run_once(sf)
        ok("it reports the enabled channel", len(r["channels"]) == 1, str(r))
        ch = r["channels"][0]
        ok("with its privacy resolved", ch["privacy"] == "private", str(ch))
        ok("and its usage against the limit",
           ch["uploads_today"] == 0 and ch["daily_upload_limit"] == 3, str(ch))
        ok("and it claimed nothing by looking",
           (await w.may_upload(sf, "news", "topic-1"))[0] is True)
        disarm()
    run(body)


def test_the_loop_never_dies_on_an_error():
    """A scheduler that raises schedules nothing, for ever."""
    import inspect
    src = inspect.getsource(w.run_periodically)
    ok("the pass is wrapped", "except Exception" in src)
    ok("and the error is recorded rather than raised",
       "HEARTBEAT" in src and "raise" not in src.split("except Exception")[1][:300],
       src[:200])
    ok("and it sleeps between passes", "sleep" in src)


def test_zz_nothing_above_failed():
    assert not _failures, f"{len(_failures)} checks failed: {_failures}"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for t in TESTS:
        print(f"\n== {t.__name__}")
        try:
            t()
        except BaseException as e:
            ok(f"{t.__name__} ran to completion", False,
               f"raised {type(e).__name__}: {e}")
    disarm()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED, {_passes} passed:")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"all {_passes} scheduler checks passed")
