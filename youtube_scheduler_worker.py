"""The scheduler: what may post, to which channel, how often, and when not.

WHAT THIS REPLACES

youtube_scheduler.py exists but could not be wired into the service. It calls
schedule.run_forever(), which blocks - there is no way to run it inside the
async app - it keeps its state in a local sqlite file that Railway wipes on
every deploy, it posts on a fixed Mon/Wed/Fri cadence regardless of what any
channel is configured for, and it predates channels having configuration at
all. So nothing called it, and the channel went quiet. This is the loop that
actually runs, following the same shape as every other worker here.

FOUR GATES, AND THEY ARE NOT NEGOTIABLE

  1. MODE. YOUTUBE_SCHEDULER must be exactly "arm". Not "1", not "true", not
     "on" - the same exact-word rule the resting stops and the adopted stop
     use, because a switch that something posts public video through should
     not be throwable by a stray environment value.
  2. THE CHANNEL. publishing_enabled, per channel, default False. Armed mode
     is permission for the LOOP to run, never permission to post to a
     particular channel.
  3. THE DAILY LIMIT. Per channel, from the ledger. A channel with no limit
     set does not get "unlimited" - it gets nothing, because an unset limit
     is an unanswered question.
  4. DUPLICATES. A content_key this channel already has a row for is never
     posted again, whatever the row's status.

Every one of them fails CLOSED. An unreadable ledger, an unreadable channel
list, an unparseable limit - each skips the upload. A video not posted costs
one video and the next cycle comes round in minutes; a wrong or duplicated
post is public, is attributed to the owner, and is what gets a channel
demonetised.

PRIVACY DEFAULTS TO PRIVATE. Not unlisted, not public. A channel has to ask
for anything else in its content_profile, and asking for "public" is a
separate explicit value - the first thing a new automation does should not
be visible to the world while nobody has checked what it made.
"""
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import select, func

log = logging.getLogger("youtube_scheduler_worker")

MODE_ENV = "YOUTUBE_SCHEDULER"
#: The exact word. Anything else is off.
ARMED = "arm"

#: How often the loop wakes. Deliberately not the posting cadence - the loop
#: checks often and the per-channel daily limit decides what actually goes.
CHECK_SECONDS = int(os.getenv("YOUTUBE_SCHEDULER_SECONDS", "900"))

#: What a video is published as when the channel does not say otherwise.
DEFAULT_PRIVACY = "private"
#: The only values a channel may ask for. "public" is here, but it has to be
#: written down by a person - it is never arrived at by default or by typo.
ALLOWED_PRIVACY = ("private", "unlisted", "public")

HEARTBEAT = {"started_at": None, "last_run": None, "last_result": None,
             "last_error": None, "uploads": 0}


def current_mode():
    """'arm' or 'off'. Anything that is not exactly the word is off."""
    raw = (os.getenv(MODE_ENV) or "").strip().lower()
    return ARMED if raw == ARMED else "off"


def is_armed():
    return current_mode() == ARMED


def resolve_privacy(content_profile):
    """What this channel publishes as. (privacy, reason).

    Unknown or malformed values do NOT fall through to something permissive
    - they fall back to private, and say so.
    """
    want = None
    if isinstance(content_profile, dict):
        want = content_profile.get("privacy")
    if want is None:
        return DEFAULT_PRIVACY, "no privacy set on the channel, so private"
    want = str(want).strip().lower()
    if want not in ALLOWED_PRIVACY:
        return DEFAULT_PRIVACY, (f"{want!r} is not one of {ALLOWED_PRIVACY}, "
                                 f"so private")
    return want, f"the channel asks for {want}"


async def _uploads_today(db, label):
    """How many uploads this channel has CLAIMED since midnight UTC.

    Counts claimed rows, not just successful ones. An attempt whose outcome
    is unknown has still consumed a slot - counting only successes would let
    a channel retry past its own limit on a bad day, which is exactly when
    restraint matters.
    """
    from models import YouTubeUpload
    since = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    n = (await db.execute(
        select(func.count()).select_from(YouTubeUpload)
        .where(YouTubeUpload.channel_label == label)
        .where(YouTubeUpload.claimed_at >= since))).scalar()
    return int(n or 0)


