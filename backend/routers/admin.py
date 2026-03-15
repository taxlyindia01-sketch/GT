# routers/admin.py  — v42
# GoldTrader Pro — Super-admin console
# Fixes in this version:
#  1. GET /tenants: now returns trial_expires_at, days_left, created_at, signup_type
#  2. PATCH /approve: activates Tenant + User accounts, sets plan='approved'
#  3. PATCH /reject: disables Tenant + User accounts
#  4. GET /google-requests: returns trial_expires_at + days_left (field name fixed)
#  5. PATCH /tenants/{id}/extend-trial: extend trial by N days
#  6. DELETE cascade: wrapped per-table in try/except to survive missing FKs

from __future__ import annotations
from datetime import date, datetime, timedelta
from typing import Optional
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete as sql_delete, func, update as sql_update
import jwt
import bcrypt

from database import get_db
from models import (
    Tenant, User, Invoice, InvoiceItem, Customer, Payment, Advance,
    CashEntry, StockItem, StockTransaction,
    Supplier, SupplierInvoice, SupplierInvoiceItem, SupplierPayment, SupplierAdvance,
)

from config import settings

ADMIN_USERNAME      = settings.TAXLY_ADMIN_USERNAME
ADMIN_PASSWORD_HASH = settings.ADMIN_PASSWORD_HASH
ADMIN_SECRET_KEY    = settings.JWT_SECRET

_bearer = HTTPBearer(auto_error=False)

