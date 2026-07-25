"""AI reply generation for the customer-service support inbox.

Uses AsyncAnthropic (unlike routers/study.py's sync client called from
async routes) since this runs on the request path for a real webhook
that needs to answer promptly without blocking the event loop for other
requests.

Replies are grounded in the support account's own knowledge base -
the model is told explicitly to escalate rather than guess when the
knowledge base doesn't cover the question, since a wrong answer to a
paying business's customer is worse than admitting "let me get a human."
"""
import os
import logging

import anthropic

log = logging.getLogger("support_agent")

CLAUDE_KEY = os.getenv("ANTHROPIC_API_KEY", "")
client = anthropic.AsyncAnthropic(api_key=CLAUDE_KEY)

MODEL = "claude-3-5-sonnet-20241022"
ESCALATION_MARKER = "[ESCALATE]"

SYSTEM_PROMPT = """You are a customer support agent for {business_name}. \
Answer the customer's message using ONLY the knowledge base entries provided below. \
Be concise and friendly.

If the knowledge base does not contain enough information to answer confidently, \
or the customer is angry, requesting a refund, threatening legal action, or asking \
for something a human needs to handle, respond with exactly: {escalation_marker}
Do not guess or make up an answer you cannot support from the knowledge base.

Knowledge base:
{knowledge_base}"""


def _format_knowledge_base(entries: list) -> str:
    if not entries:
        return "(empty - no knowledge base entries configured yet)"
    return "\n\n".join(f"Topic: {e.topic}\n{e.content}" for e in entries)


async def generate_reply(business_name: str, knowledge_base_entries: list, message_history: list) -> dict:
    """message_history: list of {"sender": "customer"|"ai", "body": str},
    oldest first. Returns {"body": str, "escalate": bool} - body is None
    when escalate is True (nothing useful to send the customer)."""
    system = SYSTEM_PROMPT.format(
        business_name=business_name,
        escalation_marker=ESCALATION_MARKER,
        knowledge_base=_format_knowledge_base(knowledge_base_entries),
    )
    messages = [
        {"role": "user" if m["sender"] == "customer" else "assistant", "content": m["body"]}
        for m in message_history
    ]

    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=500,
            system=system,
            messages=messages,
        )
        text = response.content[0].text.strip()
    except Exception as e:
        log.error(f"Claude call failed generating support reply: {e}")
        return {"body": None, "escalate": True}

    if ESCALATION_MARKER in text:
        return {"body": None, "escalate": True}
    return {"body": text, "escalate": False}
