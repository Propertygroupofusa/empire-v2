"""Correct MIME/Gmail body extraction for the support inbox.

Both helpers here exist to fix real bugs pointed out in the inherited
prototype code:

- extract_text_from_mime() replaces `get_payload(decode=True).decode()
  if msg.is_multipart() else msg.get_payload()`. `get_payload(decode=True)`
  only ever returns bytes for a *leaf* (non-multipart) part - on a
  multipart message it returns None, so `.decode()` on it throws
  AttributeError. This walks every leaf part via msg.walk() and picks
  the first real text/plain body, which is what nearly all real
  incoming email (Gmail, Outlook, anything with an HTML alternative)
  actually needs.

- extract_text_from_gmail_message() replaces
  `payload.get("parts", [{}])[0].get("body", {}).get("data", "")`, which
  assumes the body is always the first element of "parts". That's wrong
  two ways: a simple non-multipart message has no "parts" key at all
  (the body is directly in payload.body.data), and a multipart message's
  plain-text part isn't reliably at index 0. This checks payload.body
  directly first, then recursively searches parts for a real text/plain
  leaf.
"""
import base64
from email.message import Message


def extract_text_from_mime(msg: Message) -> str:
    if not msg.is_multipart():
        payload = msg.get_payload(decode=True)
        return payload.decode(errors="replace") if payload else ""

    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            if payload:
                return payload.decode(errors="replace")

    # No text/plain leaf found (e.g. HTML-only email) - fall back to
    # whatever the first non-empty leaf part actually contains.
    for part in msg.walk():
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True)
        if payload:
            return payload.decode(errors="replace")
    return ""


def _decode_gmail_body_data(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode(errors="replace")


def extract_text_from_gmail_message(payload: dict) -> str:
    body_data = payload.get("body", {}).get("data")
    if body_data:
        return _decode_gmail_body_data(body_data)

    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data")
            if data:
                return _decode_gmail_body_data(data)

    # No top-level text/plain part - recurse (multipart/alternative or
    # multipart/mixed can nest another multipart inside "parts").
    for part in payload.get("parts", []):
        if part.get("parts"):
            nested = extract_text_from_gmail_message(part)
            if nested:
                return nested
    return ""
