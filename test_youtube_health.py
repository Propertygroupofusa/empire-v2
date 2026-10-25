#!/usr/bin/env python3
"""The YouTube health check must answer, not crash, and never leak a secret.

WHY THIS FILE EXISTS

The check is the thing you reach for when the channel has gone quiet, so its
failure modes matter more than most. Three in particular:

  * it must not need the network to tell you credentials are missing;
  * it must never put a client secret or refresh token in a response body -
    the endpoint is admin-guarded, but "behind auth" is not "safe to print";
  * an unreachable Google must come back UNKNOWN, never FAIL. A timeout is
    not a dead token, and telling someone their token is dead when it is not
    sends them to re-run a consent flow for nothing.

And the one the whole file is really for: a green credential check with
nothing scheduled is the state this repo was actually in, and every natural
check a person runs passes in it.

Run: python3 test_youtube_health.py
"""
import asyncio
import os
import sys

import youtube_health as yh

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


SECRETS = {
    "YOUTUBE_CLIENT_ID": "client-id-SECRET-VALUE-1234567890",
    "YOUTUBE_CLIENT_SECRET": "client-secret-SECRET-VALUE-abcdef",
    "YOUTUBE_REFRESH_TOKEN": "1//refresh-SECRET-VALUE-zyxwvu",
    "YOUTUBE_API_KEY": "AIza-SECRET-VALUE-0987654321",
}


class _Env:
    """Set the four variables for one block, restore them after."""

    def __init__(self, **kw):
        self.kw = kw
        self.old = {}

    def __enter__(self):
        for k in list(SECRETS) + list(self.kw):
            self.old[k] = os.environ.get(k)
        for k in SECRETS:
            os.environ.pop(k, None)
        for k, v in self.kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run(**env):
    with _Env(**env):
        return asyncio.run(yh.youtube_health())


def test_missing_credentials_are_reported_without_touching_the_network():
    """The commonest state, and the one that must not need Google to answer."""
    out = run()
    ok("it returns a verdict", "verdict" in out, str(out)[:200])
    cred = next(c for c in out["checks"] if c["name"] == "credentials_configured")
    ok("credentials are FAIL", cred["status"] == yh.FAIL, str(cred))
    ok("and it names which ones are missing",
       set(cred["missing"]) == {"YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET",
                                "YOUTUBE_REFRESH_TOKEN"}, str(cred["missing"]))
    downstream = {c["name"]: c["status"] for c in out["checks"]
                  if c["name"] in ("refresh_token_works", "scopes_allow_upload",
                                   "channel_reachable")}
    ok("the checks that could not run are UNKNOWN, not FAIL",
       set(downstream.values()) == {yh.UNKNOWN}, str(downstream))
    ok("and the verdict says it is blocked",
       out["can_upload_right_now"] is False)


def test_the_api_key_alone_is_not_treated_as_credentials():
    """YOUTUBE_API_KEY authenticates public reads and cannot upload. Counting
    it would send someone hunting for the wrong missing variable."""
    out = run(YOUTUBE_API_KEY=SECRETS["YOUTUBE_API_KEY"])
    cred = next(c for c in out["checks"] if c["name"] == "credentials_configured")
    ok("an API key alone is still FAIL", cred["status"] == yh.FAIL, str(cred))
    ok("and the API key is not listed as missing",
       "YOUTUBE_API_KEY" not in cred["missing"], str(cred["missing"]))


def test_no_secret_value_ever_appears_in_the_output():
    """THE ONE THAT MATTERS MOST. Checked against the whole serialised body,
    so a value cannot hide in a nested field somebody adds later."""
    import json
    out = run(**SECRETS)
    body = json.dumps(out)
    for name, value in SECRETS.items():
        ok(f"{name}'s value is absent from the response", value not in body,
           "a credential value is being returned")
    ok("'SECRET-VALUE' appears nowhere at all", "SECRET-VALUE" not in body,
       "something is echoing a credential")
    cred = next(c for c in out["checks"] if c["name"] == "credentials_configured")
    ok("lengths are reported instead",
       cred["lengths"]["YOUTUBE_CLIENT_ID"] == len(SECRETS["YOUTUBE_CLIENT_ID"]),
       str(cred["lengths"]))


