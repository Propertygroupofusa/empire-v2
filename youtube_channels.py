"""Which channel are we posting to, and with whose credentials.

THE CONSTRAINT THAT SHAPES ALL OF THIS

YouTube issues one refresh token PER CHANNEL. The consent screen asks which
channel you are authorising, so a network of ten channels is ten tokens. No
amount of code removes that - it is how Google's OAuth works - which is why
the single YOUTUBE_REFRESH_TOKEN environment variable could only ever address
one channel, and why adding a second meant a redeploy.

AND THE ONE THAT CANNOT BE AUTOMATED AT ALL

There is no API for creating a YouTube channel. The Data API v3 channels
resource supports `list` and `update` and nothing else - no `insert`. A new
channel is made by hand in the YouTube interface (a Brand Account), and then
authorised once. Everything AFTER that point is automatable and is what this
module exists for; the two minutes of clicking is not, and any design that
assumes otherwise is built on something that does not exist.

WHAT MAKES "SET IT ONCE" TRUE

Not this file. Google expires a refresh token after 7 DAYS while the OAuth
app's consent screen sits in "Testing" publishing status, and that single
setting is the usual reason a working bot goes quiet a week later. Flipped to
"In production" there is no fixed expiry - a token then dies only if it is
revoked, goes six months unused, or is pushed out by the 100-live-tokens-per
-client-id limit. resolve() reports which of those happened when it can,
because "it stopped working" is not an answer anybody can act on.

PUBLISHING IS OFF BY DEFAULT, PER CHANNEL. A configured channel is not a
channel that may post. publishing_enabled is the consent, it defaults False,
and nothing in this module turns it on.
"""
import logging
import os
from datetime import datetime

from sqlalchemy import select

from database import get_session_factory
from models import YouTubeChannel

log = logging.getLogger("youtube_channels")

#: Returned instead of a token when the channel cannot be used. The caller
#: must branch on `ok`, never on truthiness of the token.
class Resolution:
    def __init__(self, ok, label=None, refresh_token=None, client_id=None,
                 client_secret=None, reason=None, channel_id=None):
        self.ok = ok
        self.label = label
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.reason = reason
        self.channel_id = channel_id

    def __repr__(self):  # never prints the token
        return (f"<Resolution ok={self.ok} label={self.label!r} "
                f"reason={self.reason!r}>")


def _env_default_client():
    return (os.getenv("YOUTUBE_CLIENT_ID", ""),
            os.getenv("YOUTUBE_CLIENT_SECRET", ""))


def env_channel():
    """The single channel configured the old way, as a Resolution.

    Kept so that nothing breaks the moment this module lands: a service with
    only the environment variables set keeps publishing exactly as it did.
    Migration is an INSERT when the owner is ready, not a cutover.
    """
    token = os.getenv("YOUTUBE_REFRESH_TOKEN", "")
    cid, secret = _env_default_client()
    if not token or not cid or not secret:
        missing = [n for n, v in (("YOUTUBE_CLIENT_ID", cid),
                                  ("YOUTUBE_CLIENT_SECRET", secret),
                                  ("YOUTUBE_REFRESH_TOKEN", token)) if not v]
        return Resolution(False, label="env",
                          reason=f"not configured in the environment: "
                                 f"{', '.join(missing)}")
    return Resolution(True, label="env", refresh_token=token,
                      client_id=cid, client_secret=secret)


async def list_channels():
    """Every configured channel, newest first. NEVER returns a token."""
    async with get_session_factory()() as db:
        rows = (await db.execute(
            select(YouTubeChannel).order_by(YouTubeChannel.id.desc()))).scalars().all()
        return [_safe_dict(r) for r in rows]


def _safe_dict(row):
    """One channel as the API may report it: presence and length, no value.

    The same rule the health check follows. "Behind an admin key" is not the
    same as "safe to print" - an admin endpoint's response ends up in logs,
    screenshots and support threads.
    """
    tok = row.refresh_token or ""
    return {
        "id": row.id,
        "label": row.label,
        "channel_id": row.channel_id,
        "channel_title": row.channel_title,
        "has_refresh_token": bool(tok),
        "refresh_token_length": len(tok),
        "uses_own_oauth_client": bool(row.client_id),
        "content_profile": row.content_profile,
        "publishing_enabled": bool(row.publishing_enabled),
        "daily_upload_limit": row.daily_upload_limit,
        "last_token_ok_at": (row.last_token_ok_at.isoformat()
                             if row.last_token_ok_at else None),
        "last_token_error": row.last_token_error,
        "last_upload_at": (row.last_upload_at.isoformat()
                           if row.last_upload_at else None),
    }


