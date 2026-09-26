"""Delivery. Pluggable, fail-closed, and honest about having no channel.

WHY IT DOES NOT MARK THINGS SENT WHEN THERE IS NOWHERE TO SEND

The convenient design logs the alert, marks the row `sent`, and moves on.
That produces a queue that looks perfectly healthy while nobody has ever
been told anything - which is the same class of failure as a stop that is
really just a number on a page, or a census that reports $79.30 because it
dropped everything it could not price.

So: with no channel configured, nothing is attempted and the rows stay
`pending`. The queue endpoint says `channel_configured: false` at the top.
When a channel is finally set, the backlog flushes - which is the whole
point of a durable queue.

CONFIGURATION, by environment variable, none of them required:

    ALERT_WEBHOOK_URL   any https endpoint that accepts a JSON POST -
                        Slack, Discord, a Zapier hook, your own service
    ALERT_WEBHOOK_FORMAT  "generic" (default) | "slack" | "discord"

Deliberately not hardcoded to one vendor. The queue does not care where
the message goes and should not need editing to change its mind.
"""
from __future__ import annotations

import json
import os

WEBHOOK_ENV = "ALERT_WEBHOOK_URL"
FORMAT_ENV = "ALERT_WEBHOOK_FORMAT"

SEVERITY_PREFIX = {"CRITICAL": "[CRITICAL]", "HIGH": "[HIGH]", "INFO": "[INFO]"}


def webhook_url() -> str:
    return (os.getenv(WEBHOOK_ENV) or "").strip()


def channel_configured() -> bool:
    return bool(webhook_url())


def diagnose() -> dict:
    """Why is there no channel? Names only - never a value.

    "channel_configured: false" is true and useless when someone has just
    set the variable and is asking why nothing happened. The usual causes
    are a near-miss on the name, the variable landing on a different
    Railway service than the one running this process, or a deploy that has
    not restarted yet. This reports enough to tell those apart WITHOUT ever
    printing a secret: variable NAMES that look related, whether the exact
    one is present, and whether its value is empty or malformed.
    """
    url = os.getenv(WEBHOOK_ENV)
    near = sorted(k for k in os.environ
                  if k != WEBHOOK_ENV
                  and any(w in k.upper() for w in ("ALERT", "WEBHOOK", "SLACK",
                                                   "DISCORD", "NOTIF")))
    if url is None:
        why = (f"{WEBHOOK_ENV} is not present in this process at all. Either it "
               f"was set on a different service than the one running this app, "
               f"or the deploy has not restarted since. Railway injects "
               f"variables at container start - an existing container does not "
               f"pick up a new one.")
    elif not url.strip():
        why = f"{WEBHOOK_ENV} is present but empty."
    elif not url.strip().lower().startswith(("http://", "https://")):
        why = (f"{WEBHOOK_ENV} is set but does not start with http:// or "
               f"https://, so it is not a URL this can POST to.")
    else:
        why = None
    return {
        "expected_variable": WEBHOOK_ENV,
        "present": url is not None,
        "non_empty": bool((url or "").strip()),
        "looks_like_url": bool((url or "").strip().lower().startswith(
            ("http://", "https://"))),
        "similar_variables_seen": near,
        "why_not": why,
        "format_variable": FORMAT_ENV,
        "format_value": (os.getenv(FORMAT_ENV) or "generic (default)"),
        "note": "No value is ever reported here - only whether one exists.",
    }


def render(alert: dict) -> str:
    """One line a human reads on a phone at 3am, then the detail."""
    pre = SEVERITY_PREFIX.get(alert.get("severity"), "[ALERT]")
    head = f"{pre} {alert.get('message') or ''}".strip()
    detail = alert.get("detail")
    return f"{head}\n{detail}" if detail else head


def payload_for(alert: dict, fmt: str = None) -> dict:
    """Shape the body for the configured receiver.

    Slack and Discord both take a plain object with one text field, under
    different names. Everything else gets the whole alert, so a receiver
    that wants structure is not forced to parse a sentence.
    """
    fmt = (fmt or os.getenv(FORMAT_ENV) or "generic").strip().lower()
    text = render(alert)
    if fmt == "slack":
        return {"text": text}
    if fmt == "discord":
        return {"content": text}
    return {
        "text": text,
        "kind": alert.get("kind"),
        "asset": alert.get("asset"),
        "severity": alert.get("severity"),
        "message": alert.get("message"),
        "detail": alert.get("detail"),
        "source": "empire-newsroom",
        "is_an_alert_not_an_order": True,
    }


async def deliver(session, alert: dict) -> tuple:
    """(ok, error). Never raises - a delivery bug must not kill the worker.

    Returns ok=False with a reason when there is no channel, rather than
    pretending success, so the caller can leave the row pending instead of
    burning an attempt against a webhook that does not exist.
    """
    url = webhook_url()
    if not url:
        return False, "no ALERT_WEBHOOK_URL configured - nothing was sent"
    try:
        async with session.post(url, json=payload_for(alert), timeout=20) as r:
            body = (await r.text())[:300]
            if 200 <= r.status < 300:
                return True, None
            return False, f"HTTP {r.status}: {body}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
