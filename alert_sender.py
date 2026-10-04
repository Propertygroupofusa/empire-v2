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
        present, misnamed = [], {}
        for k in keys:
            # Same tolerance the sender itself now uses - see _env_tolerant().
            # A page that reports "present: []" for a variable the owner can
            # see on his own dashboard sends him to re-enter a secret that
            # was never wrong, which is how this one burned an hour.
            val, actual = _env_tolerant(k)
            if val:
                present.append(k)
                if actual != k:
                    misnamed[k] = actual
        fallbacks[name] = {
            "variables": list(keys),
            "present": sorted(present),
            "complete": len(present) == len(keys),
        }
        if misnamed:
            # NAMES ONLY, never a value. Says which stored spelling was
            # actually found so the owner can see the stray character that
            # Railway's own list renders invisibly.
            fallbacks[name]["found_under_a_different_name"] = misnamed
            fallbacks[name]["what_to_do"] = (
                "Delivery works - this is read correctly now. To make every "
                "other reader of this variable find it too, rename it to the "
                "exact spelling with no surrounding whitespace. The value "
                "does not need to change.")
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
            f"credentials for {', '.join(_usable)} are present in this "
            f"process AND the email senders are wired to them (alert_worker "
            f"and trade_notify_worker both call send_email). So a webhook is "
            f"NOT required: if nothing is arriving, the blocker is the "
            f"credential or the transport, not a missing webhook - read "
            f"email_route below."
            if _usable else
            "no email credentials are present either, so a webhook really is "
            "the only route and ALERT_WEBHOOK_URL has to be set."),
        # An earlier version of this block ended "Nothing is wired to them
        # yet", which stopped being true the moment both workers were
        # wired. A diagnosis that is stale is worse than no diagnosis: it
        # sent the owner to set a variable that would not have helped.
        "why_not_email": (
            None if (email_armed() and recipient()) else
            (f"Email is not armed: set {EMAIL_MODE_ENV}=send."
             if not email_armed() else
             "Email is armed but no recipient resolves - set TRADE_ALERT_EMAIL.")),
        "email_route": {
            "senders_are_wired": True,
            "armed": email_armed(),
            "mode_variable": EMAIL_MODE_ENV,
            "mode_value": email_mode(),
            "recipient_resolves": bool(recipient()),
            "a_webhook_is_required": not bool(_usable),
            "note": ("When this is armed and a credential is present, the "
                     "per-alert last_error carries the real transport "
                     "diagnosis - including whether SMTP connected and was "
                     "refused (a credential problem, fixed by a Gmail App "
                     "Password) or never answered (a blocked port, fixed by "
                     "SENDGRID_API_KEY). Those two look identical in a "
                     "counts row and need opposite actions."),
        },
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


# A failure that means "there is no route out of this host" is not the
# alert's fault, and must not spend the alert's retry budget. Measured
# 2026-10-01 01:0xZ: 79 rows sat at 4 of 6 attempts against a host that
# blocks outbound SMTP. Two more passes and every one of them would have
# been marked failed - permanently, before any working route existed -
# so the backlog would have been lost at the exact moment it became
# deliverable. MAX_ATTEMPTS exists to stop a poisoned MESSAGE retrying
# forever, not to time out the infrastructure.
NO_ROUTE_MARKER = "no transport delivered"

# NO ADDRESS IS AS UNROUTABLE AS NO TRANSPORT.
#
# send_email returns "no recipient resolved (...)" when none of
# TRADE_ALERT_EMAIL / DAILY_BRIEF_EMAIL / GMAIL_EMAIL is set, and that
# string carries no NO_ROUTE_MARKER - so is_infrastructure_failure() read
# False and the worker spent one of six attempts on a pass that never had
# anywhere to go. Six quiet cycles and the alert is marked failed
# permanently, which is precisely the loss the comment above this exists to
# prevent, arriving through the other door. The message was never poisoned;
# the mailbox was simply unset, and that is infrastructure.
#
# Found by a test asserting the round trip rather than the spelling. Not
# live on this account - its recipient resolves - so this is a trap removed,
# not an outage fixed.
NO_RECIPIENT_MARKER = "no recipient resolved"


