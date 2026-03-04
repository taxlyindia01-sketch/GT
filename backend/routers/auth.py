# routers/auth.py — Authentication endpoints

from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models import User, Tenant, ApprovalStatus, AuthProvider, RoleEnum, PlanEnum
from utils.auth import (
    verify_password, hash_password, create_access_token,
    verify_google_token, get_current_user_payload,
    is_trial_active, trial_days_remaining
)
from config import settings

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────

class PasswordLoginRequest(BaseModel):
    username_or_mobile: str
    password: str

class GoogleLoginRequest(BaseModel):
    id_token: str                       # Google ID token from frontend

class GoogleSignupRequest(BaseModel):
    id_token: str
    company_name: str = Field(..., min_length=2, max_length=200)
    mobile: str       = Field(..., pattern=r"^\d{10}$")

class DemoSignupRequest(BaseModel):
    name:         str = Field(..., min_length=2, max_length=100)
    mobile:       str = Field(..., pattern=r"^\d{10}$")
    company_name: str = Field(..., min_length=2, max_length=200)
    password:     str = Field(..., min_length=8)

class TokenResponse(BaseModel):
    access_token:    str
    token_type:      str = "bearer"
    user_name:       str
    user_role:       str
    tenant_name:     str
    trial_active:    bool = False
    trial_days_left: int  = 0


# ── Password Login ────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
async def password_login(body: PasswordLoginRequest, db: AsyncSession = Depends(get_db)):
    """Login with username/mobile + password."""
    result = await db.execute(
        select(User).where(
            (User.username == body.username_or_mobile) |
            (User.mobile   == body.username_or_mobile)
        )
    )
    user = result.scalar_one_or_none()

    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled. Contact admin.")

    tenant = await db.get(Tenant, user.tenant_id)

    token = create_access_token({
        "sub":        str(user.id),
        "tenant_id":  user.tenant_id,
        "role":       user.role.value,
        "mobile":     user.mobile,
    })

    return TokenResponse(
        access_token=token,
        user_name=user.username,
        user_role=user.role.value,
        tenant_name=tenant.company_name if tenant else "",
    )


# ── Google Login (existing approved account) ──────────────────

@router.post("/google/login", response_model=TokenResponse)
async def google_login(body: GoogleLoginRequest, db: AsyncSession = Depends(get_db)):
    """
    Login with Google ID token.
    - If user exists and approved → login
    - If user is on active trial → login with trial info
    - If trial expired → reject, prompt for admin approval
    - If no account → redirect to signup
    """
    claims    = verify_google_token(body.id_token)
    google_id = claims["sub"]
    email     = claims["email"]

    result = await db.execute(select(User).where(User.google_id == google_id))
    user   = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=404,
            detail="No account found. Please sign up with Google first.",
            headers={"X-Action": "signup"},
        )

    # Check trial / approval status
    if user.approval_status == ApprovalStatus.trial:
        if not is_trial_active(user.trial_expires_at):
            # Trial expired — move to pending
            user.approval_status = ApprovalStatus.pending
            await db.commit()
            raise HTTPException(
                status_code=403,
                detail="Your 10-day trial has ended. Awaiting admin approval.",
                headers={"X-Action": "pending"},
            )
    elif user.approval_status == ApprovalStatus.pending:
        raise HTTPException(
            status_code=403,
            detail="Your account is pending admin approval.",
            headers={"X-Action": "pending"},
        )
    elif user.approval_status == ApprovalStatus.rejected:
        raise HTTPException(status_code=403, detail="Account access denied.")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled.")

    tenant = await db.get(Tenant, user.tenant_id)
    days_left = trial_days_remaining(user.trial_expires_at) if user.approval_status == ApprovalStatus.trial else 0

    token = create_access_token({
        "sub":          str(user.id),
        "tenant_id":    user.tenant_id,
        "role":         user.role.value,
        "mobile":       user.mobile,
        "trial_active": user.approval_status == ApprovalStatus.trial,
    })

    return TokenResponse(
        access_token=token,
        user_name=user.username or email,
        user_role=user.role.value,
        tenant_name=tenant.company_name if tenant else "",
        trial_active=user.approval_status == ApprovalStatus.trial,
        trial_days_left=days_left,
    )


