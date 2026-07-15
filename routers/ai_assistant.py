"""AI Assistant for helping users navigate the video production platform"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import os
import logging

log = logging.getLogger("pgusa")

router = APIRouter()

# Try to import Claude API
try:
    from anthropic import Anthropic
    CLAUDE_AVAILABLE = True
except ImportError:
    CLAUDE_AVAILABLE = False
    log.warning("Anthropic SDK not available - AI assistant disabled")

# Platform context for AI assistant
PLATFORM_CONTEXT = """
You are a helpful AI assistant for Empire Video Production platform.
Help users navigate the quote process, understand subscription tiers, and answer questions about video generation.

PLATFORM FEATURES:
- Video Types: explainer, product demo, training, testimonial, animated presentation
- Avatars Available: anna, benjamin, carlos, diana, emma, frank, grace, henry
- Languages: English (US, UK, Australian), Spanish (Spain, Mexico, Latin America), French, German, Italian, Portuguese, Dutch, Polish, Russian, Chinese (Simplified, Traditional), Japanese, Korean
- Subscription Tiers:
  * Free: 0 videos/month, $0
  * Starter: 3 videos/month, $49
  * Pro: 10 videos/month, $149
  * Enterprise: Unlimited videos/month, Custom pricing
- Delivery Times: 2-7 days depending on avatar and complexity
- Video Generation: Uses HeyGen AI with avatar synthesis and voice generation

PRICING:
- Subscription videos: Included in tier (no extra cost)
- One-off videos (no subscription): $99-299 per video based on delivery time

CAPABILITIES:
- Customer can select avatar, language, script, target audience
- Automatic price calculation based on delivery timeline
- Stripe payment integration
- Email notifications when video is ready
- Video download after generation completes

Be friendly, concise, and helpful. Answer questions about the platform features, pricing, and how to use the service.
If user asks about something outside the platform, politely redirect them back to the video service.
"""


class QuestionRequest(BaseModel):
    question: str
    chat_history: list = []  # Previous conversation turns for context


class AssistantResponse(BaseModel):
    answer: str
    helpful: bool


@router.post("/ask")
async def ask_assistant(request: QuestionRequest) -> AssistantResponse:
    """Ask the AI assistant a question about the video platform"""

    if not CLAUDE_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="AI Assistant not available - Anthropic SDK not installed"
        )

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("ANTHROPIC_API_KEY not configured - AI assistant disabled")
        raise HTTPException(
            status_code=503,
            detail="AI Assistant not configured"
        )

    try:
        client = Anthropic(api_key=api_key)

        # Build messages from chat history
        messages = []
        for turn in request.chat_history:
            messages.append({"role": turn.get("role"), "content": turn.get("content")})

        # Add current question
        messages.append({"role": "user", "content": request.question})

        # Call Claude API
        response = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=500,
            system=PLATFORM_CONTEXT,
            messages=messages,
        )

        answer = response.content[0].text

        return AssistantResponse(
            answer=answer,
            helpful=True
        )

    except Exception as e:
        log.error(f"AI Assistant error: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Error processing question: {str(e)}"
        )


@router.get("/ready")
async def assistant_ready() -> dict:
    """Check if AI assistant is ready"""
    return {
        "available": CLAUDE_AVAILABLE,
        "configured": bool(os.getenv("ANTHROPIC_API_KEY")),
        "status": "ready" if (CLAUDE_AVAILABLE and os.getenv("ANTHROPIC_API_KEY")) else "not_available"
    }
