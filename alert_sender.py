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
import logging
import os

WEBHOOK_ENV = "ALERT_WEBHOOK_URL"
FORMAT_ENV = "ALERT_WEBHOOK_FORMAT"

log = logging.getLogger(__name__)

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
    # IS THERE ALREADY A WAY TO REACH HIM? This only ever looked for a
    # webhook, so "nothing is being delivered" was true and offered no way
    # out that did not involve setting a new secret. This app ALREADY sends
    # email elsewhere - daily_brief.py and prop_bot.py use GMAIL_EMAIL /
    # GMAIL_PASSWORD, routers/support.py uses SENDGRID_API_KEY - so if one
    # of those is present in THIS process, a delivery path exists that
    # needs no new credential from anybody.
    #
    # NAMES ONLY. Nothing here reads or reports a value, same contract as
    # the rest of this function.
    _email_sets = {
        "gmail_smtp": ("GMAIL_EMAIL", "GMAIL_PASSWORD"),
        "sendgrid": ("SENDGRID_API_KEY",),
    }
    fallbacks = {}
    for name, keys in _email_sets.items():
        present = [k for k in keys if (os.environ.get(k) or "").strip()]
        fallbacks[name] = {
            "variables": list(keys),
            "present": sorted(present),
            "complete": len(present) == len(keys),
        }
    _usable = sorted(n for n, v in fallbacks.items() if v["complete"])
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
        "email_fallbacks": fallbacks,
        "a_delivery_path_already_exists": bool(_usable),
        "usable_without_a_new_secret": _usable,
        "what_that_means": (
            f"credentials for {', '.join(_usable)} are already present in this "
            f"process, so alerts could be delivered by email without anyone "
            f"setting a new secret. Nothing is wired to them yet."
            if _usable else
            "no email credentials are present either, so a webhook really is "
            "the only route and ALERT_WEBHOOK_URL has to be set."),
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

# ---------------------------------------------------------------------------
# EMAIL DELIVERY - the route that needs no new secret
# ---------------------------------------------------------------------------
#
# 76 alerts were queued and 0 delivered because ALERT_WEBHOOK_URL has
# never been set. Meanwhile GMAIL_EMAIL and GMAIL_PASSWORD are ALREADY in
# this process - daily_brief.py, prop_bot.py and notary_bot.py all send
# through them. A working route existed the whole time; nothing used it.
#
# ONE DIGEST, NEVER A FLOOD. The backlog is not 76 separate facts. The
# live queue is dominated by one branch retrying one order - QNT's exit
# was refused 200 times in 24 hours - and 76 emails about it would get
# the sender muted, which is strictly worse than silence because the
# muting also swallows the next real one. So repeats are grouped and
# counted, and the whole backlog goes as a single message.
#
# ARMED DELIBERATELY, like every other outbound path here. Default is
# off: build_digest() is pure and can be read before anything is sent, so
# the account owner sees the exact first message before it starts
# arriving rather than after.

EMAIL_MODE_ENV = "ALERT_EMAIL_MODE"
RECIPIENT_ENV = ("TRADE_ALERT_EMAIL", "DAILY_BRIEF_EMAIL")
GMAIL_USER_ENV, GMAIL_PASS_ENV = "GMAIL_EMAIL", "GMAIL_PASSWORD"

# A digest that runs to hundreds of lines is not read on a phone. Past
# this, the rest is summarised as a count - the tail of a flood carries
# no information the head did not already give.
MAX_DIGEST_GROUPS = 25


def email_mode() -> str:
    return (os.getenv(EMAIL_MODE_ENV) or "off").strip().strip('"').strip("'").lower()


def email_armed() -> bool:
    return email_mode() == "send"


def recipient() -> str:
    """Where alerts go. Same resolution order the daily brief already uses,
    so there is one destination convention and not two. Falls back to the
    sending account itself, which is always a real inbox."""
    for var in RECIPIENT_ENV:
        v = (os.getenv(var) or "").strip()
        if v:
            return v
    return (os.getenv(GMAIL_USER_ENV) or "").strip()


def email_configured() -> bool:
    return bool((os.getenv(GMAIL_USER_ENV) or "").strip()
                and (os.getenv(GMAIL_PASS_ENV) or "").strip()
                and recipient())


def _group_key(alert: dict) -> tuple:
    return (str(alert.get("severity") or "ALERT"),
            str(alert.get("kind") or ""),
            str(alert.get("asset") or ""),
            str(alert.get("message") or ""))


_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "INFO": 2}


def build_digest(alerts: list) -> dict:
    """(subject, body, groups) for a backlog, as ONE message. Pure.

    Deliberately has no side effects and touches no network, so the exact
    text can be read before a single send happens.
    """
    alerts = [a for a in (alerts or []) if isinstance(a, dict)]
    if not alerts:
        return {"subject": None, "body": None, "groups": 0, "alerts": 0,
                "reason": "nothing queued"}

    grouped = {}
    for a in alerts:
        k = _group_key(a)
        g = grouped.setdefault(k, {"n": 0, "alert": a})
        g["n"] += 1

    rows = sorted(grouped.items(),
                  key=lambda kv: (_SEVERITY_ORDER.get(kv[0][0], 9), -kv[1]["n"]))
    worst = rows[0][0][0]
    n_alerts, n_groups = len(alerts), len(rows)

    subject = (f"[{worst}] empire-v2: {n_groups} issue"
               f"{'' if n_groups == 1 else 's'}"
               + (f" ({n_alerts} alerts)" if n_alerts != n_groups else ""))

    lines = [f"{n_alerts} queued alert(s), grouped into {n_groups} distinct issue(s).",
             "Most severe first. A repeat count means the same condition fired again,",
             "not that it got worse.", ""]
    for (sev, kind, asset, message), g in rows[:MAX_DIGEST_GROUPS]:
        times = f"  (x{g['n']})" if g["n"] > 1 else ""
        head = SEVERITY_PREFIX.get(sev, "[ALERT]")
        who = f" {asset}" if asset else ""
        lines.append(f"{head}{who} {message}{times}".rstrip())
        detail = (g["alert"].get("detail") or "").strip()
        if detail:
            lines.append(f"    {detail[:400]}")
        lines.append("")
    if n_groups > MAX_DIGEST_GROUPS:
        lines.append(f"... and {n_groups - MAX_DIGEST_GROUPS} further distinct "
                     f"issue(s) not listed here.")
        lines.append("")
    lines.append("This message reports. It places no order and moves no money.")
    return {"subject": subject, "body": "\n".join(lines),
            "groups": n_groups, "alerts": n_alerts, "worst_severity": worst}


