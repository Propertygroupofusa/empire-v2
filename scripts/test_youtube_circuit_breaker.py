"""
Test that a permanent YouTube 403 pauses further API calls.

WHY THIS EXISTS
---------------
403 insufficientPermissions is not transient. The refresh token was
granted without the scopes these calls need, and refreshing it changes
nothing - it will fail identically forever until the token is
regenerated out of band.

But revenue_dashboard polls three of these methods on a loop, so that one
permanent misconfiguration produced an unbounded stream of identical
ERROR lines in Railway, burying everything else. #130 made the message
accurate; it did not stop the calls.

So the tracker now latches: the first insufficientPermissions response
records the reason, and every subsequent call returns it without touching
the network.

WHAT IS ASSERTED
----------------
  1. before any failure, calls go through to the API (the breaker is not
     tripped by default - a breaker that starts closed would silently
     disable YouTube for everyone)
  2. the first 403 is logged ONCE, with the required scopes named
  3. after that, NO further network calls are made by any of the three
     polled methods - asserted by counting real invocations, not by
     trusting the return value
  4. the refusal explains what to do and marks itself paused
  5. a NON-403 error does not trip the breaker, since transient failures
     must still be retried
  6. the latch lives in memory only, so a restart clears it and a
     regenerated token resumes automatically without anyone flipping a
     flag back

Uses a fake service that counts calls rather than a live API - the
property under test is "does it stop calling", which needs a call
counter, not a network.
"""
import os
import sys
import pathlib
import types

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Keep the constructor from building real Google clients.
os.environ.pop("YOUTUBE_REFRESH_TOKEN", None)
os.environ.pop("YOUTUBE_API_KEY", None)

import youtube_monetization as ym  # noqa: E402


class FakeHttpError(Exception):
    """Shaped like googleapiclient's HttpError string form."""
    def __init__(self):
        super().__init__(
            '<HttpError 403 when requesting https://youtube.googleapis.com/'
            'youtube/v3/search?forMine=true returned "Request had '
            'insufficient authentication scopes.". Details: "[{\'message\': '
            "'Insufficient Permission', 'reason': 'insufficientPermissions'}]\">")


class CountingService:
    """Raises `exc` on every terminal .execute(), counting attempts."""
    def __init__(self, exc):
        self.exc, self.calls = exc, 0

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        self.calls += 1
        raise self.exc


def make_tracker(exc):
    t = object.__new__(ym.YouTubeMonetizationTracker)
    t.youtube_api_key = t.youtube_client_id = None
    t.youtube_client_secret = t.youtube_refresh_token = None
    t.metrics_cache = {}
    t.earnings_cache = {}
    t._scope_error = None
    svc = CountingService(exc)
    t.youtube_service = svc
    t.youtube_analytics_service = svc
    return t, svc


def main():
    failures = []
    logged = []
    ym.log.error = lambda *a, **k: logged.append(a[0] % a[1:] if len(a) > 1 else a[0])

    # ── 1. breaker starts open ─────────────────────────────────────────
    t, svc = make_tracker(FakeHttpError())
    if t._scope_blocked():
        failures.append("breaker is tripped before any failure - YouTube would "
                        "be disabled for everyone from startup")
    else:
        print("  breaker starts open (calls are allowed)")

    # ── 2 & 3. first 403 latches; later calls make no network calls ────
    t.get_top_videos()
    after_first = svc.calls
    if after_first != 1:
        failures.append(f"expected 1 API attempt, got {after_first}")

    r2 = t.get_top_videos()
    r3 = t.get_channel_metrics()
    r4 = t.get_daily_analytics()
    if svc.calls != after_first:
        failures.append(f"breaker did not stop the calls: {svc.calls - after_first} "
                        f"further API attempts after the 403")
    else:
        print(f"  after the first 403: {svc.calls} total API attempt(s), "
              f"3 further method calls made none")

    scope_logs = [m for m in logged if "insufficientPermissions" in m]
    if len(scope_logs) != 1:
        failures.append(f"expected exactly ONE 403 log line, got {len(scope_logs)} "
                        f"- the log spam is the thing being fixed")
    else:
        if "yt-analytics.readonly" not in scope_logs[0]:
            failures.append("the 403 log does not name the required scopes")
        if "PAUSING" not in scope_logs[0]:
            failures.append("the 403 log does not say calls are being paused")
        print("  logged exactly once, naming the required scopes")

    # ── 4. the refusal is actionable ───────────────────────────────────
    for name, r in (("get_top_videos", r2), ("get_channel_metrics", r3),
                    ("get_daily_analytics", r4)):
        if r.get("error") != "insufficientPermissions" or not r.get("paused"):
            failures.append(f"{name} returned {r!r}, expected the paused refusal")
        if "regenerate" not in r.get("detail", ""):
            failures.append(f"{name} refusal does not say to regenerate the token")
    print("  all three methods return the same actionable refusal")

    # ── 5. a transient error must NOT trip the breaker ─────────────────
    t2, svc2 = make_tracker(RuntimeError("connection reset by peer"))
    t2.get_top_videos()
    t2.get_top_videos()
    if t2._scope_blocked():
        failures.append("a non-403 error tripped the breaker - transient "
                        "failures must still be retried")
    elif svc2.calls != 2:
        failures.append(f"transient errors were not retried: {svc2.calls} calls")
    else:
        print("  a transient (non-403) error does not trip the breaker")

    # ── 6. in-memory only, so a restart resumes ────────────────────────
    src = (REPO / "youtube_monetization.py").read_text()
    if "_scope_error" not in src:
        failures.append("_scope_error latch is gone")
    for persist in ("os.environ[", "open(", "json.dump"):
        seg = src[src.index("def _latch_scope_error"):src.index("def _init_services")]
        if persist in seg:
            failures.append(f"the latch persists via {persist!r} - it must clear "
                            f"on restart so a regenerated token resumes "
                            f"automatically")
    t3, _ = make_tracker(FakeHttpError())
    if t3._scope_blocked():
        failures.append("a fresh tracker starts latched - a restart would not "
                        "recover after the token is fixed")
    else:
        print("  a fresh instance starts unlatched (restart resumes)")

    print()
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  PASS  a permanent 403 pauses YouTube calls, logged once, "
          "self-clearing on restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
