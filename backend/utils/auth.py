# utils/auth.py — JWT tokens, password hashing, Google OAuth verification

from datetime import datetime, timedelta, timezone
from typing import Optional
import bcrypt
from jose import jwt, JWTError
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from config import settings

bearer_scheme = HTTPBearer(auto_error=False)


# ── Password ─────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    """Hash a plain-text password using bcrypt."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plain-text password against a bcrypt hash."""
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ── JWT ──────────────────────────────────────────────────────

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a signed JWT access token."""
    payload = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT. Raises HTTPException on failure."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── Current user dependency ───────────────────────────────────

def get_current_user_payload(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    """FastAPI dependency — validates Bearer token, returns decoded payload."""
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return decode_token(credentials.credentials)


def require_admin(payload: dict = Depends(get_current_user_payload)) -> dict:
    """FastAPI dependency — ensures the authenticated user has admin role."""
    if payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return payload


def require_taxly_admin(payload: dict = Depends(get_current_user_payload)) -> dict:
    """FastAPI dependency — ensures the user is the Taxly super-admin."""
    if not payload.get("is_taxly_admin"):
        raise HTTPException(status_code=403, detail="Taxly admin access required")
    return payload


# ── Google OAuth ─────────────────────────────────────────────

def verify_google_token(id_token_str: str) -> dict:
    """
    Verify a Google ID token and return the decoded claims.
    Claims include: sub (google_id), email, name, picture, email_verified
    Raises HTTPException if invalid.
    """
    try:
        claims = id_token.verify_oauth2_token(
            id_token_str,
            google_requests.Request(),
            settings.GOOGLE_CLIENT_ID,
        )
        if not claims.get("email_verified"):
            raise HTTPException(status_code=400, detail="Google email not verified")
        return claims
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid Google token: {e}")


# ── Trial helpers ─────────────────────────────────────────────

def is_trial_active(trial_expires_at: Optional[datetime]) -> bool:
    """Return True if the Google trial period has not yet expired."""
    if not trial_expires_at:
        return False
    return datetime.now(timezone.utc) < trial_expires_at


def trial_days_remaining(trial_expires_at: Optional[datetime]) -> int:
    """Return the number of full days remaining in the trial (0 if expired)."""
    if not trial_expires_at:
        return 0
    delta = trial_expires_at - datetime.now(timezone.utc)
    return max(0, delta.days)