async def resolve(label=None, require_publishing=True):
    """Credentials for one channel. (Resolution) - check .ok, never the token.

    `label` picks a configured channel; omitted, it falls back to the
    environment-variable channel so existing behaviour is unchanged.

    require_publishing is the gate that matters. A row exists as soon as
    someone adds a channel, and existing is not consent to post to it, so by
    default a channel with publishing_enabled False resolves to NOT ok with
    the reason said out loud. A caller that genuinely only wants to READ
    (the health check does) passes False.
    """
    if label is None:
        return env_channel()

    async with get_session_factory()() as db:
        row = (await db.execute(
            select(YouTubeChannel).where(YouTubeChannel.label == label)
        )).scalars().first()

    if row is None:
        return Resolution(False, label=label,
                          reason=f"no channel configured under the label "
                                 f"{label!r}")
    if not row.refresh_token:
        return Resolution(False, label=label, channel_id=row.channel_id,
                          reason=f"{label!r} has no refresh token stored - it "
                                 f"has been added but never authorised")
    if require_publishing and not row.publishing_enabled:
        return Resolution(
            False, label=label, channel_id=row.channel_id,
            reason=f"publishing is OFF for {label!r}. The credentials are "
                   f"present and this is deliberate: a configured channel is "
                   f"not a channel that may post. Turn it on explicitly.")

    cid, secret = row.client_id, row.client_secret
    if not cid or not secret:
        cid, secret = _env_default_client()
        if not cid or not secret:
            return Resolution(
                False, label=label, channel_id=row.channel_id,
                reason=f"{label!r} stores no OAuth client of its own and the "
                       f"service default (YOUTUBE_CLIENT_ID / "
                       f"YOUTUBE_CLIENT_SECRET) is not set either")
    return Resolution(True, label=label, refresh_token=row.refresh_token,
                      client_id=cid, client_secret=secret,
                      channel_id=row.channel_id)


async def record_token_result(label, ok, error=None, channel_id=None,
                              channel_title=None):
    """Write back what the last authentication attempt found.

    Stored so the dashboard can show which channels are healthy without
    re-authenticating every one of them on every page load, and so a token
    that died on a Tuesday is still identifiable on the Friday.
    """
    async with get_session_factory()() as db:
        row = (await db.execute(
            select(YouTubeChannel).where(YouTubeChannel.label == label)
        )).scalars().first()
        if row is None:
            return False
        if ok:
            row.last_token_ok_at = datetime.utcnow()
            row.last_token_error = None
            # The venue's own answer for who we are actually posting as.
            # Only ever written from a successful read, never guessed.
            if channel_id:
                row.channel_id = channel_id
            if channel_title:
                row.channel_title = channel_title
        else:
            row.last_token_error = (error or "")[:500]
        await db.commit()
        return True


async def add_channel(label, refresh_token, client_id=None,
                      client_secret=None, content_profile=None,
                      daily_upload_limit=None):
    """Register a channel. It is created with publishing OFF, always.

    There is deliberately no `publishing_enabled` argument. Adding a channel
    and authorising it to post publicly are two decisions, and collapsing
    them into one call means a typo in an automation starts a channel
    posting. Turning it on is its own explicit step.
    """
    if not label or not str(label).strip():
        raise ValueError("a channel needs a label")
    if not refresh_token:
        raise ValueError("a channel needs a refresh token - authorise it "
                         "first, then add it")
    async with get_session_factory()() as db:
        existing = (await db.execute(
            select(YouTubeChannel).where(YouTubeChannel.label == label)
        )).scalars().first()
        if existing is not None:
            raise ValueError(f"a channel labelled {label!r} already exists")
        row = YouTubeChannel(
            label=str(label).strip(),
            refresh_token=refresh_token,
            client_id=client_id or None,
            client_secret=client_secret or None,
            content_profile=content_profile or None,
            daily_upload_limit=daily_upload_limit,
            publishing_enabled=False,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return _safe_dict(row)


async def set_publishing(label, enabled):
    """Turn automatic publishing on or off for one channel.

    Its own function, called on purpose, because it is the switch that
    decides whether anything reaches the public.
    """
    async with get_session_factory()() as db:
        row = (await db.execute(
            select(YouTubeChannel).where(YouTubeChannel.label == label)
        )).scalars().first()
        if row is None:
            raise ValueError(f"no channel labelled {label!r}")
        row.publishing_enabled = bool(enabled)
        await db.commit()
        await db.refresh(row)
        log.warning(f"[YOUTUBE] publishing for {label!r} set to "
                    f"{bool(enabled)}")
        return _safe_dict(row)
