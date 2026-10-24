#!/usr/bin/env python3
"""Multi-channel YouTube credentials: the rules that must not bend.

Three properties, each of which is the kind of thing that only announces
itself after it has already gone wrong:

  * A CONFIGURED CHANNEL IS NOT A CHANNEL THAT MAY POST. Rows get added by
    automations, imports and half-finished setup. If existing were enough,
    a typo would start a channel publishing to the public. publishing_enabled
    defaults False, add_channel takes no argument to override it, and resolve
    refuses by default.

  * NO TOKEN VALUE EVER LEAVES THE MODULE. Not in a listing, not in a repr,
    not in a log line. "It is behind an admin key" is not the same as "safe
    to print" - these responses end up in screenshots and support threads.

  * A FAILURE SAYS WHICH FAILURE. "Could not post" is not actionable. Not
    configured, never authorised, publishing off, and no OAuth client are
    four different problems with four different fixes.

Run: python3 test_youtube_channels.py
"""
import asyncio
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import database
import youtube_channels as yc
from models import YouTubeChannel

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


TOKEN = "1//SUPER-SECRET-REFRESH-TOKEN-do-not-print"
CID = "client-id-SECRET"
CSEC = "client-secret-SECRET"

# A known starting state. Several tests below deliberately clear these to
# check what happens without a shared OAuth app, and tests run in name order,
# so without a baseline here the FIRST test's result depends on which other
# test ran before it - exactly the kind of order-dependence that makes a
# suite report a problem that is not there.
os.environ["YOUTUBE_CLIENT_ID"] = CID
os.environ["YOUTUBE_CLIENT_SECRET"] = CSEC


async def fresh_db():
    """A clean in-memory database for one test."""
    database.engine = None
    database._session_factory = getattr(database, "_session_factory", None)
    import sqlalchemy.ext.asyncio as sa_async
    eng = sa_async.create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    factory = sa_async.async_sessionmaker(eng, expire_on_commit=False)
    database.get_session_factory = lambda: factory
    yc.get_session_factory = lambda: factory
    return factory


def run(coro):
    async def _wrap():
        await fresh_db()
        return await coro()
    return asyncio.run(_wrap())


def test_a_new_channel_cannot_publish_until_it_is_told_to():
    """THE ONE THAT MATTERS MOST."""
    async def body():
        added = await yc.add_channel("news", TOKEN)
        ok("a new channel has publishing OFF",
           added["publishing_enabled"] is False, str(added))
        r = await yc.resolve("news")
        ok("and resolving it for publishing is refused", r.ok is False, repr(r))
        ok("with the reason said plainly",
           "publishing is OFF" in (r.reason or ""), r.reason)
        ok("it hands back no token while refused", r.refresh_token is None)
        # A read-only caller may still look at it.
        r2 = await yc.resolve("news", require_publishing=False)
        ok("a read-only caller can still resolve it", r2.ok is True, repr(r2))
        ok("and gets the token for that purpose", r2.refresh_token == TOKEN)
        # Turning it on is its own act.
        after = await yc.set_publishing("news", True)
        ok("turning it on is a separate call",
           after["publishing_enabled"] is True, str(after))
        r3 = await yc.resolve("news")
        ok("and then publishing resolves", r3.ok is True, repr(r3))
    run(body)


def test_add_channel_has_no_way_to_enable_publishing_in_one_step():
    """Checked on the signature, so the guard cannot be argued away by a
    caller passing publishing_enabled=True."""
    import inspect
    params = list(inspect.signature(yc.add_channel).parameters)
    ok("add_channel takes no publishing argument",
       not any("publish" in p for p in params), str(params))
    ok("set_publishing exists as its own call",
       callable(getattr(yc, "set_publishing", None)))


def test_no_token_value_is_ever_returned_or_printed():
    async def body():
        await yc.add_channel("news", TOKEN, client_id=CID, client_secret=CSEC)
        import json
        listing = json.dumps(await yc.list_channels())
        ok("the listing contains no refresh token", TOKEN not in listing)
        ok("nor the client secret", CSEC not in listing, listing[:200])
        ok("'SECRET' appears nowhere in the listing", "SECRET" not in listing,
           listing[:200])
        ok("but presence and length are reported",
           '"has_refresh_token": true' in listing
           and f'"refresh_token_length": {len(TOKEN)}' in listing, listing[:300])
        r = await yc.resolve("news", require_publishing=False)
        ok("a Resolution's repr hides the token", TOKEN not in repr(r), repr(r))
        ok("and hides the client secret", CSEC not in repr(r), repr(r))
    run(body)


def test_each_failure_names_itself():
    async def body():
        missing = await yc.resolve("nope")
        ok("an unknown label says so", missing.ok is False
           and "no channel configured" in missing.reason, missing.reason)

        async with yc.get_session_factory()() as db:
            db.add(YouTubeChannel(label="blank", refresh_token="",
                                  publishing_enabled=True))
            await db.commit()
        blank = await yc.resolve("blank")
        ok("a channel with no token says it was never authorised",
           blank.ok is False and "never authorised" in blank.reason,
           blank.reason)

        # No per-channel client and no env default either.
        for k in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET"):
            os.environ.pop(k, None)
        await yc.add_channel("noclient", TOKEN)
        await yc.set_publishing("noclient", True)
        nc = await yc.resolve("noclient")
        ok("a channel with no OAuth client anywhere says that",
           nc.ok is False and "no OAuth client of its own" in nc.reason,
           nc.reason)

        # With the service default present it resolves, sharing the app.
        os.environ["YOUTUBE_CLIENT_ID"] = CID
        os.environ["YOUTUBE_CLIENT_SECRET"] = CSEC
        shared = await yc.resolve("noclient")
        ok("and falls back to the shared OAuth app when there is one",
           shared.ok is True and shared.client_id == CID, repr(shared))
    run(body)


