"""AI Study Material Generation Router"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Header
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func
from datetime import datetime, timedelta
import os
import base64
import json
import logging
import httpx
from io import BytesIO
from typing import Optional

import anthropic
from database import get_db
from models import StudyMaterial, StudyUser

log = logging.getLogger("study")
router = APIRouter()

CLAUDE_KEY = os.getenv("ANTHROPIC_API_KEY", "")
client = anthropic.Anthropic(api_key=CLAUDE_KEY)

# Free tier limits
FREE_LIMIT = 5  # materials per month
PAID_LIMIT = 999


async def verify_user(authorization: Optional[str] = Header(None), db: AsyncSession = Depends(get_db)):
    """Extract user email from auth header (Bearer token format)"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized - use Bearer token with email")

    email = authorization.replace("Bearer ", "").strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=401, detail="Invalid email in Bearer token")

    # Get or create user
    result = await db.execute(select(StudyUser).where(StudyUser.email == email))
    user = result.scalar_one_or_none()

    if not user:
        user = StudyUser(email=email, tier="free")
        db.add(user)
        await db.commit()
        await db.refresh(user)
        log.info(f"Created new study user: {email}")

    return {"email": email, "tier": user.tier, "user_obj": user}


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
            detail=f"Free tier limit ({FREE_LIMIT}/month) exceeded. Upgrade to unlimited at /study/upgrade"
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

    # Validate material type
    if material_type not in ["guide", "quiz", "flashcards"]:
        raise HTTPException(
            status_code=400,
            detail="material_type must be one of: guide, quiz, flashcards"
        )

    # Verify user
    user = await verify_user(authorization, db)
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


@router.post("/upgrade")
async def upgrade_to_paid(
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db)
):
    """
    Upgrade user to paid tier.

    In production, this would be called by Stripe webhook after payment.
    For MVP, can be called directly to test paid features.

    Args:
        authorization: Bearer token with email

    Returns:
        Updated user tier information
    """
    user = await verify_user(authorization, db)

    result = await db.execute(
        select(StudyUser).where(StudyUser.email == user["email"])
    )
    db_user = result.scalar_one()
    db_user.tier = "paid"
    await db.commit()
    await db.refresh(db_user)

    log.info(f"Upgraded user to paid tier: {user['email']}")

    return {
        "status": "upgraded",
        "email": user["email"],
        "tier": "paid",
        "materials_limit": "unlimited"
    }


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
