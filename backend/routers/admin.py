# routers/admin.py — Taxly super-admin endpoints
# Admin credentials: username=Taxly, password=@Gsf025@

import re
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from models import Tenant, User, Invoice, Customer, ApprovalStatus
from utils.auth import (
    verify_password, hash_password, create_access_token, require_taxly_admin
)
from utils.email import send_approval_email, send_rejection_email
from config import settings

router = APIRouter()


# ── Admin Login ───────────────────────────────────────────────

class AdminLoginRequest(BaseModel):
    username: str
    password: str


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not re.search(r"\d", v):
            raise ValueError("Password must contain at least one digit (0–9).")
        if not re.search(r"[!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>/?`~]", v):
            raise ValueError("Password must contain at least one special character (!@#$…).")
        return v

@router.post("/login")
async def admin_login(body: AdminLoginRequest):
    """
    Taxly super-admin login.
    Credentials: username='Taxly', password='@Gsf025@'
    """
    if body.username != settings.TAXLY_ADMIN_USERNAME:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not verify_password(body.password, settings.ADMIN_PASSWORD_HASH):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token({
        "sub":             "taxly-admin",
        "is_taxly_admin":  True,
        "role":            "super_admin",
    })
    return {"access_token": token, "token_type": "bearer", "username": "Taxly"}


# ── Tenant Management ─────────────────────────────────────────

@router.get("/tenants")
async def list_tenants(
    _:  dict          = Depends(require_taxly_admin),
    db: AsyncSession  = Depends(get_db),
):
    """List all tenants with invoice and user counts."""
    result  = await db.execute(select(Tenant).order_by(Tenant.id))
    tenants = result.scalars().all()

    rows = []
    for t in tenants:
        inv_count  = await db.scalar(select(func.count()).where(Invoice.tenant_id  == t.id)) or 0
        user_count = await db.scalar(select(func.count()).where(User.tenant_id     == t.id)) or 0
        rows.append({
            "id":           t.id,
            "company_name": t.company_name,
            "plan":         t.plan.value,
            "is_active":    t.is_active,
            "user_count":   user_count,
            "invoice_count":inv_count,
            "created_at":   t.created_at.isoformat(),
        })
    return rows


@router.post("/tenants", status_code=201)
async def create_tenant(
    body: dict,
    _:   dict         = Depends(require_taxly_admin),
    db:  AsyncSession = Depends(get_db),
):
    """Create a new tenant with initial admin user."""
    tenant = Tenant(company_name=body["company_name"], plan=body.get("plan", "demo"))
    db.add(tenant)
    await db.flush()

    admin_user = User(
        tenant_id=tenant.id,
        username=body["admin_username"],
        mobile=body["admin_mobile"],
        password_hash=hash_password(body["password"]),
        role="admin",
        is_active=True,
        approval_status=ApprovalStatus.approved,
    )
    db.add(admin_user)
    await db.commit()
    return {"tenant_id": tenant.id, "message": "Tenant created successfully"}


@router.patch("/tenants/{tenant_id}/toggle")
async def toggle_tenant(
    tenant_id: int,
    _:   dict         = Depends(require_taxly_admin),
    db:  AsyncSession = Depends(get_db),
):
    """Enable or disable a tenant."""
    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    tenant.is_active = not tenant.is_active
    await db.commit()
    return {"tenant_id": tenant_id, "is_active": tenant.is_active}


@router.patch("/tenants/{tenant_id}/reset-password")
async def reset_tenant_password(
    tenant_id: int,
    body:      ResetPasswordRequest,
    _:   dict         = Depends(require_taxly_admin),
    db:  AsyncSession = Depends(get_db),
):
    """Reset the admin password for a tenant. Password must be ≥8 chars with digit + special char."""
    result = await db.execute(
        select(User).where(User.tenant_id == tenant_id, User.role == "admin")
    )
    admin = result.scalars().first()
    if not admin:
        raise HTTPException(status_code=404, detail="Admin user not found for this tenant")
    admin.password_hash = hash_password(body.new_password)
    await db.commit()
    return {"message": f"Password reset for {admin.username}"}


# ── User Management ───────────────────────────────────────────