def send_digest(alerts: list, *, force: bool = False) -> tuple:
    """(ok, error). Sends the whole backlog as ONE email. Never raises.

    `force` sends a single message even while ALERT_EMAIL_MODE is off -
    used once, deliberately, so the owner sees the first one and can
    judge it before the route is armed for good.
    """
    d = build_digest(alerts)
    if not d.get("subject"):
        return False, d.get("reason", "nothing to send")
    if not email_configured():
        missing = [v for v in (GMAIL_USER_ENV, GMAIL_PASS_ENV)
                   if not (os.getenv(v) or "").strip()]
        return False, ("email not configured: "
                       + (", ".join(missing) or "no recipient resolved")
                       + " (names only - no value is ever read out here)")
    if not (email_armed() or force):
        return False, (f"{EMAIL_MODE_ENV} is '{email_mode()}' - nothing sent. "
                       f"Set it to 'send' to arm this route.")
    ok, err = send_email(d["subject"], d["body"])
    if ok:
        log.info(f"[alerts] digest sent: {d['groups']} issue(s), "
                 f"{d['alerts']} alert(s)")
    return ok, err


def send_email(subject: str, body: str) -> tuple:
    """(ok, error). Delivers by whatever route this host actually permits.

    MEASURED ON THIS HOST 2026-10-01 00:5xZ, from the queue's own rows:

        SMTP failed on every port
        (465: SMTPServerDisconnected, 587: SMTPServerDisconnected)

    Railway blocks outbound SMTP. Both the implicit-SSL port and the
    STARTTLS port are dropped before the banner, so NO amount of port
    juggling fixes it and adding a third port would be wasted work. The
    same bare-465 pattern is used by daily_brief.py, prop_bot.py and
    notary_bot.py, which means every one of those has been failing on
    this host too - quietly, because each one logs a warning and returns.

    HTTPS works here; the app talks to Coinbase continuously. So HTTPS
    transports are tried FIRST and SMTP is kept only as a last resort, so
    this code still works unchanged on a host that permits it.

    Every transport's failure is reported together. "sendgrid: no key,
    smtp: blocked" tells the owner which ONE variable would fix it;
    reporting only the last failure hides that.

    Never raises, and never returns an exception's TEXT - an SMTP error
    can echo the conversation, login line included.
    """
    to = recipient()
    if not to:
        return False, "no recipient resolved (TRADE_ALERT_EMAIL / DAILY_BRIEF_EMAIL / GMAIL_EMAIL)"

    tried = []

    # --- 1. SendGrid over HTTPS. Raw REST, deliberately NOT the sendgrid
    # package: it is in requirements.txt but importing it is one more way
    # to fail at runtime, and the v3 send endpoint is a single POST.
    key = (os.getenv("SENDGRID_API_KEY") or "").strip()
    sender = (os.getenv(GMAIL_USER_ENV) or "").strip() or to
    if key:
        try:
            import urllib.request
            payload = json.dumps({
                "personalizations": [{"to": [{"email": to}]}],
                "from": {"email": sender},
                "subject": subject,
                "content": [{"type": "text/plain", "value": body}],
            }).encode()
            req = urllib.request.Request(
                "https://api.sendgrid.com/v3/mail/send", data=payload,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                if 200 <= r.status < 300:
                    return True, None
                tried.append(f"sendgrid: HTTP {r.status}")
        except Exception as e:
            tried.append(f"sendgrid: {type(e).__name__}")
    else:
        tried.append("sendgrid: no SENDGRID_API_KEY")

    # --- 2. SMTP, last, because it is blocked here. Kept so this file
    # still works on a host that allows it.
    secret = os.getenv(GMAIL_PASS_ENV, "")
    if sender and secret:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = to
        raw = msg.as_string()
        for port, use_ssl in ((465, True), (587, False)):
            try:
                if use_ssl:
                    server = smtplib.SMTP_SSL("smtp.gmail.com", port, timeout=30)
                else:
                    server = smtplib.SMTP("smtp.gmail.com", port, timeout=30)
                try:
                    if not use_ssl:
                        server.ehlo()
                        server.starttls()
                        server.ehlo()
                    server.login(sender, secret)
                    server.sendmail(sender, to, raw)
                finally:
                    try:
                        server.quit()
                    except Exception:
                        pass
                if tried:
                    log.info(f"[mail] smtp:{port} delivered after "
                             f"{len(tried)} earlier failure(s)")
                return True, None
            except Exception as e:
                tried.append(f"smtp:{port}: {type(e).__name__}")
    else:
        tried.append("smtp: no GMAIL_EMAIL/GMAIL_PASSWORD")

    return False, ("no transport delivered (" + ", ".join(tried) + "). "
                   "SMTP is blocked outbound on this host; an HTTPS route "
                   "is the one that can work here.")
