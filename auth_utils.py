"""Authentication utilities: password hashing, JWT tokens, email verification."""

import os
from datetime import datetime, timedelta
from typing import Optional, Dict
import jwt
import bcrypt
import random
import string

# JWT configuration
SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours
REFRESH_TOKEN_EXPIRE_DAYS = 7


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
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def create_refresh_token(data: dict) -> str:
    """Create a JWT refresh token."""
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def decode_token(token: str) -> Optional[Dict]:
    """Decode and validate a JWT token."""
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