# ── Google Signup (new account — 10-day trial) ───────────────

@router.post("/google/signup", response_model=TokenResponse, status_code=201)
async def google_signup(body: GoogleSignupRequest, db: AsyncSession = Depends(get_db)):
    """
    Register a new user via Google OAuth.
    - Creates a Tenant + admin User immediately
    - Sets trial_expires_at = now + 10 days
    - No admin approval needed for trial period
    - After 10 days: status → pending, admin must approve to continue
    """
    claims    = verify_google_token(body.id_token)
    google_id = claims["sub"]
    email     = claims["email"]
    name      = claims.get("name", email)

    # Check if Google account already has an account
    result = await db.execute(select(User).where(User.google_id == google_id))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Google account already registered. Please login.")

    # Create Tenant
    tenant = Tenant(
        company_name=body.company_name,
        plan=PlanEnum.demo,
        is_active=True,
    )
    db.add(tenant)
    await db.flush()   # get tenant.id

    # Create admin user with 10-day trial
    trial_expires = datetime.now(timezone.utc) + timedelta(days=settings.TRIAL_DAYS)
    user = User(
        tenant_id=tenant.id,
        username=name,
        mobile=body.mobile,
        email=email,
        password_hash=hash_password("google-oauth-no-password"),  # placeholder
        role=RoleEnum.admin,
        auth_provider=AuthProvider.google,
        google_id=google_id,
        approval_status=ApprovalStatus.trial,
        trial_expires_at=trial_expires,
        company_name=body.company_name,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_access_token({
        "sub":          str(user.id),
        "tenant_id":    tenant.id,
        "role":         RoleEnum.admin.value,
        "mobile":       body.mobile,
        "trial_active": True,
    })

    return TokenResponse(
        access_token=token,
        user_name=name,
        user_role=RoleEnum.admin.value,
        tenant_name=body.company_name,
        trial_active=True,
        trial_days_left=settings.TRIAL_DAYS,
    )


# ── Demo Signup (password-based, no Google) ───────────────────

@router.post("/signup-demo", response_model=TokenResponse, status_code=201)
async def demo_signup(body: DemoSignupRequest, db: AsyncSession = Depends(get_db)):
    """
    Register with name + mobile + company + password.
    10-day trial, no approval needed.
    """
    # Check mobile uniqueness globally
    result = await db.execute(select(User).where(User.mobile == body.mobile))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Mobile number already registered.")

    trial_expires = datetime.now(timezone.utc) + timedelta(days=settings.TRIAL_DAYS)

    tenant = Tenant(company_name=body.company_name, plan=PlanEnum.demo, is_active=True)
    db.add(tenant)
    await db.flush()

    user = User(
        tenant_id=tenant.id,
        username=body.name,
        mobile=body.mobile,
        password_hash=hash_password(body.password),
        role=RoleEnum.admin,
        auth_provider=AuthProvider.password,
        approval_status=ApprovalStatus.trial,
        trial_expires_at=trial_expires,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_access_token({
        "sub":          str(user.id),
        "tenant_id":    tenant.id,
        "role":         RoleEnum.admin.value,
        "mobile":       body.mobile,
        "trial_active": True,
    })

    return TokenResponse(
        access_token=token,
        user_name=body.name,
        user_role=RoleEnum.admin.value,
        tenant_name=body.company_name,
        trial_active=True,
        trial_days_left=settings.TRIAL_DAYS,
    )


# ── Trial Status Check ────────────────────────────────────────

@router.get("/trial-status")
async def trial_status(
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    """Check if the current user's trial is still active."""
    user = await db.get(User, int(payload["sub"]))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    active    = is_trial_active(user.trial_expires_at)
    days_left = trial_days_remaining(user.trial_expires_at)

    if not active and user.approval_status == ApprovalStatus.trial:
        user.approval_status = ApprovalStatus.pending
        await db.commit()

    return {
        "trial_active":    active,
        "trial_days_left": days_left,
        "approval_status": user.approval_status.value,
        "trial_expires_at": user.trial_expires_at,
    }