async def _already_posted(db, label, content_key):
    from models import YouTubeUpload
    row = (await db.execute(
        select(YouTubeUpload)
        .where(YouTubeUpload.channel_label == label)
        .where(YouTubeUpload.content_key == content_key))).scalars().first()
    return row is not None


async def may_upload(session_factory, label, content_key):
    """Every gate, in order. (allowed: bool, reason: str).

    Pure decision - it posts nothing. Separated so the whole policy can be
    tested without a network, and so the dashboard can ask "why is this
    channel quiet?" and get the same answer the loop acts on.
    """
    if not is_armed():
        return False, (f"the scheduler is off - {MODE_ENV} is "
                       f"{os.getenv(MODE_ENV)!r}, and it must be exactly "
                       f"{ARMED!r}")
    if not content_key:
        return False, "no content_key, so a duplicate could not be detected"

    import youtube_channels
    res = await youtube_channels.resolve(label)
    if not res.ok:
        return False, res.reason

    try:
        async with session_factory() as db:
            from models import YouTubeChannel
            row = (await db.execute(
                select(YouTubeChannel)
                .where(YouTubeChannel.label == label))).scalars().first()
            if row is None:
                return False, f"no channel configured under {label!r}"

            limit = row.daily_upload_limit
            if limit is None:
                return False, (f"{label!r} has no daily_upload_limit set. An "
                               f"unset limit is an unanswered question, not "
                               f"permission for any number of uploads.")
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                return False, f"{label!r} has an unreadable daily_upload_limit"
            if limit <= 0:
                return False, f"{label!r} has a daily_upload_limit of {limit}"

            if await _already_posted(db, label, content_key):
                return False, (f"{label!r} already has an upload row for "
                               f"{content_key!r} - never posted twice")

            used = await _uploads_today(db, label)
            if used >= limit:
                return False, (f"{label!r} has used {used} of {limit} uploads "
                               f"today")
            return True, (f"{label!r} may upload: {used} of {limit} used "
                          f"today, {content_key!r} is new")
    except Exception as e:
        # An unreadable ledger is not permission. It is the one state where
        # posting could duplicate silently, so it is the one state where the
        # answer is most certainly no.
        return False, (f"could not read the upload ledger for {label!r} "
                       f"({type(e).__name__}: {e}) - refusing rather than "
                       f"risking a duplicate")


async def claim(session_factory, label, content_key, title, privacy):
    """Take the slot BEFORE uploading. Returns the row id, or None.

    Written first on purpose - see YouTubeUpload's docstring. A crash between
    YouTube accepting a video and us recording it must not produce a second
    upload on the next cycle.
    """
    from models import YouTubeUpload
    async with session_factory() as db:
        if await _already_posted(db, label, content_key):
            return None
        row = YouTubeUpload(channel_label=label, content_key=content_key,
                            status="claimed", title=(title or "")[:500],
                            privacy=privacy)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


async def settle(session_factory, upload_id, ok, video_id=None, error=None):
    """Record what became of a claimed upload."""
    from models import YouTubeUpload
    async with session_factory() as db:
        row = (await db.execute(
            select(YouTubeUpload)
            .where(YouTubeUpload.id == upload_id))).scalars().first()
        if row is None:
            return False
        row.status = "uploaded" if ok else "failed"
        row.youtube_video_id = video_id
        row.error = (error or "")[:1000] if error else None
        row.settled_at = datetime.utcnow()
        await db.commit()
        return True