def test_an_unreachable_google_is_unknown_not_a_dead_token():
    """A timeout is not a revoked credential. Reporting it as FAIL sends
    someone to re-run a consent flow over a network blip."""
    class _Boom:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise TimeoutError("connect timeout")

        async def get(self, *a, **k):
            raise TimeoutError("connect timeout")

    real = yh.httpx.AsyncClient
    yh.httpx.AsyncClient = lambda *a, **k: _Boom()
    try:
        out = run(**SECRETS)
    finally:
        yh.httpx.AsyncClient = real
    tok = next(c for c in out["checks"] if c["name"] == "refresh_token_works")
    ok("an unreachable Google is UNKNOWN", tok["status"] == yh.UNKNOWN, str(tok))
    ok("it is not called FAIL", tok["status"] != yh.FAIL)
    ok("and it says the result is a network one, not a credential one",
       "network" in tok["detail"].lower(), tok["detail"])
    ok("the overall verdict refuses to claim it can upload",
       out["can_upload_right_now"] is False)
    # The verdict names the FAILing check, not this UNKNOWN one. My first
    # version expected "INCONCLUSIVE" and the code was right: a confirmed
    # blocker outranks a check that could not run. Only when NOTHING failed
    # does an unknown become the headline.
    ok("the verdict leads with a confirmed blocker when there is one",
       out["verdict"].startswith("BLOCKED:"), out["verdict"])
    ok("and the unreachable check is still carried in the body",
       any(c["status"] == yh.UNKNOWN for c in out["checks"]), out["verdict"])


def test_a_dead_refresh_token_is_named_with_googles_own_words():
    class _Resp:
        status_code = 400
        text = '{"error":"invalid_grant","error_description":"Token has been expired or revoked."}'

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

        async def get(self, *a, **k):
            raise AssertionError("must not reach the channel read")

    real = yh.httpx.AsyncClient
    yh.httpx.AsyncClient = lambda *a, **k: _Client()
    try:
        out = run(**SECRETS)
    finally:
        yh.httpx.AsyncClient = real
    tok = next(c for c in out["checks"] if c["name"] == "refresh_token_works")
    ok("a refused token is FAIL", tok["status"] == yh.FAIL, str(tok))
    ok("Google's own wording is relayed", "invalid_grant" in tok["detail"])
    ok("and the 7-day Testing-mode trap is named, because it is the usual cause",
       "Testing" in tok["detail"], tok["detail"])
    ok("it says the token cannot be repaired",
       "cannot be repaired" in tok["detail"], tok["detail"])


def test_a_token_that_cannot_upload_is_caught_even_though_it_is_valid():
    """The quiet one: a live, correctly-issued token granted the wrong scope.
    Everything about it looks healthy."""
    chk = yh._scope_check(["https://www.googleapis.com/auth/youtube.readonly"])
    ok("a readonly-only token FAILs the upload check", chk["status"] == yh.FAIL,
       str(chk))
    upload_only = yh._scope_check([yh.UPLOAD_SCOPE])
    ok("upload scope passes", upload_only["status"] == yh.OK, str(upload_only))
    ok("but it is told it cannot EDIT existing videos",
       upload_only["can_edit_existing"] is False
       and "CANNOT edit" in upload_only["detail"], str(upload_only))
    full = yh._scope_check([yh.MANAGE_SCOPE])
    ok("the manage scope can do both", full["status"] == yh.OK
       and full["can_edit_existing"] is True, str(full))
    ok("an empty scope list is UNKNOWN, not a pass",
       yh._scope_check([])["status"] == yh.UNKNOWN)