def is_infrastructure_failure(err) -> bool:
    """True when nothing could carry the message, whatever it said.

    Kept as a marker check rather than exception sniffing because the
    sender already collapses every transport's exception to a type name;
    there is no exception left to inspect by the time a caller sees this.

    Both markers mean the same thing to a caller deciding whether to spend
    a retry: no route existed, so this pass proved nothing about the
    message. A rejection the transport actually issued - a bad password, a
    refused address - is NOT one of these and must still burn its attempt.
    """
    if not err:
        return False
    text = str(err)
    return NO_ROUTE_MARKER in text or NO_RECIPIENT_MARKER in text


# Failures that prove the port was OPEN. Each of these can only be raised
# after the socket connected and the server answered, so seeing one of them
# is positive evidence AGAINST "the host blocks SMTP".
_REACHED_THE_SERVER = (
    "SMTPAuthenticationError",   # login() was sent and refused
    "SMTPSenderRefused",
    "SMTPRecipientsRefused",
    "SMTPDataError",
    "SMTPNotSupportedError",
)

# Failures consistent with the port being closed or filtered.
_NEVER_GOT_THROUGH = (
    "SMTPServerDisconnected",
    "SMTPConnectError",
    "ConnectionRefusedError",
    "TimeoutError",
    "socket.timeout",
    "OSError",
    "gaierror",
)


def _smtp_outcomes(tried) -> dict:
    """{port: ExceptionName} for the smtp legs recorded in `tried`.

    `tried` entries look like "smtp:465: SMTPAuthenticationError" or
    "smtp: no GMAIL_EMAIL/GMAIL_PASSWORD". Only the first shape carries a
    port and an exception name; the credential-missing line is not an
    outcome and is deliberately excluded.
    """
    out = {}
    for entry in tried or ():
        text = str(entry)
        if not text.startswith("smtp:"):
            continue
        parts = text.split(":")
        if len(parts) < 3:
            continue              # "smtp: no GMAIL_EMAIL/..." - no port
        port = parts[1].strip()
        if not port.isdigit():
            continue
        out[port] = parts[2].strip()
    return out


def is_auth_refusal(exc) -> bool:
    """True when the server answered and REFUSED THE CREDENTIAL.

    isinstance, not a name match, for the same reason as
    reached_the_server: every real Gmail refusal arrives as some subclass,
    and this one verdict is the difference between "change GMAIL_PASSWORD
    to an App Password" and "buy a different mail service".
    """
    import smtplib
    return isinstance(exc, smtplib.SMTPAuthenticationError)


def reached_the_server(exc) -> bool:
    """True when this exception proves the socket connected and the server
    answered.

    Classified by isinstance on the live exception object, because the
    alternative - matching type(e).__name__ against a list - silently
    misreads any SUBCLASS of these, and smtplib, Gmail wrappers and test
    doubles all produce subclasses. A name-match that misses means the leg
    gets filed as "never connected", which is the wrong verdict with the
    wrong remedy attached.
    """
    import smtplib
    return isinstance(exc, (
        smtplib.SMTPAuthenticationError,   # login() was sent and refused
        smtplib.SMTPSenderRefused,
        smtplib.SMTPRecipientsRefused,
        smtplib.SMTPDataError,
        smtplib.SMTPNotSupportedError,
    ))


