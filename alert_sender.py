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