def test_the_scheduling_check_is_the_one_that_catches_the_real_state():
    """Credentials can be perfect and the channel still silent. This repo was
    in exactly that state: nothing in production calls the uploader."""
    chk = yh._scheduling_check()
    ok("it returns a verdict", chk["name"] == "uploads_are_scheduled", str(chk))
    ok("the status is one of the three",
       chk["status"] in (yh.OK, yh.FAIL, yh.UNKNOWN), str(chk))
    # TWO DIFFERENT FAILING STATES, and they need different fixes:
    # nothing wired into the service at all, or wired and DISARMED. Both must
    # FAIL - "wired" is not "posting", and scoring a disarmed scheduler as OK
    # would recreate the blindness this check exists to remove. Asserted on
    # the property (a FAIL names the thing to change) rather than on either
    # one's wording, since this test previously pinned the first state's
    # sentence and broke the moment the scheduler was wired in.
    if chk["status"] == yh.FAIL:
        detail = chk["detail"]
        names_the_fix = ("/publish/youtube/" in detail        # nothing wired
                         or "YOUTUBE_SCHEDULER" in detail)    # wired, disarmed
        ok("a FAIL names what has to change", names_the_fix, detail)
        ok("and it does not read as a credential problem",
           "token" not in detail.lower(), detail)
    else:
        ok("it names what does the scheduling", bool(chk.get("callers")),
           str(chk))


def test_being_wired_in_is_not_scored_as_posting():
    """The specific regression: once the worker was imported by main.py, a
    naive check would see the reference and call scheduling healthy while
    the switch was still off and nothing was being posted."""
    import youtube_scheduler_worker as w
    old = os.environ.get(w.MODE_ENV)
    try:
        os.environ.pop(w.MODE_ENV, None)
        off = yh._scheduling_check()
        ok("disarmed is a FAIL, not an OK", off["status"] == yh.FAIL, str(off))
        ok("and it says disarmed rather than missing",
           "DISARMED" in off["detail"], off["detail"])
        ok("and names the exact word needed", "'arm'" in off["detail"],
           off["detail"])
        os.environ[w.MODE_ENV] = "arm"
        on = yh._scheduling_check()
        ok("armed is an OK", on["status"] == yh.OK, str(on))
        ok("but it still says the per-channel gate applies",
           "publishing_enabled" in on["detail"], on["detail"])
        ok("and that the daily limit applies",
           "daily_upload_limit" in on["detail"], on["detail"])
    finally:
        if old is None:
            os.environ.pop(w.MODE_ENV, None)
        else:
            os.environ[w.MODE_ENV] = old


def test_a_green_credential_run_still_cannot_claim_uploads_work():
    """The whole point. A token that works is not a channel that updates."""
    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": "at", "scope": yh.MANAGE_SCOPE}

    class _Chan:
        status_code = 200

        def json(self):
            return {"items": [{"id": "UC123",
                               "snippet": {"title": "PGUSA"}}]}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

        async def get(self, *a, **k):
            return _Chan()

    real = yh.httpx.AsyncClient
    yh.httpx.AsyncClient = lambda *a, **k: _Client()
    try:
        out = run(**SECRETS)
    finally:
        yh.httpx.AsyncClient = real
    names = {c["name"]: c["status"] for c in out["checks"]}
    ok("credentials, token, scopes and channel are all OK",
       all(names[n] == yh.OK for n in ("credentials_configured",
                                       "refresh_token_works",
                                       "scopes_allow_upload",
                                       "channel_reachable")), str(names))
    sched = names["uploads_are_scheduled"]
    if sched == yh.FAIL:
        ok("and yet it does NOT claim uploads will happen",
           out["can_upload_right_now"] is False,
           "four green checks are being read as a working pipeline")
        ok("the verdict names scheduling as the blocker",
           "uploads_are_scheduled" in out["verdict"], out["verdict"])
    else:
        ok("with scheduling wired, it does claim uploads work",
           out["can_upload_right_now"] is True, str(out["verdict"]))


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
    print(f"all {_passes} youtube-health checks passed")