def remedy_for(tried, legs=None) -> str:
    """The one sentence that tells the owner what to actually change.

    Derived from what the transports DID, never asserted. The three cases
    lead to three different actions, and naming the wrong one costs the
    owner a wasted afternoon:

      authenticated-and-refused -> the credential is wrong. For Gmail with
          2FA on, a normal account password is ALWAYS refused here; SMTP
          needs a 16-character App Password. No new service required.
      never-connected           -> the port really is filtered. An HTTPS
          transport is the fix.
      no credential at all      -> name the missing variable.

    Mixed results are reported as mixed rather than collapsed, because a
    port that authenticates and a port that never answers are different
    facts and the owner may need both.
    """
    outcomes = _smtp_outcomes(tried)
    if legs:
        # Authoritative: the caller watched the exceptions happen.
        reached = {str(p): n for p, n, got, _a in legs if got}
        refused = {str(p): n for p, n, got, _a in legs if not got
                   and n in _NEVER_GOT_THROUGH}
        unknown = {str(p): n for p, n, got, _a in legs if not got
                   and n not in _NEVER_GOT_THROUGH}
        # Authoritative auth verdict, by isinstance, taken at the raise site.
        auth = [str(p) for p, _n, _g, a in legs if a]
        if not outcomes:
            outcomes = {str(p): n for p, n, _g, _a in legs}
    else:
        # Re-diagnosing a STORED row: only the type names survived, so fall
        # back to matching them - and anything unrecognised stays UNKNOWN.
        reached = {p: e for p, e in outcomes.items()
                   if e in _REACHED_THE_SERVER}
        refused = {p: e for p, e in outcomes.items()
                   if e in _NEVER_GOT_THROUGH}
        unknown = {p: e for p, e in outcomes.items()
                   if p not in reached and p not in refused}
        # Stored row: the type NAME is all that survived the trip to the
        # database, so this leg can only match on it.
        auth = [p for p, e in reached.items()
                if e == "SMTPAuthenticationError"]

    if not outcomes:
        if any("no GMAIL_EMAIL" in str(t) for t in (tried or ())):
            return ("No mail credential is present: set GMAIL_EMAIL and "
                    "GMAIL_PASSWORD, or SENDGRID_API_KEY for the HTTPS "
                    "route.")
        return ("No transport was attempted. Check which delivery "
                "variables this process actually has.")

    if reached:
        ports = ", ".join(sorted(reached))
        if auth:
            return (f"SMTP is NOT blocked here - port(s) {ports} connected "
                    f"and Gmail REFUSED the login, which can only happen "
                    f"after the connection succeeded. The fix is the value "
                    f"of GMAIL_PASSWORD: with 2-step verification on, Gmail "
                    f"rejects a normal account password over SMTP and "
                    f"requires a 16-character App Password "
                    f"(myaccount.google.com -> Security -> App passwords). "
                    f"Paste it with no spaces. No new service is needed."
                    + (f" Port(s) {', '.join(sorted(refused))} separately "
                       f"never answered." if refused else ""))
        return (f"Port(s) {ports} reached the mail server and it rejected "
                f"the message ({', '.join(sorted(set(reached.values())))}). "
                f"This is a mail-account problem, not a network one.")

    if refused:
        ports = ", ".join(sorted(refused))
        return (f"SMTP port(s) {ports} never answered "
                f"({', '.join(sorted(set(refused.values())))}), so outbound "
                f"SMTP looks filtered on this host. Set SENDGRID_API_KEY to "
                f"deliver over HTTPS instead."
                + (f" Port(s) {', '.join(sorted(unknown))} failed in a way "
                   f"this cannot classify "
                   f"({', '.join(sorted(set(unknown.values())))})."
                   if unknown else ""))

    # UNKNOWN IS A THIRD VERDICT. An earlier draft of this function fell
    # through to "every port never answered ()" for any exception it did not
    # recognise - asserting a blocked host, with an empty parenthesis where
    # the evidence should have been, which is precisely the failure this
    # whole function exists to stop. A gap is not a zero.
    ports = ", ".join(sorted(unknown)) or "the SMTP leg"
    kinds = ", ".join(sorted(set(unknown.values()))) or "no type recorded"
    return (f"SMTP port(s) {ports} failed with {kinds}, which this cannot "
            f"classify as either a refused login or a closed port - so "
            f"whether SMTP is usable here is UNKNOWN, not blocked. Check "
            f"the app log for that exception before changing any variable.")


