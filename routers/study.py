"""AI Study Material Generation Router"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Header, Request
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func
from datetime import datetime, timedelta
import os
import base64
import json
import logging
import httpx
import stripe
from io import BytesIO
from typing import Optional

import anthropic
from database import get_db
from models import StudyMaterial, StudyUser
from payments_pause import payments_paused, PAUSE_MESSAGE
from study_auth import (require_study_auth, create_study_token, hash_password,
                        verify_password, validate_password, normalize_email)
from pydantic import BaseModel

log = logging.getLogger("study")
router = APIRouter()

CLAUDE_KEY = os.getenv("ANTHROPIC_API_KEY", "")
client = anthropic.Anthropic(api_key=CLAUDE_KEY)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
stripe_publishable_key = os.getenv("STRIPE_PUBLISHABLE_KEY")

# Free tier limits
FREE_LIMIT = 5  # materials per month
PAID_LIMIT = 999

# Burn-rate cap, separate from the monthly billing allowance and applied
# to paid accounts too. See check_rate_limit.
RATE_LIMIT_PER_HOUR = int(os.getenv("STUDY_RATE_LIMIT_PER_HOUR", "20"))

# OFF by default. /upload-and-generate is the only endpoint here that
# spends money, and until it has real authentication in front of it,
# leaving it reachable is a standing cost drain rather than a product.
#
# verify_user() performs NO authentication. It accepts any string
# containing "@" as an identity, auto-creates a StudyUser at tier="free",
# and returns that user's tier. Two consequences:
#
#   - the paywall does not hold. The free allowance is 5/month keyed on
#     email, so a different email is another 5, forever, with no signup
#     and no confirmation step.
#   - a paying customer can be impersonated. Bearer <their-email> returns
#     tier="paid" and unlimited generations on their subscription.
#
# Meanwhile each call to /upload-and-generate makes TWO claude-3-5-sonnet
# requests - a vision call to read the image, then a generation call -
# and there is no rate limiting anywhere in this application. So the
# endpoint bills the Anthropic account per request, without a cap, for
# anyone who finds the URL, and cannot charge for any of it.
#
# That is not a product that fails to earn; it is one that loses money
# for anyone who calls it. Hence a flag rather than a fix: closing the
# drain is minutes, building real auth is not, and the two should not be
# tangled together.
#
# UPDATE: that prerequisite is now met. study_auth.py provides real
# signup/login with bcrypt + JWT, verify_user below authenticates a signed
# token instead of trusting a bearer email, and check_rate_limit bounds
# the burn rate for every tier. The flag still defaults to OFF so that
# turning generation back on stays a deliberate act - set
# STUDY_GENERATION_ENABLED=true, and STUDY_JWT_SECRET along with it or
# every request will 500 by design rather than fall open.
STUDY_GENERATION_ENABLED = (
    os.getenv("STUDY_GENERATION_ENABLED", "false").strip().lower() == "true"
)
STUDY_DISABLED_MESSAGE = (
    "Study material generation is currently disabled. Set "
    "STUDY_GENERATION_ENABLED=true (and STUDY_JWT_SECRET) to enable it."
)
PAID_TIER_PRICE_CENTS = 999  # $9.99/month, per STUDY_MVP_SUMMARY.md


async def verify_user(authorization: Optional[str] = Header(None), db: AsyncSession = Depends(get_db)):
    """Resolve the authenticated student from a signed JWT.

    This used to accept any string containing "@" as an identity and
    auto-create the account. It now verifies a token issued by /login and
    looks the account up; it never creates one. Account creation belongs
    to /signup, where a password is set.

    The lookup is by ID from the token's `sub`, not by the email claim. A
    token is a bearer credential for one account, and the ID is the thing
    that cannot be re-pointed by changing an email address later.
    """
    claims = await require_study_auth(authorization)

    result = await db.execute(select(StudyUser).where(StudyUser.id == claims["id"]))
    user = result.scalar_one_or_none()
    if not user:
        # Valid signature, deleted account. 401 rather than 404: from the
        # caller's side the credential is simply no longer good.
        raise HTTPException(status_code=401, detail="Account no longer exists")

    return {"email": user.email, "tier": user.tier, "user_obj": user}


async def check_rate_limit(user: dict, db: AsyncSession):
    """Short-window throttle, applied to EVERY tier including paid.

    The monthly allowance is a billing control, not an abuse control. A
    paid account is capped at PAID_LIMIT per month, and each generation
    costs two claude-3-5-sonnet calls, so a single subscriber running flat
    out can spend far more than $9.99 in API cost long before the monthly
    cap stops them. This bounds the burn rate.

    Counted from StudyMaterial rows rather than an in-memory counter, so
    it survives restarts and stays correct if the service is ever run as
    more than one container - an in-process dict would reset on every
    deploy and be per-replica, i.e. no limit at all under exactly the
    conditions where one matters.
    """
    window_start = datetime.utcnow() - timedelta(hours=1)
    result = await db.execute(
        select(func.count(StudyMaterial.id)).where(
            (StudyMaterial.user_id == user["email"]) &
            (StudyMaterial.created_at >= window_start)
        )
    )
    if (result.scalar() or 0) >= RATE_LIMIT_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit: {RATE_LIMIT_PER_HOUR} materials per hour. "
                   f"Try again shortly.")
    return True


async def check_limit(user: dict, db: AsyncSession):
    """Check if user exceeded monthly limit"""
    if user["tier"] == "paid":
        return True  # Unlimited

    # Check free limit (5 per month)
    month_ago = datetime.utcnow() - timedelta(days=30)
    result = await db.execute(
        select(func.count(StudyMaterial.id)).where(
            (StudyMaterial.user_id == user["email"]) &
            (StudyMaterial.created_at >= month_ago)
        )
    )
    count = result.scalar() or 0

    if count >= FREE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"Free tier limit ({FREE_LIMIT}/month) exceeded. Upgrade to unlimited at /study/checkout"
        )
    return True


async def extract_text_from_image(image_data: bytes) -> str:
    """Use Claude's vision to extract text from textbook image"""
    try:
        base64_image = base64.standard_b64encode(image_data).decode("utf-8")

        message = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=2000,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": base64_image,
                            },
                        },
                        {
                            "type": "text",
                            "text": "Extract ALL text from this textbook/document page. Preserve structure, headings, and important formatting."
                        }
                    ],
                }
            ],
        )
        extracted = message.content[0].text
        log.info(f"Extracted {len(extracted)} characters from image")
        return extracted
    except Exception as e:
        log.error(f"Vision extraction error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process image: {str(e)}")


