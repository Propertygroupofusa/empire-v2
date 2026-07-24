"""AI customer-service support inbox - the DB-backed replacement for the
inherited prototype's in-memory conversations/tickets/customer_profiles/
knowledge_base dicts, which lost all data on every restart.

Auth: each SupportAccount (a paying business using this to handle their
own customers) gets a generated api_key at registration, shown once.
Business-facing endpoints require it via the X-Api-Key header. The
inbound email webhook is a separate case - SendGrid's Inbound Parse
webhook doesn't sign its requests the way Stripe signs webhooks, so it's
authorized by the same api_key passed as a URL query token instead
(an unguessable secret in the URL, same idea as an unlisted webhook
endpoint)."""
import os
import re
import secrets
import logging
from email import message_from_string
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Form
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import SupportAccount, KnowledgeBaseEntry, SupportConversation, SupportMessage
from email_integration import extract_text_from_mime
from support_agent import generate_reply

log = logging.getLogger("support")
router = APIRouter()

FROM_ADDR_RE = re.compile(r"<([^>]+)>")


def _extract_email_address(from_field: str) -> str:
    """SendGrid's "from" field is often 'Display Name <addr@example.com>'
    rather than a bare address."""
    match = FROM_ADDR_RE.search(from_field or "")
    return (match.group(1) if match else (from_field or "")).strip().lower()


async def require_support_account(x_api_key: str = Header(None), db: AsyncSession = Depends(get_db)) -> SupportAccount:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-Api-Key header")
    result = await db.execute(select(SupportAccount).where(SupportAccount.api_key == x_api_key))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return account


class RegisterRequest(BaseModel):
    email: str
    business_name: str


@router.post("/register")
async def register_account(payload: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """Creates a new support account. The generated api_key is returned
    ONLY in this response - it is never shown again, and is not stored
    anywhere retrievable in plaintext logs. Rejects a duplicate email
    outright rather than silently handing back an existing account's key
    to whoever asks."""
    email = payload.email.lower()
    existing = await db.execute(select(SupportAccount).where(SupportAccount.email == email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="An account is already registered with this email")

    api_key = secrets.token_urlsafe(32)
    account = SupportAccount(email=email, business_name=payload.business_name, api_key=api_key)
    db.add(account)
    await db.commit()
    await db.refresh(account)
    log.info(f"New support account registered: {account.email} (id={account.id})")

    response = account.to_dict()
    response["api_key"] = api_key
    return response


@router.get("/me")
async def get_my_account(account: SupportAccount = Depends(require_support_account)):
    return account.to_dict()


class KnowledgeBaseRequest(BaseModel):
    topic: str
    content: str


@router.post("/knowledge-base")
async def add_knowledge_base_entry(
    payload: KnowledgeBaseRequest,
    account: SupportAccount = Depends(require_support_account),
    db: AsyncSession = Depends(get_db),
):
    entry = KnowledgeBaseEntry(account_id=account.id, topic=payload.topic, content=payload.content)
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry.to_dict()


@router.get("/knowledge-base")
async def list_knowledge_base(account: SupportAccount = Depends(require_support_account), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(KnowledgeBaseEntry).where(KnowledgeBaseEntry.account_id == account.id))
    return {"entries": [e.to_dict() for e in result.scalars().all()]}


@router.put("/knowledge-base/{entry_id}")
async def update_knowledge_base_entry(
    entry_id: int,
    payload: KnowledgeBaseRequest,
    account: SupportAccount = Depends(require_support_account),
    db: AsyncSession = Depends(get_db),
):
    entry = await db.get(KnowledgeBaseEntry, entry_id)
    if not entry or entry.account_id != account.id:
        raise HTTPException(status_code=404, detail="Knowledge base entry not found")
    entry.topic = payload.topic
    entry.content = payload.content
    await db.commit()
    await db.refresh(entry)
    return entry.to_dict()


@router.delete("/knowledge-base/{entry_id}")
async def delete_knowledge_base_entry(
    entry_id: int,
    account: SupportAccount = Depends(require_support_account),
    db: AsyncSession = Depends(get_db),
):
    entry = await db.get(KnowledgeBaseEntry, entry_id)
    if not entry or entry.account_id != account.id:
        raise HTTPException(status_code=404, detail="Knowledge base entry not found")
    await db.delete(entry)
    await db.commit()
    return {"deleted": True}


def _send_reply_email(to_email: str, from_email: str, subject: str, body: str):
    """No-ops quietly (just a log line) if SENDGRID_API_KEY isn't
    configured - same soft-fail pattern as the GMAIL_EMAIL/GMAIL_PASSWORD
    checks elsewhere in this app, so this is safe to ship before a real
    SendGrid account is wired up."""
    api_key = os.getenv("SENDGRID_API_KEY", "")
    if not api_key or not to_email or not from_email:
        log.info(f"(reply email skipped - SendGrid not configured or missing address) to={to_email}")
        return
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        message = Mail(
            from_email=from_email,
            to_emails=to_email,
            subject=subject or "Re: your support request",
            plain_text_content=body,
        )
        SendGridAPIClient(api_key).send(message)
        log.info(f"Support reply emailed to {to_email}")
    except Exception as e:
        log.warning(f"Support reply email failed: {e}")


@router.post("/inbound")
async def inbound_email(
    token: str,
    db: AsyncSession = Depends(get_db),
    from_: str = Form(..., alias="from"),
    to: Optional[str] = Form(None),
    subject: Optional[str] = Form(None),
    text: Optional[str] = Form(None),
    email: Optional[str] = Form(None),
):
    """SendGrid Inbound Parse webhook target. Prefers SendGrid's own
    already-parsed "text" field; falls back to parsing the raw MIME
    "email" field (present when Inbound Parse is configured to include
    the raw message) via email_integration's fixed multipart walker."""
    result = await db.execute(select(SupportAccount).where(SupportAccount.api_key == token))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=401, detail="Invalid inbound token")

    customer_email = _extract_email_address(from_)
    if not customer_email:
        raise HTTPException(status_code=400, detail="Could not determine sender address")

    body = text
    if not body and email:
        body = extract_text_from_mime(message_from_string(email))
    body = (body or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="Could not extract message body")

    if to and not account.inbound_email:
        account.inbound_email = to

    convo_result = await db.execute(
        select(SupportConversation)
        .where(
            SupportConversation.account_id == account.id,
            SupportConversation.customer_email == customer_email,
            SupportConversation.status != "resolved",
        )
        .order_by(SupportConversation.created_at.desc())
    )
    conversation = convo_result.scalars().first()
    if not conversation:
        conversation = SupportConversation(account_id=account.id, customer_email=customer_email, subject=subject)
        db.add(conversation)
        await db.flush()

    db.add(SupportMessage(conversation_id=conversation.id, sender="customer", body=body))
    await db.commit()

    history_result = await db.execute(
        select(SupportMessage).where(SupportMessage.conversation_id == conversation.id).order_by(SupportMessage.created_at)
    )
    history = [{"sender": m.sender, "body": m.body} for m in history_result.scalars().all()]

    kb_result = await db.execute(select(KnowledgeBaseEntry).where(KnowledgeBaseEntry.account_id == account.id))
    kb_entries = kb_result.scalars().all()

    reply = await generate_reply(account.business_name, kb_entries, history)

    if reply["escalate"]:
        conversation.status = "escalated"
        await db.commit()
        log.info(f"Conversation {conversation.id} escalated to a human for {account.email}")
        return {"status": "escalated"}

    db.add(SupportMessage(conversation_id=conversation.id, sender="ai", body=reply["body"]))
    conversation.status = "open"
    await db.commit()
    _send_reply_email(customer_email, account.inbound_email or account.email, f"Re: {subject or 'your message'}", reply["body"])
    return {"status": "replied"}


@router.get("/conversations")
async def list_conversations(
    status: Optional[str] = None,
    account: SupportAccount = Depends(require_support_account),
    db: AsyncSession = Depends(get_db),
):
    query = select(SupportConversation).where(SupportConversation.account_id == account.id)
    if status:
        query = query.where(SupportConversation.status == status)
    result = await db.execute(query.order_by(SupportConversation.updated_at.desc()))
    return {"conversations": [c.to_dict() for c in result.scalars().all()]}


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: int,
    account: SupportAccount = Depends(require_support_account),
    db: AsyncSession = Depends(get_db),
):
    conversation = await db.get(SupportConversation, conversation_id)
    if not conversation or conversation.account_id != account.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages_result = await db.execute(
        select(SupportMessage).where(SupportMessage.conversation_id == conversation.id).order_by(SupportMessage.created_at)
    )
    data = conversation.to_dict()
    data["messages"] = [m.to_dict() for m in messages_result.scalars().all()]
    return data