# ---- A VARIABLE THE DASHBOARD SHOWS AND THE PROCESS CANNOT SEE -----------
# Railway's variable list renders "SENDGRID_API_KEY" and "SENDGRID_API_KEY "
# identically - the name is left-aligned and a trailing space is invisible.
# os.getenv() is not: it looks up the exact string and misses the padded one.
# That is not a hypothesis here. This project's own process environment
# already carries one: "STRIPE_WEBHOOK_SECRET " with a trailing space, which
# alert-queue's similar_variables_seen has been printing all along.
#
# The account owner confirmed the key is set, declined to re-enter it, and
# asked whether it works. On a container two minutes old the sender still
# reported `sendgrid: no SENDGRID_API_KEY`. So the lookup is made tolerant
# rather than the person being asked to retype a secret to satisfy a string
# comparison.
#
# NAMES ONLY leave this function in any diagnostic. The value is returned to
# the caller that sends the mail and is never logged, echoed or reported -
# the same contract the rest of this module already keeps.
def _env_tolerant(name: str):
    """(value, actual_variable_name) for `name`, allowing a name whose
    stored spelling carries stray surrounding whitespace or differs in case.
    The exact name always wins; a padded one is only a fallback, so nothing
    about a correctly-named variable changes."""
    exact = os.environ.get(name)
    if exact is not None and exact.strip():
        return exact.strip(), name
    target = name.strip().upper()
    for k, v in os.environ.items():
        if k == name:
            continue
        if k.strip().upper() == target and (v or "").strip():
            return v.strip(), k
    return "", None



def send_email(subject: str, body: str) -> tuple:
    """(ok, error). Delivers by whatever route this host actually permits.

    CORRECTION, MEASURED 2026-10-01 from the queue's own rows. An earlier
    version of this docstring concluded "Railway blocks outbound SMTP",
    on the evidence that both ports returned SMTPServerDisconnected.
    That conclusion was WRONG, and the queue itself disproved it:

        smtp:465: SMTPAuthenticationError
        smtp:587: SMTPAuthenticationError

    SMTPAuthenticationError can only be raised AFTER the TCP connection
    opened, TLS negotiated, the banner arrived, EHLO succeeded and
    server.login() was actually sent. Reaching login proves the port is
    open. So outbound SMTP is NOT blocked on this host - Gmail is
    refusing the credential.

    That distinction decides the remedy, which is why the message this
    function returns is now DERIVED from the failures recorded in
    `tried` instead of asserting one cause. A hardcoded "the host blocks
    SMTP" sent the owner looking for a new mail provider when the actual
    fix was one 16-character value.

    The earlier Disconnected readings were real but were a SYMPTOM:
    Gmail drops connections from an IP that keeps failing to log in.
    Repeated failed logins across a 79-row backlog produced them.

    HTTPS is still tried FIRST: it is the route that needs no secret
    stored as a password, and it is known to work here (the app talks to
    Coinbase continuously). SMTP stays as fallback.

    Every transport's failure is reported together, so the message names
    which ONE variable would fix it; reporting only the last failure
    hides that.

    Never raises, and never returns an exception's TEXT - an SMTP error
    can echo the conversation, login line included.
    """
    to = recipient()
    if not to:
        return False, (NO_RECIPIENT_MARKER
                       + " (TRADE_ALERT_EMAIL / DAILY_BRIEF_EMAIL / GMAIL_EMAIL)")

    tried = []
    legs = []

    # --- 1. SendGrid over HTTPS. Raw REST, deliberately NOT the sendgrid
    # package: it is in requirements.txt but importing it is one more way
    # to fail at runtime, and the v3 send endpoint is a single POST.
    key, key_var = _env_tolerant("SENDGRID_API_KEY")
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
                tried.append(
                    f"sendgrid: HTTP {r.status}"
                    + ("" if key_var == "SENDGRID_API_KEY"
                       else f" (read from the variable named {key_var!r} - "
                            f"note the stray whitespace in its name)"))
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
        legs = []   # (port, type name, reached_the_server, is_auth_refusal)
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
                # The verdict is taken HERE, where the exception object is
                # still in hand. Deriving it later from the name loses every
                # subclass.
                legs.append((port, type(e).__name__,
                             reached_the_server(e), is_auth_refusal(e)))
    else:
        legs = []
        tried.append("smtp: no GMAIL_EMAIL/GMAIL_PASSWORD")

    return False, (NO_ROUTE_MARKER + " (" + ", ".join(tried) + "). "
                   + remedy_for(tried, legs))