async def generate_study_guide(text: str) -> dict:
    """Generate study guide from extracted text"""
    prompt = f"""You are an expert educator. Create a comprehensive study guide from this textbook excerpt.

Format your response as VALID JSON (no markdown, no backticks, just raw JSON) with this exact structure:
{{
    "title": "Clear title based on content",
    "key_concepts": ["concept1", "concept2", "concept3"],
    "summary": "2-3 paragraph comprehensive summary",
    "key_points": [
        {{"heading": "Topic 1", "explanation": "detailed explanation"}},
        {{"heading": "Topic 2", "explanation": "detailed explanation"}}
    ],
    "review_questions": [
        "Question 1?",
        "Question 2?",
        "Question 3?"
    ]
}}

TEXTBOOK CONTENT:
{text}

Remember: Return ONLY valid JSON, no markdown formatting."""

    try:
        message = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=2000,
            messages=[
                {"role": "user", "content": prompt}
            ],
        )

        response_text = message.content[0].text

        # Extract JSON from response (handle markdown code blocks)
        json_str = response_text
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0].strip()
        elif "```" in json_str:
            json_str = json_str.split("```")[1].split("```")[0].strip()

        parsed = json.loads(json_str)
        log.info(f"Generated study guide: {parsed.get('title', 'Untitled')}")
        return parsed
    except json.JSONDecodeError as e:
        log.error(f"JSON decode error: {e}, response: {response_text[:200]}")
        return {
            "title": "Study Guide",
            "summary": response_text,
            "key_concepts": ["See summary for details"],
            "key_points": [{"heading": "Overview", "explanation": response_text}],
            "review_questions": ["What are the main concepts covered?"]
        }
    except Exception as e:
        log.error(f"Study guide generation error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate study guide: {str(e)}")