class ReplyRequest(BaseModel):
    body: str


@router.post("/conversations/{conversation_id}/reply")
async def reply_to_conversation(
    conversation_id: int,
    payload: ReplyRequest,
    account: SupportAccount = Depends(require_support_account),
    db: AsyncSession = Depends(get_db),
):
    """A human at the business manually replying - used after an
    escalation, or any time they want to override the AI."""
    conversation = await db.get(SupportConversation, conversation_id)
    if not conversation or conversation.account_id != account.id:
        raise HTTPException(status_code=404, detail="Conversation not found")

    db.add(SupportMessage(conversation_id=conversation.id, sender="business", body=payload.body))
    conversation.status = "open"
    await db.commit()
    _send_reply_email(
        conversation.customer_email, account.inbound_email or account.email,
        f"Re: {conversation.subject or 'your message'}", payload.body,
    )
    return {"status": "sent"}


@router.post("/conversations/{conversation_id}/escalate")
async def escalate_conversation(
    conversation_id: int,
    account: SupportAccount = Depends(require_support_account),
    db: AsyncSession = Depends(get_db),
):
    conversation = await db.get(SupportConversation, conversation_id)
    if not conversation or conversation.account_id != account.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conversation.status = "escalated"
    await db.commit()
    return {"status": "escalated"}


@router.post("/conversations/{conversation_id}/resolve")
async def resolve_conversation(
    conversation_id: int,
    account: SupportAccount = Depends(require_support_account),
    db: AsyncSession = Depends(get_db),
):
    conversation = await db.get(SupportConversation, conversation_id)
    if not conversation or conversation.account_id != account.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conversation.status = "resolved"
    await db.commit()
    return {"status": "resolved"}
