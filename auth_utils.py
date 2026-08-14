"""Authentication utilities: password hashing, JWT tokens, email verification."""

import logging
import os
from datetime import datetime, timedelta
from typing import Optional, Dict
import jwt
import bcrypt
import random
import string

from fastapi import HTTPException

log = logging.getLogger("auth_utils")

# JWT configuration.
#
# This previously defaulted to a hardcoded "change me in production"
# string. In a public repository that is not a placeholder, it is a
# published signing key: with SECRET_KEY unset
# in the environment, anyone who read the source could mint a valid token
# for any user id and the application would accept it. Nothing about that
# state looks broken from the outside, which is what makes it worse than
# a leaked credential - a leak gets rotated, this just sits there.
#
# No default now. Absent configuration means tokens cannot be issued and
# cannot be verified, which is the only safe reading of "no signing key".
SECRET_KEY = os.getenv("SECRET_KEY", "")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours
REFRESH_TOKEN_EXPIRE_DAYS = 7

# Surfaced at import, the same convention as worker_auth.py and
# study_auth.py. Without this the only symptom is a 500 the first time
# somebody tries to log in, which is exactly when you least want to be
# diagnosing configuration.
if not SECRET_KEY:
    log.warning("SECRET_KEY not configured - customer login and token refresh "
                "will fail with 500 until it is set in Railway Variables. "
                "Tokens are NOT being signed with a fallback key.")


def _require_secret() -> str:
    """Fail closed. An auth system that degrades to a known key under
    misconfiguration is worse than one that stops, because it still looks
    protected."""
    if not SECRET_KEY:
        raise HTTPException(status_code=500,
                            detail="Authentication is not configured (SECRET_KEY unset)")
    return SECRET_KEY


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash."""
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, _require_secret(), algorithm=ALGORITHM)
    return encoded_jwt


def create_refresh_token(data: dict) -> str:
    """Create a JWT refresh token."""
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})
    encoded_jwt = jwt.encode(to_encode, _require_secret(), algorithm=ALGORITHM)
    return encoded_jwt


def decode_token(token: str) -> Optional[Dict]:
    """Decode and validate a JWT token.

    Returns None with SECRET_KEY unset, so every token is rejected rather
    than verified against a published fallback key. The signature is
    unchanged, so callers keep treating None as "not authenticated".
    """
    if not SECRET_KEY:
        log.warning("decode_token called with SECRET_KEY unset - rejecting")
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def generate_verification_code() -> str:
    """Generate a 6-digit verification code."""
    return ''.join(random.choices(string.digits, k=6))


def send_verification_email(email: str, code: str) -> bool:
    """Send verification email to user."""
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        sender_email = os.getenv("GMAIL_EMAIL", "noreply@empire-v2.com")
        sender_password = os.getenv("GMAIL_PASSWORD", "")

        if not sender_password:
            print(f"⚠️  Email not configured - verification code for {email}: {code}")
            return True  # Pretend success for development

        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Verify Your Email - Empire v2"
        msg["From"] = sender_email
        msg["To"] = email

        text = f"Your verification code is: {code}\n\nThis code expires in 15 minutes."
        html = f"""\
        <html>
            <body>
                <h2>Email Verification</h2>
                <p>Your verification code is:</p>
                <h1 style="color: #2563eb; font-family: monospace;">{code}</h1>
                <p>This code expires in 15 minutes.</p>
                <p>If you didn't request this, please ignore this email.</p>
            </body>
        </html>
        """

        msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, email, msg.as_string())

        return True
    except Exception as e:
        print(f"Email send error: {e}")
        return False
