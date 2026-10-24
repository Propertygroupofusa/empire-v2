"""Why is nothing reaching YouTube? Answered by asking, not by guessing.

WHAT THIS IS FOR

"The bot stopped putting things on YouTube" has at least five separate
causes, and from outside they all look identical - silence:

  1. no credentials configured at all
  2. the refresh token has been revoked or expired
  3. the token is live but was granted the wrong scope
  4. the token is live and correctly scoped but points at no channel
  5. everything above is fine and NOTHING EVER CALLS THE UPLOADER

Number 5 is the one that cost the most time here, because every check a
person naturally runs - is the key set, does the token work - passes while
the feature produces nothing. It is the same shape as the retired model id
that 404'd thirteen call sites for weeks while the daily brief logged
"generated, persisted, and sent" one line later (5785b42).

READ-ONLY, AND IT SAYS SO

Nothing here uploads, edits, deletes or publishes. It mints an access token
the same way the uploader does - that changes no account state - and makes
one GET for the channel name. A diagnostic that can modify the channel is
not one anybody will run when they are already nervous about the channel.

IT NEVER PRINTS A SECRET. Client id, client secret and refresh token are
reported as present/absent and by length only. Google's own error text IS
returned, because that string is the whole answer when the token is dead
and withholding it just means another round trip.

AN UNREADABLE ANSWER IS NOT A PASSING ONE. Every check is OK, FAIL or
UNKNOWN, and UNKNOWN is never folded into either - a network timeout is not
a working token and it is not a broken one.
"""
import logging
import os

import httpx

log = logging.getLogger("youtube_health")

TOKEN_URL = "https://oauth2.googleapis.com/token"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"

#: The scope the uploader actually needs. youtube.upload alone can post a new
#: video but cannot edit one that already exists, which is why the SEO
#: backfill needs the broader youtube scope - a token granted only .upload
#: looks perfectly healthy right up until an update is attempted.
UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
MANAGE_SCOPE = "https://www.googleapis.com/auth/youtube"

OK, FAIL, UNKNOWN = "OK", "FAIL", "UNKNOWN"


def _result(name, status, detail, **extra):
    out = {"name": name, "status": status, "detail": detail}
    out.update(extra)
    return out


def _credentials_present():
    """Which of the four variables exist. Lengths only, never values."""
    fields = {
        "YOUTUBE_CLIENT_ID": os.getenv("YOUTUBE_CLIENT_ID", ""),
        "YOUTUBE_CLIENT_SECRET": os.getenv("YOUTUBE_CLIENT_SECRET", ""),
        "YOUTUBE_REFRESH_TOKEN": os.getenv("YOUTUBE_REFRESH_TOKEN", ""),
        "YOUTUBE_API_KEY": os.getenv("YOUTUBE_API_KEY", ""),
    }
    lengths = {k: len(v) for k, v in fields.items()}
    # The API key is NOT required for uploading - it authenticates public
    # read calls only. Listing it as required sends people hunting for the
    # wrong thing, so it is reported and excluded from the verdict.
    required = ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET",
                "YOUTUBE_REFRESH_TOKEN")
    missing = [k for k in required if not fields[k]]
    if missing:
        return _result(
            "credentials_configured", FAIL,
            f"{', '.join(missing)} not set on this service. Nothing can "
            f"authenticate, so no upload has been attempted at all.",
            lengths=lengths, missing=missing)
    return _result(
        "credentials_configured", OK,
        "all three OAuth variables are set (lengths only shown - values are "
        "never returned by this endpoint)",
        lengths=lengths, missing=[])


async def _token_exchange(client):
    """Mint an access token exactly as the uploader does. (result, token, scopes)."""
    try:
        r = await client.post(TOKEN_URL, data={
            "client_id": os.getenv("YOUTUBE_CLIENT_ID", ""),
            "client_secret": os.getenv("YOUTUBE_CLIENT_SECRET", ""),
            "refresh_token": os.getenv("YOUTUBE_REFRESH_TOKEN", ""),
            "grant_type": "refresh_token",
        })
    except Exception as e:
        # Could not reach Google at all. Not a dead token - unknown.
        return _result(
            "refresh_token_works", UNKNOWN,
            f"could not reach {TOKEN_URL} ({type(e).__name__}: {str(e)[:200]}). "
            f"Whether the token is valid is UNKNOWN - this is a network "
            f"result, not a credential one."), None, None

    if r.status_code != 200:
        body = r.text[:400]
        # Google's own wording, because it is the answer. invalid_grant is
        # the one that matters most: it means revoked, expired, or issued by
        # a different client than the one asking.
        hint = ""
        if "invalid_grant" in body:
            hint = (" — invalid_grant means the refresh token is dead: "
                    "revoked from the Google Account's linked-apps page, "
                    "expired because the OAuth app is still in Testing "
                    "publishing status (those expire after 7 days), or "
                    "issued by a different client id than the one sending "
                    "it. It cannot be repaired; a new one has to be minted "
                    "by re-running the consent flow.")
        elif "invalid_client" in body:
            hint = (" — invalid_client means the client id or secret does "
                    "not match the OAuth app that issued the token.")
        return _result("refresh_token_works", FAIL,
                       f"Google refused the refresh token "
                       f"(HTTP {r.status_code}): {body}{hint}"), None, None

    data = r.json()
    token = data.get("access_token")
    if not token:
        return _result("refresh_token_works", FAIL,
                       "Google returned 200 with no access_token in the "
                       "body - unexpected, and nothing can upload without "
                       "one."), None, None
    scopes = (data.get("scope") or "").split()
    return _result("refresh_token_works", OK,
                   "Google issued an access token, so the refresh token is "
                   "live and the client id/secret match it."), token, scopes


