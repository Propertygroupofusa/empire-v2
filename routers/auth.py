"""Customer authentication router: registration, login, token refresh, email verification."""

from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timedelta
import logging

from database import get_db
from models import User
from auth_utils import (
    hash_password, verify_password, create_access_token, create_refresh_token,
    decode_token, generate_verification_code, send_verification_email
)

log = logging.getLogger("auth")
router = APIRouter(tags=["authentication"])


# ============================================================
# SCHEMAS
# ============================================================

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    name: str = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    code: str


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int
    user: dict


# ============================================================
# DEPENDENCIES
# ============================================================

async def get_current_user(
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db)
):
    """Dependency to extract and validate current user from JWT token."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")

    token = authorization.replace("Bearer ", "").strip()
    payload = decode_token(token)

    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Fetch user from database
    result = await db.execute(select(User).where(User.id == int(user_id)))
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    return user


# ============================================================
# POST /auth/register - Register new user
# ============================================================

@router.post("/register", response_model=TokenResponse)
async def register(request: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """
    Register a new customer account.
    Returns access token, refresh token, and user info.
    """
    # Check if user already exists
    result = await db.execute(select(User).where(User.email == request.email.lower()))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")

    # Create new user
    user = User(
        email=request.email.lower(),
        password_hash=hash_password(request.password),
        name=request.name,
        is_verified=False,
        verification_code=generate_verification_code(),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    log.info(f"New user registered: {user.email}")

    # Send verification email
    send_verification_email(user.email, user.verification_code)

    # Generate tokens
    access_token = create_access_token(data={"sub": str(user.id)})
    refresh_token = create_refresh_token(data={"sub": str(user.id)})

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": 60 * 24 * 60,  # seconds
        "user": user.to_dict(),
    }


# ============================================================
# POST /auth/login - Login user
# ============================================================

@router.post("/login", response_model=TokenResponse)
async def login(request: LoginRequest, db: AsyncSession = Depends(get_db)):
    """
    Login with email and password.
    Returns access token, refresh token, and user info.
    """
    # Find user by email
    result = await db.execute(select(User).where(User.email == request.email.lower()))
    user = result.scalar_one_or_none()

    if not user or not verify_password(request.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is inactive")

    log.info(f"User logged in: {user.email}")

    # Generate tokens
    access_token = create_access_token(data={"sub": str(user.id)})
    refresh_token = create_refresh_token(data={"sub": str(user.id)})

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": 60 * 24 * 60,  # seconds
        "user": user.to_dict(),
    }


# ============================================================
# POST /auth/verify-email - Verify email with code
# ============================================================

@router.post("/verify-email")
async def verify_email(request: VerifyEmailRequest, db: AsyncSession = Depends(get_db)):
    """
    Verify user email with verification code sent via email.
    """
    # Find user by email
    result = await db.execute(select(User).where(User.email == request.email.lower()))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.is_verified:
        return {"message": "Email already verified"}

    # Check verification code
    if user.verification_code != request.code:
        raise HTTPException(status_code=400, detail="Invalid verification code")

    # Mark as verified
    user.is_verified = True
    user.verification_code = None
    await db.commit()

    log.info(f"Email verified: {user.email}")

    return {"message": "Email verified successfully"}


# ============================================================
# POST /auth/refresh-token - Refresh access token
# ============================================================

@router.post("/refresh-token")
async def refresh_token(request: RefreshTokenRequest, db: AsyncSession = Depends(get_db)):
    """
    Refresh access token using refresh token.
    """
    payload = decode_token(request.refresh_token)

    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    user_id = payload.get("sub")
    result = await db.execute(select(User).where(User.id == int(user_id)))
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    # Generate new access token
    access_token = create_access_token(data={"sub": str(user.id)})

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": 60 * 24 * 60,  # seconds
    }


# ============================================================
# GET /auth/me - Get current user profile
# ============================================================

@router.get("/me")
async def get_profile(user: User = Depends(get_current_user)):
    """
    Get current authenticated user's profile.
    Requires valid JWT token in Authorization header.
    """
    return user.to_dict()


# ============================================================
# POST /auth/logout - Logout (client-side token deletion)
# ============================================================

@router.post("/logout")
async def logout(user: User = Depends(get_current_user)):
    """
    Logout user. Client should delete the token on their side.
    In a production system, you'd add tokens to a blacklist.
    """
    log.info(f"User logged out: {user.email}")
    return {"message": "Logged out successfully"}


# ============================================================
# PUT /auth/me - Update user profile
# ============================================================

class UpdateProfileRequest(BaseModel):
    name: str = None


@router.put("/me")
async def update_profile(
    request: UpdateProfileRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Update user profile (name, etc).
    """
    if request.name:
        user.name = request.name

    await db.commit()
    log.info(f"User profile updated: {user.email}")

    return user.to_dict()