async def generate_quiz(text: str) -> dict:
    """Generate 10-question quiz from text"""
    prompt = f"""You are a teacher creating a quiz. Generate exactly 10 multiple-choice questions from this content.

Format your response as VALID JSON (no markdown, no backticks, just raw JSON) with this exact structure:
{{
    "title": "Quiz: [Topic]",
    "questions": [
        {{
            "question": "Question text?",
            "options": ["A) option 1", "B) option 2", "C) option 3", "D) option 4"],
            "correct_answer": "A",
            "explanation": "Why this is correct"
        }}
    ]
}}

TEXTBOOK CONTENT:
{text}

Requirements:
- Create exactly 10 questions
- Each question has 4 options (A, B, C, D)
- Mix difficulty levels
- Include one-word answer in correct_answer field (A, B, C, or D)
- Return ONLY valid JSON, no markdown formatting"""

    try:
        message = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=3000,
            messages=[
                {"role": "user", "content": prompt}
            ],
        )

        response_text = message.content[0].text

        # Extract JSON
        json_str = response_text
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0].strip()
        elif "```" in json_str:
            json_str = json_str.split("```")[1].split("```")[0].strip()

        parsed = json.loads(json_str)
        log.info(f"Generated quiz with {len(parsed.get('questions', []))} questions")
        return parsed
    except json.JSONDecodeError as e:
        log.error(f"JSON decode error in quiz: {e}")
        return {"title": "Quiz", "questions": []}
    except Exception as e:
        log.error(f"Quiz generation error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate quiz: {str(e)}")


async def generate_flashcards(text: str) -> dict:
    """Generate flashcards from text"""
    prompt = f"""You are creating flashcards for studying. Generate 15-20 flashcards from this content.

Format your response as VALID JSON (no markdown, no backticks, just raw JSON) with this exact structure:
{{
    "title": "Flashcards: [Topic]",
    "cards": [
        {{"front": "Term/Question", "back": "Definition/Answer"}},
        {{"front": "Another term", "back": "Its definition"}}
    ]
}}

TEXTBOOK CONTENT:
{text}

Requirements:
- Create 15-20 flashcards minimum
- Front: key terms or questions (short)
- Back: definitions or answers (concise but complete)
- Return ONLY valid JSON, no markdown formatting"""

    try:
        message = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=2500,
            messages=[
                {"role": "user", "content": prompt}
            ],
        )

        response_text = message.content[0].text

        # Extract JSON
        json_str = response_text
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0].strip()
        elif "```" in json_str:
            json_str = json_str.split("```")[1].split("```")[0].strip()

        parsed = json.loads(json_str)
        log.info(f"Generated {len(parsed.get('cards', []))} flashcards")
        return parsed
    except json.JSONDecodeError as e:
        log.error(f"JSON decode error in flashcards: {e}")
        return {"title": "Flashcards", "cards": []}
    except Exception as e:
        log.error(f"Flashcard generation error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate flashcards: {str(e)}")


@router.post("/upload-and-generate")
async def upload_and_generate(
    file: UploadFile = File(...),
    material_type: str = Form(...),
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db)
):
    """
    Upload textbook image and generate study material in one call.

    Args:
        file: Image file (JPG, PNG, max 5MB)
        material_type: One of 'guide', 'quiz', or 'flashcards'
        authorization: Bearer token with email (e.g., "Bearer student@example.com")

    Returns:
        Generated study material with title and content
    """

    # FIRST, before anything else. Ahead of verify_user in particular,
    # which auto-creates a StudyUser row for any email it is handed - so
    # gating after it would still let an unauthenticated caller fill the
    # table, just without spending Claude tokens.
    if not STUDY_GENERATION_ENABLED:
        raise HTTPException(status_code=503, detail=STUDY_DISABLED_MESSAGE)

    # Validate material type
    if material_type not in ["guide", "quiz", "flashcards"]:
        raise HTTPException(
            status_code=400,
            detail="material_type must be one of: guide, quiz, flashcards"
        )

    # Verify user
    user = await verify_user(authorization, db)
    await check_rate_limit(user, db)
    await check_limit(user, db)

    # Validate file
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    # Read image data
    image_data = await file.read()
    if len(image_data) > 5 * 1024 * 1024:  # 5MB limit
        raise HTTPException(status_code=413, detail="Image too large (max 5MB)")

    if len(image_data) == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    # Extract text from image
    log.info(f"Extracting text from image for {user['email']}: {file.filename}")
    extracted_text = await extract_text_from_image(image_data)

    if not extracted_text or len(extracted_text) < 50:
        raise HTTPException(
            status_code=400,
            detail="Could not extract enough text from image. Please try a clearer image."
        )

    # Generate appropriate material
    if material_type == "guide":
        generated = await generate_study_guide(extracted_text)
    elif material_type == "quiz":
        generated = await generate_quiz(extracted_text)
    elif material_type == "flashcards":
        generated = await generate_flashcards(extracted_text)

    # Save to database
    material = StudyMaterial(
        user_id=user["email"],
        material_type=material_type,
        source_text=extracted_text[:1000],  # Store first 1000 chars
        generated_content=generated,
        title=generated.get("title", f"{material_type.capitalize()} Material"),
        topic=material_type.capitalize(),
    )
    db.add(material)
    await db.commit()
    await db.refresh(material)

    log.info(f"Generated {material_type} for {user['email']} - Material ID: {material.id}")

    return {
        "id": material.id,
        "material_type": material_type,
        "title": generated.get("title"),
        "content": generated,
        "created_at": material.created_at.isoformat()
    }


