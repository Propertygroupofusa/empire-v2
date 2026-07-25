"""Worker authentication - real per-worker login (password + JWT) so a
worker can self-service their own jobs/bookings without needing the
shared admin key. Parallel to admin_auth.py's shared-secret pattern, but
per-account since workers are real individual users, not one shared
admin secret.

Uses bcrypt's own hashpw/checkpw API directly rather than passlib's
CryptContext wrapper - passlib's version-detection code crashes against
modern bcrypt (4.x+ removed the __about__ submodule passlib checks),
and neither passlib nor bcrypt were actually used anywhere in this repo
before now, so there's no existing behavior to stay compatible with.
"""
import os
from datetime import datetime, timedelta

import bcrypt
from fastapi import Header, HTTPException
from jose import jwt, JWTError

JWT_ALGORITHM = "HS256"
TOKEN_EXPIRE_DAYS = 7


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_worker_token(worker_id: int, email: str) -> str:
    secret = os.getenv("WORKER_JWT_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="Worker auth is not configured (WORKER_JWT_SECRET unset)")
    expire = datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS)
    claims = {"sub": str(worker_id), "email": email, "exp": int(expire.timestamp())}
    return jwt.encode(claims, secret, algorithm=JWT_ALGORITHM)


async def require_worker_auth(authorization: str = Header(None)) -> int:
    """FastAPI dependency: verifies the Bearer JWT and returns the
    authenticated worker's id. Fails closed - if WORKER_JWT_SECRET isn't
    configured, no token can ever be accepted as valid, rather than
    silently accepting unsigned/unverifiable tokens."""
    secret = os.getenv("WORKER_JWT_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="Worker auth is not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization[len("Bearer "):]
    try:
        payload = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    worker_id = payload.get("sub")
    if worker_id is None:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    return int(worker_id)
