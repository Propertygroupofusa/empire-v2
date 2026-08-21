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
from github_device_flow import (
    request_device_code, poll_device_flow, get_github_user,
    PendingDeviceFlow, DeviceFlowError
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


# ============================================================
# GITHUB DEVICE FLOW AUTHENTICATION
# ============================================================

class DeviceCodeResponse(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class PollDeviceFlowRequest(BaseModel):
    device_code: str


class GitHubAuthResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int
    user: dict


@router.post("/github/device-code", response_model=DeviceCodeResponse)
async def github_request_device_code():
    """
    Step 1 of GitHub device flow: Request a device code and user code.

    User should visit the verification_uri and enter the user_code.
    Client then polls /github/device-poll to check if authorized.

    Example response:
    {
        "device_code": "long_random_string",
        "user_code": "ABCD-1234",
        "verification_uri": "https://github.com/login/device",
        "expires_in": 900,
        "interval": 5
    }
    """
    try:
        response = await request_device_code()
        device_code = response["device_code"]

        # Store pending device flow
        PendingDeviceFlow.create(
            device_code=device_code,
            user_code=response["user_code"],
            verification_uri=response["verification_uri"],
        )

        return response
    except DeviceFlowError as e:
        log.error(f"Device code request failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/github/device-poll", response_model=GitHubAuthResponse)
async def github_poll_device_flow(
    request: PollDeviceFlowRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Step 2 of GitHub device flow: Poll to check if user authorized the device.

    Poll this endpoint repeatedly (respecting the `interval` from device-code)
    until the user authorizes or the device code expires.

    Returns tokens and user info once authorized.
    Raises 401 if still waiting or denied.
    """
    pending_flow = PendingDeviceFlow.get(request.device_code)
    if not pending_flow:
        raise HTTPException(
            status_code=401,
            detail="Device code not found or expired. Request a new one.",
        )

    # If already authorized, return cached tokens
    if pending_flow["access_token"]:
        user = pending_flow["github_user"]
        github_user = await _github_user_to_app_user(user, db)

        access_token = create_access_token(data={"sub": str(github_user.id)})
        refresh_token = create_refresh_token(data={"sub": str(github_user.id)})

        # Cleanup
        PendingDeviceFlow.cleanup(request.device_code)

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": 60 * 24 * 60,  # seconds
            "user": github_user.to_dict(),
        }

    # Poll GitHub to check if authorized
    try:
        token_response = await poll_device_flow(request.device_code)
    except DeviceFlowError as e:
        log.error(f"Device flow poll failed: {e}")
        raise HTTPException(status_code=401, detail=str(e))

    if token_response is None:
        # Still waiting for user authorization
        raise HTTPException(
            status_code=202,  # Accepted - still processing
            detail="Waiting for authorization. Please visit the verification URL.",
        )

    # User authorized - get their GitHub profile
    access_token = token_response.get("access_token")
    if not access_token:
        raise HTTPException(status_code=500, detail="No access token in GitHub response")

    try:
        github_user_data = await get_github_user(access_token)
    except DeviceFlowError as e:
        log.error(f"Failed to get GitHub user info: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # Create or link user in our database
    github_user = await _github_user_to_app_user(github_user_data, db)

    # Update with access token
    github_user.github_access_token = access_token
    await db.commit()

    log.info(f"GitHub device flow authorized: {github_user.email}")

    # Cache successful authorization
    PendingDeviceFlow.set_token(
        request.device_code,
        access_token,
        github_user_data,
    )

    # Generate our own tokens
    app_access_token = create_access_token(data={"sub": str(github_user.id)})
    app_refresh_token = create_refresh_token(data={"sub": str(github_user.id)})

    # Cleanup
    PendingDeviceFlow.cleanup(request.device_code)

    return {
        "access_token": app_access_token,
        "refresh_token": app_refresh_token,
        "token_type": "bearer",
        "expires_in": 60 * 24 * 60,  # seconds
        "user": github_user.to_dict(),
    }


async def _github_user_to_app_user(github_user: dict, db: AsyncSession) -> User:
    """
    Create or link a GitHub user to an app user account.

    If user with this github_id exists, update it.
    If user with this email exists, link it.
    Otherwise create a new user.
    """
    github_id = github_user.get("id")
    github_login = github_user.get("login")
    github_email = github_user.get("email")
    github_name = github_user.get("name")

    # Check if user with this github_id exists
    result = await db.execute(select(User).where(User.github_id == github_id))
    user = result.scalar_one_or_none()

    if user:
        # Update existing linked user
        user.github_username = github_login
        if github_email and not user.email:
            user.email = github_email
        if github_name and not user.name:
            user.name = github_name
        await db.commit()
        return user

    # Check if user with this email exists
    if github_email:
        result = await db.execute(select(User).where(User.email == github_email))
        user = result.scalar_one_or_none()
        if user:
            # Link existing user to GitHub
            user.github_id = github_id
            user.github_username = github_login
            if not user.name:
                user.name = github_name
            await db.commit()
            return user

    # Create new user from GitHub profile
    user = User(
        email=github_email or f"{github_login}@github",
        name=github_name or github_login,
        github_id=github_id,
        github_username=github_login,
        is_verified=True,  # GitHub verified users
        password_hash="",  # No password for OAuth users
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    log.info(f"New user created from GitHub: {user.email}")
    return user


@router.get("/github/user")
async def get_github_profile(
    user: User = Depends(get_current_user),
):
    """
    Get current user's GitHub profile information if linked.
    """
    if not user.github_id:
        raise HTTPException(status_code=404, detail="User not linked to GitHub")

    return {
        "github_id": user.github_id,
        "github_username": user.github_username,
        "email": user.email,
        "name": user.name,
    }