@router.get("/users")
async def list_all_users(
    _:  dict         = Depends(require_taxly_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all users across all tenants."""
    result  = await db.execute(select(User).order_by(User.tenant_id, User.id))
    users   = result.scalars().all()

    tenant_cache = {}
    rows = []
    for u in users:
        if u.tenant_id not in tenant_cache:
            t = await db.get(Tenant, u.tenant_id)
            tenant_cache[u.tenant_id] = t.company_name if t else "Unknown"
        rows.append({
            "id":              u.id,
            "username":        u.username,
            "mobile":          u.mobile,
            "email":           u.email,
            "role":            u.role.value,
            "tenant":          tenant_cache[u.tenant_id],
            "tenant_id":       u.tenant_id,
            "is_active":       u.is_active,
            "auth_provider":   u.auth_provider.value,
            "approval_status": u.approval_status.value,
        })
    return rows


# ── Google Signup Requests ────────────────────────────────────

@router.get("/google-requests")
async def list_google_requests(
    _:  dict         = Depends(require_taxly_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    List all users who signed up via Google.
    Includes trial users (10-day free) and pending approvals (trial expired).
    """
    result = await db.execute(
        select(User).where(
            User.auth_provider == "google",
            User.approval_status.in_(["trial", "pending", "approved", "rejected"])
        ).order_by(User.created_at.desc())
    )
    users = result.scalars().all()

    now = datetime.now(timezone.utc)
    rows = []
    for u in users:
        days_left = 0
        if u.trial_expires_at and u.approval_status.value == "trial":
            delta     = u.trial_expires_at - now
            days_left = max(0, delta.days)
            if days_left == 0:
                u.approval_status = ApprovalStatus.pending

        rows.append({
            "id":              u.id,
            "name":            u.username,
            "email":           u.email,
            "company":         u.company_name,
            "mobile":          u.mobile,
            "status":          u.approval_status.value,
            "trial_expires_at":u.trial_expires_at.isoformat() if u.trial_expires_at else None,
            "days_left":       days_left,
            "signed_up":       u.created_at.date().isoformat(),
        })

    await db.commit()   # commit any pending→trial changes
    return rows


@router.patch("/google-requests/{user_id}/approve")
async def approve_google_user(
    user_id:   int,
    body:      dict = {},
    _:   dict         = Depends(require_taxly_admin),
    db:  AsyncSession = Depends(get_db),
):
    """
    Approve a Google signup user.
    Sets status → approved, unlocks full CRM access.
    In production: also sends approval email via SMTP.
    """
    user = await db.get(User, user_id)
    if not user or user.auth_provider.value != "google":
        raise HTTPException(status_code=404, detail="Google user not found")

    user.approval_status  = ApprovalStatus.approved
    user.trial_expires_at = None   # clear trial expiry on approval
    user.role             = body.get("role", user.role.value)
    await db.commit()

    # Send approval email — fire-and-forget, never raises
    if user.email:
        import asyncio
        asyncio.create_task(
            send_approval_email(user.email, user.username or "there", user.company_name or "your business")
        )

    return {"message": f"{user.username} approved successfully. They can now log in.",
            "email_sent": bool(user.email)}


@router.patch("/google-requests/{user_id}/reject")
async def reject_google_user(
    user_id: int,
    _:   dict         = Depends(require_taxly_admin),
    db:  AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.approval_status = ApprovalStatus.rejected
    await db.commit()

    if user.email:
        import asyncio
        asyncio.create_task(
            send_rejection_email(user.email, user.username or "there")
        )

    return {"message": f"{user.username} rejected.", "email_sent": bool(user.email)}


# ── Tenant Backups ────────────────────────────────────────────

@router.get("/backups")
async def list_backups(
    _:  dict         = Depends(require_taxly_admin),
    db: AsyncSession = Depends(get_db),
):
    """List backup info for all tenants."""
    result  = await db.execute(select(Tenant).order_by(Tenant.id))
    tenants = result.scalars().all()

    rows = []
    for t in tenants:
        inv_count  = await db.scalar(select(func.count()).where(Invoice.tenant_id  == t.id)) or 0
        cust_count = await db.scalar(select(func.count()).where(Customer.tenant_id == t.id)) or 0
        rows.append({
            "tenant_id":    t.id,
            "company_name": t.company_name,
            "is_active":    t.is_active,
            "invoice_count":inv_count,
            "customer_count":cust_count,
        })
    return rows


@router.get("/backups/{tenant_id}/download")
async def download_tenant_backup(
    tenant_id: int,
    _:   dict         = Depends(require_taxly_admin),
    db:  AsyncSession = Depends(get_db),
):
    """Download full Excel backup for a specific tenant. See export router for implementation."""
    # Delegates to export router logic
    return {"message": f"Use GET /api/export/excel?tenant_id={tenant_id} for download", "tenant_id": tenant_id}