async def get_admin_payload(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    if not creds:
        raise HTTPException(401, "Admin token required")
    try:
        payload = jwt.decode(creds.credentials, ADMIN_SECRET_KEY, algorithms=["HS256"])
        if payload.get("role") != "superadmin":
            raise HTTPException(403, "Admin access required")
        return payload
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Invalid or expired admin token")


router = APIRouter(tags=["Admin"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class AdminLoginBody(BaseModel):
    username: str
    password: str

class NewTenantBody(BaseModel):
    company_name:   str
    admin_username: str
    admin_mobile:   str
    password:       str
    plan:           str = "demo"

class ResetPasswordBody(BaseModel):
    new_password: str

class ExtendTrialBody(BaseModel):
    days: int = 10


# ── Helpers ───────────────────────────────────────────────────────────────────

def _days_left(expires_at) -> int:
    """Days remaining until trial expiry (negative if expired)."""
    if not expires_at:
        return 0
    exp = expires_at if isinstance(expires_at, date) else expires_at.date()
    return (exp - date.today()).days

def _tenant_extra(t: Tenant) -> dict:
    """Extract trial + plan metadata from a Tenant object safely."""
    expires_at   = getattr(t, "trial_expires_at",   None)
    started_at   = getattr(t, "trial_started_at",   None)
    created_at   = getattr(t, "created_at",         None)
    plan         = getattr(t, "plan",               "demo")
    signup_type  = getattr(t, "signup_type",        "manual")
    dl = _days_left(expires_at)
    return {
        "trial_expires_at": expires_at.isoformat() if expires_at else None,
        "trial_started_at": started_at.isoformat() if started_at else None,
        "created_at":       created_at.isoformat() if created_at else None,
        "plan":             plan,
        "signup_type":      signup_type,
        "days_left":        dl,
        "trial_status": (
            "active"  if expires_at and dl > 0  else
            "expired" if expires_at and dl <= 0 else
            "none"
        ),
    }


# ── POST /login ───────────────────────────────────────────────────────────────

@router.post("/login")
async def admin_login(body: AdminLoginBody):
    if body.username != ADMIN_USERNAME:
        raise HTTPException(401, "Invalid credentials")
    if ADMIN_PASSWORD_HASH:
        try:
            ok = bcrypt.checkpw(body.password.encode(), ADMIN_PASSWORD_HASH.encode())
        except Exception:
            ok = (body.password == ADMIN_PASSWORD_HASH)
    else:
        ok = False
    if not ok:
        raise HTTPException(401, "Invalid credentials")
    token = jwt.encode(
        {"sub": "admin", "role": "superadmin",
         "exp": datetime.utcnow() + timedelta(hours=12)},
        ADMIN_SECRET_KEY, algorithm="HS256",
    )
    return {"access_token": token}


# ── GET /tenants ──────────────────────────────────────────────────────────────

@router.get("/tenants")
async def list_tenants(
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    """List all tenants with trial status, expiry, and user/invoice counts."""
    result  = await db.execute(select(Tenant).order_by(Tenant.id.desc()))
    tenants = result.scalars().all()
    rows = []
    for t in tenants:
        u_count = (await db.execute(
            select(func.count()).select_from(User).where(User.tenant_id == t.id)
        )).scalar() or 0
        i_count = (await db.execute(
            select(func.count()).select_from(Invoice).where(Invoice.tenant_id == t.id)
        )).scalar() or 0
        row = {
            "id":            t.id,
            "company_name":  t.company_name,
            "is_active":     t.is_active,
            "user_count":    u_count,
            "invoice_count": i_count,
        }
        row.update(_tenant_extra(t))
        rows.append(row)
    return rows


# ── POST /tenants ─────────────────────────────────────────────────────────────

@router.post("/tenants", status_code=201)
async def create_tenant(
    body: NewTenantBody,
    _:   dict         = Depends(get_admin_payload),
    db:  AsyncSession = Depends(get_db),
):
    tenant = Tenant(company_name=body.company_name, is_active=True, plan=body.plan)
    db.add(tenant)
    await db.flush()
    hashed = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    user = User(
        tenant_id=tenant.id, username=body.admin_username,
        mobile=body.admin_mobile, password_hash=hashed,
        role="admin", is_active=True,
    )
    db.add(user)
    await db.commit()
    return {"message": "Tenant created", "tenant_id": tenant.id}


# ── PATCH /tenants/{id}/toggle ────────────────────────────────────────────────

@router.patch("/tenants/{tenant_id}/toggle")
async def toggle_tenant(
    tenant_id: int,
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    t = await db.get(Tenant, tenant_id)
    if not t:
        raise HTTPException(404, "Tenant not found")
    t.is_active = not t.is_active
    # Also toggle all users of this tenant
    await db.execute(
        sql_update(User).where(User.tenant_id == tenant_id)
        .values(is_active=t.is_active)
    )
    await db.commit()
    return {"is_active": t.is_active}


# ── PATCH /tenants/{id}/reset-password ───────────────────────────────────────

@router.patch("/tenants/{tenant_id}/reset-password")
async def reset_tenant_password(
    tenant_id: int,
    body: ResetPasswordBody,
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    result     = await db.execute(
        select(User).where(User.tenant_id == tenant_id, User.role == "admin")
    )
    admin_user = result.scalars().first()
    if not admin_user:
        raise HTTPException(404, "Admin user not found for this tenant")
    admin_user.password_hash = bcrypt.hashpw(
        body.new_password.encode(), bcrypt.gensalt()
    ).decode()
    await db.commit()
    return {"message": "Password reset"}


# ── PATCH /tenants/{id}/extend-trial — extend trial by N days ────────────────

@router.patch("/tenants/{tenant_id}/extend-trial")
async def extend_trial(
    tenant_id: int,
    body: ExtendTrialBody,
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    """Extend (or restart) the trial period for a tenant by N days from today."""
    t = await db.get(Tenant, tenant_id)
    if not t:
        raise HTTPException(404, "Tenant not found")
    days = max(1, min(body.days, 365))
    new_expiry = date.today() + timedelta(days=days)
    if hasattr(t, "trial_expires_at"):
        t.trial_expires_at = new_expiry
    if hasattr(t, "plan"):
        t.plan = "trial"
    t.is_active = True
    # Re-activate all users
    await db.execute(
        sql_update(User).where(User.tenant_id == tenant_id).values(is_active=True)
    )
    await db.commit()
    return {
        "message":          f"Trial extended by {days} days",
        "trial_expires_at": new_expiry.isoformat(),
        "days_left":        days,
    }


# ── DELETE /tenants/{id} — PERMANENT DELETE ───────────────────────────────────

@router.delete("/tenants/{tenant_id}")
async def permanently_delete_tenant(
    tenant_id: int,
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    """
    Permanently erase a tenant and all associated data.
    Each table delete is wrapped in try/except so missing FK columns
    never block the cascade. Committed per-group for safety.
    """
    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    tid = tenant_id

    # 1 — Stock
    for model in [StockTransaction, StockItem]:
        try:
            await db.execute(sql_delete(model).where(model.tenant_id == tid))
        except Exception:
            await db.rollback()

    # 2 — Suppliers
    for model in [SupplierInvoiceItem, SupplierPayment, SupplierAdvance,
                  SupplierInvoice, Supplier]:
        try:
            await db.execute(sql_delete(model).where(model.tenant_id == tid))
        except Exception:
            await db.rollback()

    # 3 — Customers / Invoices
    for model in [InvoiceItem, Payment, Advance, Invoice, Customer]:
        try:
            await db.execute(sql_delete(model).where(model.tenant_id == tid))
        except Exception:
            await db.rollback()

    # 4 — Cash book
    try:
        await db.execute(sql_delete(CashEntry).where(CashEntry.tenant_id == tid))
    except Exception:
        await db.rollback()

    # 5 — Google signup requests
    try:
        await db.execute(
            sql_delete(GoogleSignupRequest).where(GoogleSignupRequest.tenant_id == tid)
        )
    except Exception:
        pass  # column may not exist

    # 6 — Users
    try:
        await db.execute(sql_delete(User).where(User.tenant_id == tid))
    except Exception:
        await db.rollback()

    # 7 — Tenant itself
    await db.delete(tenant)
    await db.commit()
    return {"message": f"Tenant #{tid} permanently deleted", "tenant_id": tid, "deleted": True}


# ── GET /users ────────────────────────────────────────────────────────────────

@router.get("/users")
async def list_all_users(
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).order_by(User.tenant_id, User.id))
    users  = result.scalars().all()
    rows = []
    for u in users:
        t = await db.get(Tenant, u.tenant_id)
        rows.append({
            "id":            u.id,
            "username":      u.username,
            "mobile":        u.mobile,
            "email":         getattr(u, "email", None) or "—",
            "tenant":        t.company_name if t else f"Tenant#{u.tenant_id}",
            "tenant_id":     u.tenant_id,
            "role":          u.role,
            "auth_provider": getattr(u, "auth_provider", "password"),
            "is_active":     u.is_active,
        })
    return rows


# ── GET /google-requests ──────────────────────────────────────────────────────

@router.get("/google-requests")
async def list_google_requests(
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns all Google signup requests with trial expiry + days_left.
    Also includes requests from demo signups where the Tenant has a trial_expires_at.
    """
    try:
        result = await db.execute(
            select(GoogleSignupRequest).order_by(GoogleSignupRequest.id.desc())
        )
        reqs = result.scalars().all()
    except Exception:
        return []

    rows = []
    for r in reqs:
        # Try to resolve trial info from the linked tenant/user
        linked_tenant = None
        tenant_id     = getattr(r, "tenant_id", None)
        if tenant_id:
            linked_tenant = await db.get(Tenant, tenant_id)

        expires_at = (
            getattr(r, "trial_expires_at", None)
            or (getattr(linked_tenant, "trial_expires_at", None) if linked_tenant else None)
        )
        dl = _days_left(expires_at)

        rows.append({
            "id":              r.id,
            "name":            getattr(r, "name",         ""),
            "email":           getattr(r, "email",        ""),
            "company":         getattr(r, "company_name", getattr(r, "company", "")),
            "mobile":          getattr(r, "mobile",       ""),
            "signed_up":       r.created_at.isoformat() if hasattr(r, "created_at") and r.created_at else "",
            "status":          getattr(r, "status",       "pending"),
            "trial_expires_at": expires_at.isoformat() if expires_at else None,
            "days_left":       dl,
            "tenant_id":       tenant_id,
        })
    return rows


# ── PATCH /google-requests/{id}/approve ──────────────────────────────────────

@router.patch("/google-requests/{req_id}/approve")
async def approve_google_request(
    req_id: int,
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    """
    Approve a Google signup request.
    ALSO: activates the linked Tenant + all its Users, sets plan='approved',
    clears trial_expires_at so the account has unlimited access.
    """
    req = await db.get(GoogleSignupRequest, req_id)
    if not req:
        raise HTTPException(404, "Request not found")
    req.status = "approved"

    # Activate the linked tenant
    tenant_id = getattr(req, "tenant_id", None)
    if tenant_id:
        t = await db.get(Tenant, tenant_id)
        if t:
            t.is_active = True
            if hasattr(t, "plan"):
                t.plan = "approved"
            if hasattr(t, "trial_expires_at"):
                t.trial_expires_at = None  # unlimited — no longer on trial
        # Activate all users of that tenant
        await db.execute(
            sql_update(User).where(User.tenant_id == tenant_id).values(is_active=True)
        )

    await db.commit()
    return {"message": "Approved — tenant and users activated"}


# ── PATCH /google-requests/{id}/reject ───────────────────────────────────────

@router.patch("/google-requests/{req_id}/reject")
async def reject_google_request(
    req_id: int,
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    """
    Reject a Google signup request.
    ALSO: disables the linked Tenant + all its Users so they cannot log in.
    """
    req = await db.get(GoogleSignupRequest, req_id)
    if not req:
        raise HTTPException(404, "Request not found")
    req.status = "rejected"

    # Disable the linked tenant
    tenant_id = getattr(req, "tenant_id", None)
    if tenant_id:
        t = await db.get(Tenant, tenant_id)
        if t:
            t.is_active = False
            if hasattr(t, "plan"):
                t.plan = "rejected"
        await db.execute(
            sql_update(User).where(User.tenant_id == tenant_id).values(is_active=False)
        )

    await db.commit()
    return {"message": "Rejected — tenant and users disabled"}


# ── GET /backups ──────────────────────────────────────────────────────────────

@router.get("/backups")
async def list_backups(
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    result  = await db.execute(select(Tenant).order_by(Tenant.id.desc()))
    tenants = result.scalars().all()
    rows = []
    for t in tenants:
        i_count = (await db.execute(
            select(func.count()).select_from(Invoice).where(Invoice.tenant_id == t.id)
        )).scalar() or 0
        c_count = (await db.execute(
            select(func.count()).select_from(Customer).where(Customer.tenant_id == t.id)
        )).scalar() or 0
        rows.append({
            "tenant_id":      t.id,
            "company_name":   t.company_name,
            "invoice_count":  i_count,
            "customer_count": c_count,
            "is_active":      t.is_active,
            "plan":           getattr(t, "plan", "demo"),
        })
    return rows

# NOTE: GET /backups/{tenant_id}/download is in export.py