async def complete_render(session_factory, callback_id, download_url,
                          uploader=None):
    """A rendered video has arrived. Publish it against its claimed row.

    THIS IS THE LINK THAT WAS MISSING. The Synthesia webhook logged "Video
    ready" and returned - the render completed, the callback fired, and
    nothing ever carried the file to YouTube. From outside that looks
    identical to a pipeline that works, which is the failure mode this whole
    area keeps producing.

    It publishes ONLY against a row that was already claimed. A render
    nobody planned does not get uploaded on the strength of a webhook: the
    callback is an external input, and the claim is our own record of a
    decision that passed the gates. No row, no upload.
    """
    from models import YouTubeUpload
    if not callback_id or not download_url:
        return {"uploaded": False,
                "reason": "the callback carried no id or no download url"}

    async with session_factory() as db:
        row = (await db.execute(
            select(YouTubeUpload)
            .where(YouTubeUpload.id == _as_int(callback_id)))).scalars().first()
        if row is None:
            return {"uploaded": False,
                    "reason": f"no claimed upload matches callback "
                              f"{callback_id!r} - refusing to publish a video "
                              f"nothing here planned"}
        if row.status != "claimed":
            # Already settled. A webhook can fire twice; publishing twice is
            # the duplicate this whole design exists to prevent.
            return {"uploaded": False,
                    "reason": f"upload {row.id} is already {row.status!r}"}
        label, title, privacy = row.channel_label, row.title, row.privacy
        upload_id = row.id

    # The gates are re-checked at the moment of publishing, not trusted from
    # when the slot was claimed. A render can take a long time, and
    # publishing may have been switched off in between.
    import youtube_channels
    res = await youtube_channels.resolve(label)
    if not res.ok:
        await settle(session_factory, upload_id, False,
                     error=f"publishing refused at render time: {res.reason}")
        return {"uploaded": False, "reason": res.reason}

    send = uploader
    if send is None:
        from youtube_upload_bot import synthesia_to_youtube as send
    try:
        result = await send(synthesia_download_url=download_url, title=title,
                            description="", tags=None,
                            privacy=privacy or DEFAULT_PRIVACY, channel=label)
    except Exception as e:
        await settle(session_factory, upload_id, False,
                     error=f"{type(e).__name__}: {str(e)[:300]}")
        return {"uploaded": False, "reason": f"{type(e).__name__}: {e}"}

    vid = (result or {}).get("id")
    await settle(session_factory, upload_id, bool(vid), video_id=vid,
                 error=None if vid else "the upload returned no video id")
    return {"uploaded": bool(vid), "video_id": vid, "channel": label}


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


async def enabled_channels(session_factory):
    """Channels the loop may consider. Publishing-enabled only."""
    from models import YouTubeChannel
    async with session_factory() as db:
        rows = (await db.execute(
            select(YouTubeChannel)
            .where(YouTubeChannel.publishing_enabled.is_(True)))).scalars().all()
        return [{"label": r.label, "content_profile": r.content_profile,
                 "daily_upload_limit": r.daily_upload_limit} for r in rows]


async def run_once(session_factory):
    """One pass. Returns a dict describing what it did and did not do.

    It does NOT generate content. Deciding what a channel should say is a
    separate job from deciding whether it may say anything, and fusing them
    is how a scheduler ends up posting whatever it happened to produce. This
    reports which channels are clear to publish; the content pipeline is
    wired in behind it once a channel has a format worth repeating.
    """
    if not is_armed():
        return {"armed": False, "detail": f"{MODE_ENV} is not {ARMED!r}",
                "channels": []}
    out = []
    for ch in await enabled_channels(session_factory):
        label = ch["label"]
        privacy, why = resolve_privacy(ch["content_profile"])
        async with session_factory() as db:
            used = await _uploads_today(db, label)
        out.append({"label": label, "privacy": privacy, "privacy_reason": why,
                    "uploads_today": used,
                    "daily_upload_limit": ch["daily_upload_limit"]})
    return {"armed": True, "channels": out,
            "detail": f"{len(out)} channel(s) enabled for publishing"}


async def run_periodically(session_factory):
    """Never dies. A scheduler that raises schedules nothing."""
    import asyncio
    HEARTBEAT["started_at"] = datetime.utcnow().isoformat() + "Z"
    log.info(f"[youtube] scheduler up, mode={current_mode()}, "
             f"every {CHECK_SECONDS}s")
    while True:
        try:
            r = await run_once(session_factory)
            HEARTBEAT["last_run"] = datetime.utcnow().isoformat() + "Z"
            HEARTBEAT["last_result"] = r.get("detail")
            HEARTBEAT["last_error"] = None
        except Exception as e:
            HEARTBEAT["last_error"] = f"{type(e).__name__}: {e}"
            log.error(f"[youtube] scheduler pass failed: {type(e).__name__}: {e}")
        await asyncio.sleep(CHECK_SECONDS)
