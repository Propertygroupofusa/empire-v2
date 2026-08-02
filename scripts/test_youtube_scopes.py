"""
Test that the declared YouTube scopes cover the API calls actually made.

WHY THIS EXISTS
---------------
YouTube analytics has been returning 403 insufficientPermissions on every
call. The cause is a scope mismatch that nothing in the system could
surface:

  get_daily_analytics queries the youtubeAnalytics v2 API with
    metrics='views,estimatedMinutesWatched,estimatedRevenue,
             monetizedPlaybacks,impressions'

  but SCOPES listed only youtube.readonly and youtube, and NEITHER grants
  access to the Analytics API at all.

The failure mode is nasty because SCOPES is documentation, not
configuration - it is referenced nowhere else in the codebase. The
refresh token is minted out of band (OAuth Playground), carrying whatever
scopes were ticked there. So a wrong SCOPES list does not fail at
startup, does not fail at build() time, and does not fail on token
refresh. It fails as an opaque 403 the first time analytics is queried,
which reads like a transient API problem rather than a permanent
misconfiguration.

This test makes the mismatch a build failure instead: it reads the
metrics the code actually requests and asserts SCOPES covers every one.

Scope requirements are from the YouTube Analytics API reference:
  yt-analytics.readonly           view/watch-time/impression metrics
  yt-analytics-monetary.readonly  revenue and monetized-playback metrics

No network, no credentials - pure source analysis, so it runs in CI.
"""
import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
MODULE = REPO / "youtube_monetization.py"

ANALYTICS_RO = "https://www.googleapis.com/auth/yt-analytics.readonly"
ANALYTICS_MONEY = "https://www.googleapis.com/auth/yt-analytics-monetary.readonly"

# metric -> scope that grants it
MONETARY_METRICS = {
    "estimatedRevenue", "estimatedAdRevenue", "estimatedRedPartnerRevenue",
    "grossRevenue", "cpm", "playbackBasedCpm", "adImpressions",
    "monetizedPlaybacks",
}


def declared_scopes(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "SCOPES" for t in n.targets):
            return [e.value for e in n.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return None


def requested_metrics(tree):
    """Every metrics='...' keyword actually passed to a call.

    Read off the AST rather than regexed out of the source: a regex over
    raw text also matches the metric list quoted inside the explanatory
    comment above SCOPES, which produced a phantom '# monetizedPlaybacks'
    metric. Comments cannot reach the AST.
    """
    out = set()
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        for kw in n.keywords:
            if kw.arg == "metrics" and isinstance(kw.value, ast.Constant) \
                    and isinstance(kw.value.value, str):
                out |= {x.strip() for x in kw.value.value.split(",") if x.strip()}
    return out


def main():
    failures = []
    src = MODULE.read_text()
    tree = ast.parse(src)

    scopes = declared_scopes(tree)
    if scopes is None:
        print("  FAIL  no SCOPES list found in youtube_monetization.py")
        return 1
    print(f"  declared scopes ({len(scopes)}):")
    for s in scopes:
        print(f"      {s}")

    metrics = requested_metrics(tree)
    if not metrics:
        failures.append("no metrics=... found; does this module still query "
                        "the Analytics API? this test's premise may be stale")
    else:
        print(f"\n  metrics requested: {', '.join(sorted(metrics))}")

    # any analytics metric at all requires the readonly analytics scope
    if metrics and ANALYTICS_RO not in scopes:
        failures.append(f"analytics metrics are requested but {ANALYTICS_RO} is "
                        f"not in SCOPES - every youtubeAnalytics call 403s")

    monetary = metrics & MONETARY_METRICS
    if monetary and ANALYTICS_MONEY not in scopes:
        failures.append(f"revenue metrics {sorted(monetary)} require "
                        f"{ANALYTICS_MONEY}, which is not in SCOPES")
    elif monetary:
        print(f"  monetary metrics {sorted(monetary)} covered by "
              f"yt-analytics-monetary.readonly")

    # A 403 here is permanent, so it must not be logged as a generic error
    # that reads like something which might clear on its own.
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "get_daily_analytics"),
              None)
    if fn is None:
        failures.append("get_daily_analytics not found")
    else:
        body = ast.get_source_segment(src, fn) or ""
        if "insufficientPermissions" not in body:
            failures.append("get_daily_analytics does not special-case "
                            "insufficientPermissions - a permanent scope "
                            "misconfiguration logs as a generic transient error")
        else:
            print("  403 insufficientPermissions is reported as a scope problem")

    print()
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  PASS  declared scopes cover every metric the code requests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