def _scope_check(scopes):
    if scopes is None:
        return _result("scopes_allow_upload", UNKNOWN,
                       "no token was issued, so the granted scopes could "
                       "not be read")
    if not scopes:
        return _result("scopes_allow_upload", UNKNOWN,
                       "Google returned no scope list with the token, so "
                       "what it permits is UNKNOWN", scopes=[])
    can_upload = UPLOAD_SCOPE in scopes or MANAGE_SCOPE in scopes
    can_edit = MANAGE_SCOPE in scopes
    if not can_upload:
        return _result(
            "scopes_allow_upload", FAIL,
            f"this token grants {scopes} - neither {UPLOAD_SCOPE} nor "
            f"{MANAGE_SCOPE} is among them, so it cannot post a video no "
            f"matter how healthy it looks.", scopes=scopes)
    return _result(
        "scopes_allow_upload", OK,
        "the token can upload"
        + ("; it can also edit existing videos, so the SEO backfill works too"
           if can_edit
           else f"; it CANNOT edit existing videos - that needs {MANAGE_SCOPE}, "
                f"so title/description updates on videos already live will fail"),
        scopes=scopes, can_edit_existing=can_edit)


async def _channel_check(client, token):
    if not token:
        return _result("channel_reachable", UNKNOWN,
                       "no access token, so the channel could not be read")
    try:
        r = await client.get(CHANNELS_URL,
                             params={"part": "snippet,contentDetails",
                                     "mine": "true"},
                             headers={"Authorization": f"Bearer {token}"})
    except Exception as e:
        return _result("channel_reachable", UNKNOWN,
                       f"could not reach the YouTube API "
                       f"({type(e).__name__}: {str(e)[:200]})")
    if r.status_code != 200:
        return _result("channel_reachable", FAIL,
                       f"YouTube refused the channel read "
                       f"(HTTP {r.status_code}): {r.text[:300]}")
    items = (r.json() or {}).get("items") or []
    if not items:
        return _result(
            "channel_reachable", FAIL,
            "the token is valid but the Google Account behind it has no "
            "YouTube channel, so there is nowhere for an upload to go.")
    snip = items[0].get("snippet") or {}
    return _result("channel_reachable", OK,
                   f"authorised against the channel \"{snip.get('title')}\"",
                   channel_title=snip.get("title"),
                   channel_id=items[0].get("id"))


def _scheduling_check():
    """Is ANYTHING set up to call the uploader on its own?

    Checked separately and stated plainly because it is the cause that every
    other check in this file will happily pass over. Production runs
    service_entrypoint.py, which execs main.py or bot_runner.py; neither
    imports a YouTube scheduler. youtube_scheduler.py exists but is a manual
    CLI ("python youtube_scheduler.py start"), so unless something outside
    this service POSTs to /publish/youtube/..., no video is ever published
    however good the credentials are.
    """
    import pathlib
    here = pathlib.Path(__file__).parent
    callers = []
    for name in ("main.py", "bot_runner.py"):
        p = here / name
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        # A mention inside the router wiring does not schedule anything; what
        # would is an actual scheduler/worker import.
        for marker in ("youtube_scheduler", "run_batch_pipeline",
                       "youtube_auto_pipeline"):
            if marker in text:
                callers.append(f"{name} references {marker}")
    if callers:
        return _result("uploads_are_scheduled", OK,
                       "a production process references the pipeline: "
                       + "; ".join(callers), callers=callers)
    return _result(
        "uploads_are_scheduled", FAIL,
        "NOTHING in this service uploads on its own. service_entrypoint.py "
        "starts main.py or bot_runner.py and neither one imports the YouTube "
        "scheduler or pipeline. youtube_scheduler.py is a manual command "
        "(`python youtube_scheduler.py start`). The only path to YouTube in "
        "production is an external caller POSTing /publish/youtube/..., so "
        "if nothing is calling that, no video is published even when every "
        "credential check above is green.",
        callers=[])


async def youtube_health() -> dict:
    """Every precondition for an upload, each with its own verdict."""
    checks = [_credentials_present()]
    token = scopes = None
    if checks[0]["status"] == OK:
        async with httpx.AsyncClient(timeout=20) as client:
            tok_result, token, scopes = await _token_exchange(client)
            checks.append(tok_result)
            checks.append(_scope_check(scopes))
            checks.append(await _channel_check(client, token))
    else:
        for n in ("refresh_token_works", "scopes_allow_upload",
                  "channel_reachable"):
            checks.append(_result(n, UNKNOWN,
                                  "not attempted - credentials are missing"))
    checks.append(_scheduling_check())

    failed = [c["name"] for c in checks if c["status"] == FAIL]
    unknown = [c["name"] for c in checks if c["status"] == UNKNOWN]
    if failed:
        verdict = f"BLOCKED: {', '.join(failed)}"
    elif unknown:
        verdict = f"INCONCLUSIVE: {', '.join(unknown)} could not be checked"
    else:
        verdict = "every precondition for an upload is satisfied"
    return {
        "verdict": verdict,
        "can_upload_right_now": not failed and not unknown,
        "checks": checks,
        "note": "read-only - this endpoint never uploads, edits or deletes "
                "anything, and never returns a credential value.",
    }