@router.get("/my-materials")
async def get_my_materials(
    skip: int = 0,
    limit: int = 50,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db)
):
    """
    List all study materials for the logged-in user.

    Args:
        skip: Pagination offset
        limit: Pagination limit
        authorization: Bearer token with email

    Returns:
        List of study materials metadata
    """
    user = await verify_user(authorization, db)

    result = await db.execute(
        select(StudyMaterial)
        .where(StudyMaterial.user_id == user["email"])
        .order_by(StudyMaterial.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    materials = result.scalars().all()

    return {
        "count": len(materials),
        "materials": [m.to_dict() for m in materials]
    }


@router.get("/materials/{material_id}")
async def get_material(
    material_id: int,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db)
):
    """
    Retrieve a specific study material.

    Args:
        material_id: ID of the material
        authorization: Bearer token with email

    Returns:
        Full study material with generated content
    """
    user = await verify_user(authorization, db)

    result = await db.execute(
        select(StudyMaterial).where(StudyMaterial.id == material_id)
    )
    material = result.scalar_one_or_none()

    if not material:
        raise HTTPException(status_code=404, detail="Material not found")

    if material.user_id != user["email"]:
        raise HTTPException(status_code=403, detail="You don't have access to this material")

    return material.to_dict()


@router.get("/stripe-key")
async def get_study_stripe_key():
    """Get Stripe publishable key for the study app frontend"""
    return {"publishable_key": stripe_publishable_key}


class SignupRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/signup")
async def signup(payload: SignupRequest, db: AsyncSession = Depends(get_db)):
    """Create an account. The only way a StudyUser now comes into
    existence - previously any request conjured one."""
    email = normalize_email(payload.email)
    if "@" not in email:
        raise HTTPException(status_code=400, detail="A valid email is required")
    validate_password(payload.password)

    existing = (await db.execute(
        select(StudyUser).where(StudyUser.email == email))).scalar_one_or_none()

    if existing and existing.password_hash:
        raise HTTPException(status_code=409, detail="An account with that email exists")

    if existing:
        # A row left over from the no-auth era. Nobody ever proved
        # ownership of it, so the first person to set a password claims
        # it - along with whatever tier it carries. That tier can only
        # have become "paid" through a real Stripe payment keyed to this
        # address, so honouring it is right.
        existing.password_hash = hash_password(payload.password)
        await db.commit()
        await db.refresh(existing)
        user = existing
        log.info(f"Claimed pre-auth study account: {email}")
    else:
        user = StudyUser(email=email, tier="free",
                         password_hash=hash_password(payload.password))
        db.add(user)
        await db.commit()
        await db.refresh(user)
        log.info(f"New study user registered: {email}")

    return {"token": create_study_token(user.id, user.email),
            "email": user.email, "tier": user.tier}


@router.post("/login")
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    email = normalize_email(payload.email)
    user = (await db.execute(
        select(StudyUser).where(StudyUser.email == email))).scalar_one_or_none()

    # One message for "no such account", "no password set" and "wrong
    # password". Distinguishing them turns this endpoint into an oracle
    # for which email addresses are registered.
    if not user or not user.password_hash or not verify_password(payload.password,
                                                                 user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    return {"token": create_study_token(user.id, user.email),
            "email": user.email, "tier": user.tier}


@router.post("/checkout")
async def create_study_checkout(
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a Stripe Checkout Session to upgrade to the paid tier
    ($9.99/month per STUDY_MVP_SUMMARY.md). The user's tier is only
    flipped to "paid" by the webhook once Stripe confirms payment -
    see study_stripe_webhook below.
    """
    user = await verify_user(authorization, db)

    if user["tier"] == "paid":
        raise HTTPException(status_code=400, detail="Already on the paid tier")

    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Payments are not configured for this service")

    if payments_paused():
        raise HTTPException(status_code=503, detail=PAUSE_MESSAGE)

    base_url = os.getenv("PUBLIC_BASE_URL", "https://empire-v2-production.up.railway.app")

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "Study Assistant - Unlimited Plan"},
                    "unit_amount": PAID_TIER_PRICE_CENTS,
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
            mode="subscription",
            customer_email=user["email"],
            success_url=f"{base_url}/study-app?upgraded=true",
            cancel_url=f"{base_url}/study-app",
            metadata={"study_user_email": user["email"]},
        )
        return {"checkout_url": session.url, "session_id": session.id}
    except stripe.error.StripeError as e:
        log.error(f"Study checkout session creation failed: {e}")
        raise HTTPException(status_code=500, detail=f"Payment setup failed: {str(e)}")


@router.post("/webhook/stripe")
async def study_stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Stripe webhook: only place a user's tier actually flips to "paid"."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    # Falls back to the shared STRIPE_WEBHOOK_SECRET so this keeps working if
    # only one Stripe webhook endpoint is registered; set the dedicated var
    # once a separate endpoint (with its own signing secret) exists for /study.
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET_STUDY") or os.getenv("STRIPE_WEBHOOK_SECRET")

    if not webhook_secret:
        log.warning("STRIPE_WEBHOOK_SECRET_STUDY (or STRIPE_WEBHOOK_SECRET) not configured, cannot verify webhook")
        raise HTTPException(status_code=500, detail="Webhook not configured")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        email = session.get("metadata", {}).get("study_user_email")
        if email:
            result = await db.execute(select(StudyUser).where(StudyUser.email == email))
            db_user = result.scalar_one_or_none()
            if db_user:
                db_user.tier = "paid"
                db_user.stripe_customer_id = session.get("customer")
                db_user.stripe_subscription_id = session.get("subscription")
                await db.commit()
                log.info(f"Upgraded user to paid tier via Stripe: {email}")
            else:
                log.warning(f"Stripe webhook confirmed payment for unknown study user: {email}")

    return {"status": "success"}


@router.get("/user-stats")
async def get_user_stats(
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db)
):
    """
    Get usage statistics for the logged-in user.

    Args:
        authorization: Bearer token with email

    Returns:
        User tier, materials generated this month, and limits
    """
    user = await verify_user(authorization, db)

    # Get user from DB
    result = await db.execute(
        select(StudyUser).where(StudyUser.email == user["email"])
    )
    db_user = result.scalar_one()

    # Count materials this month
    month_ago = datetime.utcnow() - timedelta(days=30)
    count_result = await db.execute(
        select(func.count(StudyMaterial.id)).where(
            (StudyMaterial.user_id == user["email"]) &
            (StudyMaterial.created_at >= month_ago)
        )
    )
    materials_this_month = count_result.scalar() or 0

    limit = PAID_LIMIT if db_user.tier == "paid" else FREE_LIMIT

    return {
        "email": user["email"],
        "tier": db_user.tier,
        "materials_generated_this_month": materials_this_month,
        "monthly_limit": limit,
        "remaining": max(0, limit - materials_this_month),
        "created_at": db_user.created_at.isoformat()
    }


@router.post("/delete/{material_id}")
async def delete_material(
    material_id: int,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db)
):
    """
    Delete a study material (owner only).

    Args:
        material_id: ID of the material to delete
        authorization: Bearer token with email

    Returns:
        Confirmation of deletion
    """
    user = await verify_user(authorization, db)

    result = await db.execute(
        select(StudyMaterial).where(StudyMaterial.id == material_id)
    )
    material = result.scalar_one_or_none()

    if not material:
        raise HTTPException(status_code=404, detail="Material not found")

    if material.user_id != user["email"]:
        raise HTTPException(status_code=403, detail="You don't have permission to delete this")

    await db.delete(material)
    await db.commit()

    log.info(f"Deleted material {material_id} for {user['email']}")

    return {"status": "deleted", "material_id": material_id}


@router.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "ok",
        "service": "study-assistant",
        "version": "1.0.0",
        "api_key_configured": bool(CLAUDE_KEY)
    }
