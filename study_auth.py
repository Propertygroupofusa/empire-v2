"""Study-app authentication - real per-student login (password + JWT).

Replaces the previous scheme, which was not authentication at all:
routers/study.py accepted `Authorization: Bearer <any string with an @>`
as an identity, auto-created a StudyUser at tier="free", and returned
that user's tier. Nothing was verified. Two consequences, both fatal to a
paid product:

  - the paywall could not hold. The free allowance is 5 materials/month
    keyed on email, so a different email was another 5, forever, with no
    signup step and no confirmation.
  - a paying customer could be impersonated. Bearer <their-email>
    returned tier="paid" and unlimited generations on their subscription.

Mirrors worker_auth.py, which already solves this correctly for notaries,
and reuses its bcrypt helpers rather than adding a third copy of
hashpw/checkpw to this codebase.

A SEPARATE secret from WORKER_JWT_SECRET, deliberately. Students and
notaries are different populations with different privileges; one signing
key for both would mean a student token is structurally capable of being
replayed against worker endpoints, and the only thing standing in the way
would be whichever claims each route happens to check.
"""
import logging
import os
from datetime import datetime, timedelta

from fastapi import Header, HTTPException
from jose import jwt, JWTError

# bcrypt via worker_auth: passlib's CryptContext is NOT usable here -
# passlib is not in requirements.txt and its bcrypt backend reads
# bcrypt.__about__, removed in bcrypt 4.x. See #129.
from worker_auth import hash_password, verify_password  # noqa: F401  (re-exported)

log = logging.getLogger("study_auth")

JWT_ALGORITHM = "HS256"
TOKEN_EXPIRE_DAYS = 30          # students, not admins - a longer session is fine

# Surfaced at import time, same convention as worker_auth.py. A missing
# secret otherwise appears only as an opaque 500 the first time somebody
# tries to log in, which is exactly when you least want to be diagnosing
# configuration.
if not os.getenv("STUDY_JWT_SECRET"):
    log.warning("STUDY_JWT_SECRET not configured - study signup/login will fail "
                "with 500 until it is set in Railway Variables")

# bcrypt truncates silently at 72 BYTES. Rejecting longer input is better
# than accepting a password whose tail never mattered - a user who set a
# 100-character passphrase would otherwise be authenticated by its first
# 72 bytes without ever being told.
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_LEN = 8


def validate_password(password: str) -> None:
    if not password or len(password) < MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {MIN_PASSWORD_LEN} characters")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at most {MAX_PASSWORD_BYTES} bytes "
                   f"(bcrypt truncates beyond that, so the extra characters "
                   f"would not actually protect the account)")


def normalize_email(email: str) -> str:
    """Lower-cased and stripped. Without this, Student@x.com and
    student@x.com are two accounts with two separate free allowances -
    which is the same bypass the old scheme had, just less obvious."""
    return (email or "").strip().lower()


def create_study_token(user_id: int, email: str) -> str:
    secret = os.getenv("STUDY_JWT_SECRET")
    if not secret:
        raise HTTPException(status_code=500,
                            detail="Study auth is not configured (STUDY_JWT_SECRET unset)")
    expire = datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS)
    claims = {"sub": str(user_id), "email": email, "exp": int(expire.timestamp())}
    return jwt.encode(claims, secret, algorithm=JWT_ALGORITHM)


async def require_study_auth(authorization: str = Header(None)) -> dict:
    """FastAPI dependency: verifies the Bearer JWT, returns {id, email}.

    Fails CLOSED. With STUDY_JWT_SECRET unset no token can be accepted,
    rather than falling back to trusting whatever the client sent - which
    is precisely the hole this module exists to close. An auth system that
    degrades to "allow" under misconfiguration is worse than none, because
    it looks protected.
    """
    secret = os.getenv("STUDY_JWT_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="Study auth is not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401,
                            detail="Missing or invalid Authorization header")

    token = authorization[len("Bearer "):]
    try:
        payload = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
    except JWTError:
        # Deliberately does not distinguish expired from malformed from
        # wrongly-signed. The client can do nothing different with that
        # detail, and it tells an attacker whether a forged token was
        # structurally close.
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id, email = payload.get("sub"), payload.get("email")
    if user_id is None or not email:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    return {"id": int(user_id), "email": email}