def test_the_existing_env_channel_keeps_working_untouched():
    """Nothing may break the moment this lands. A service with only the old
    environment variables set must publish exactly as it did."""
    os.environ["YOUTUBE_CLIENT_ID"] = CID
    os.environ["YOUTUBE_CLIENT_SECRET"] = CSEC
    os.environ["YOUTUBE_REFRESH_TOKEN"] = TOKEN
    r = yc.env_channel()
    ok("the env channel resolves", r.ok is True, repr(r))
    ok("with its token", r.refresh_token == TOKEN)
    ok("resolve() with no label falls back to it",
       asyncio.run(yc.resolve()).ok is True)
    for k in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET",
              "YOUTUBE_REFRESH_TOKEN"):
        os.environ.pop(k, None)
    gone = yc.env_channel()
    ok("and with the variables gone it says which are missing",
       gone.ok is False and "YOUTUBE_REFRESH_TOKEN" in gone.reason, gone.reason)
    os.environ["YOUTUBE_CLIENT_ID"] = CID
    os.environ["YOUTUBE_CLIENT_SECRET"] = CSEC


def test_two_channels_are_genuinely_independent():
    """The whole point of the table: one token per channel, because that is
    how Google issues them."""
    async def body():
        await yc.add_channel("alpha", TOKEN + "-A")
        await yc.add_channel("beta", TOKEN + "-B")
        await yc.set_publishing("alpha", True)
        a = await yc.resolve("alpha")
        b = await yc.resolve("beta", require_publishing=False)
        ok("each channel carries its own token",
           a.refresh_token != b.refresh_token, "the tokens are shared")
        ok("alpha publishes", a.ok is True, repr(a))
        ok("beta does not, independently", b.ok is True)
        bpub = await yc.resolve("beta")
        ok("and beta is still refused for publishing", bpub.ok is False,
           repr(bpub))
        ok("a duplicate label is refused",
           await _raises(yc.add_channel("alpha", "x")), "duplicates allowed")
    run(body)


async def _raises(coro):
    try:
        await coro
        return False
    except ValueError:
        return True


def test_a_channel_cannot_be_added_without_what_it_needs():
    async def body():
        ok("no label is refused", await _raises(yc.add_channel("", TOKEN)))
        ok("no token is refused", await _raises(yc.add_channel("x", "")))
    run(body)


def test_health_writeback_only_records_the_venues_own_answer():
    async def body():
        await yc.add_channel("news", TOKEN)
        await yc.record_token_result("news", True, channel_id="UC_real",
                                     channel_title="Real Title")
        row = (await yc.list_channels())[0]
        ok("a success records the channel id the venue gave",
           row["channel_id"] == "UC_real", str(row))
        ok("and clears any previous error", row["last_token_error"] is None)
        ok("and stamps when it last worked", row["last_token_ok_at"] is not None)
        await yc.record_token_result("news", False, error="invalid_grant")
        row = (await yc.list_channels())[0]
        ok("a failure records the error", row["last_token_error"] == "invalid_grant")
        ok("and does NOT erase the channel id it already knew",
           row["channel_id"] == "UC_real", str(row))
        ok("writing back to an unknown channel is a no-op, not a crash",
           await yc.record_token_result("ghost", True) is False)
    run(body)


def test_the_uploader_itself_refuses_a_channel_that_may_not_publish():
    """The guard is worth nothing if the upload path does not go through it.

    Asserted end to end against the real youtube_upload_bot entry point: it
    must raise BEFORE any network call, naming the channel and the reason.
    A caller that got an empty token instead would read Google's refusal as a
    channel problem and go hunting in the wrong place.
    """
    import youtube_upload_bot as up

    async def body():
        await yc.add_channel("offchan", TOKEN)

        called = {"n": 0}

        class _NoNet:
            async def __aenter__(self):
                called["n"] += 1
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                raise AssertionError("a token request was made for a channel "
                                     "that may not publish")

        real = up.httpx.AsyncClient
        up.httpx.AsyncClient = lambda *a, **k: _NoNet()
        try:
            try:
                await up.get_access_token("offchan")
                raised = None
            except RuntimeError as e:
                raised = str(e)
        finally:
            up.httpx.AsyncClient = real

        ok("the uploader raises for a publishing-off channel",
           raised is not None, "it proceeded")
        ok("it names the channel", "offchan" in (raised or ""), str(raised))
        ok("and the reason", "publishing is OFF" in (raised or ""), str(raised))
        ok("and it never reached the network", called["n"] == 0,
           "a token request went out before the check")
        ok("the exception carries no token", TOKEN not in (raised or ""),
           "the refusal is leaking the credential")

        # Turned on, the same call now gets as far as the network.
        await yc.set_publishing("offchan", True)
        up.httpx.AsyncClient = lambda *a, **k: _NoNet()
        try:
            try:
                await up.get_access_token("offchan")
            except AssertionError:
                reached = True
            except Exception:
                reached = False
        finally:
            up.httpx.AsyncClient = real
        ok("and with publishing on it does reach the token request", reached,
           "the check above may be passing for the wrong reason")

    run(body)


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
    print()
    if _failures:
        print(f"{len(_failures)} FAILED, {_passes} passed:")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"all {_passes} youtube-channel checks passed")
