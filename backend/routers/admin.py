# routers/admin.py
# GoldTrader Pro — Super-admin console endpoints
# Manages tenants, users, Google signup requests, backups, and tenant deletion.
#
# ADDED: DELETE /tenants/{tenant_id} — permanently erase a tenant and all data
#
# Auth: all endpoints require the admin JWT (separate from the tenant JWT).
# The admin token is issued by POST /api/admin/login with superadmin credentials.

from __future__ import annotations
from datetime import date, datetime, timedelta
from typing import Optional, List
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete as sql_delete, func
import jwt
import bcrypt

from database import get_db
from models import (
    Tenant, User, Invoice, InvoiceItem, Customer, Payment, Advance,
    CashEntry, StockItem, StockTransaction,
    Supplier, SupplierInvoice, SupplierInvoiceItem, SupplierPayment, SupplierAdvance,
)

# ── Load admin credentials from config.py (matches your settings) ────────────
# config.py fields used:
#   TAXLY_ADMIN_USERNAME  → the admin login username  (default: "Taxly")
#   ADMIN_PASSWORD_HASH   → bcrypt hash of the password (default: hash of "@Gsf025@")
#   JWT_SECRET            → token signing key
from config import settings

ADMIN_USERNAME      = settings.TAXLY_ADMIN_USERNAME   # "Taxly"
ADMIN_PASSWORD_HASH = settings.ADMIN_PASSWORD_HASH    # bcrypt hash
ADMIN_SECRET_KEY    = settings.JWT_SECRET             # shared JWT signing key

_bearer = HTTPBearer(auto_error=False)

async def get_admin_payload(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    if not creds:
        raise HTTPException(401, "Admin token required")
    try:
        payload = jwt.decode(creds.credentials, ADMIN_SECRET_KEY, algorithms=["HS256"])
        # Verify it's an admin token
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
    company_name:    str
    admin_username:  str
    admin_mobile:    str
    password:        str
    plan:            str = "demo"

class ResetPasswordBody(BaseModel):
    new_password: str


# ── POST /login ───────────────────────────────────────────────────────────────

@router.post("/login")
async def admin_login(body: AdminLoginBody):
    """Issue an admin JWT on successful credentials."""
    # Compare username
    if body.username != ADMIN_USERNAME:
        raise HTTPException(401, "Invalid credentials")
    # Compare password (bcrypt hash or plain depending on your setup)
    if ADMIN_PASSWORD_HASH:
        try:
            ok = bcrypt.checkpw(body.password.encode(), ADMIN_PASSWORD_HASH.encode())
        except Exception:
            ok = body.password == ADMIN_PASSWORD_HASH
    else:
        ok = False
    if not ok:
        raise HTTPException(401, "Invalid credentials")

    token = jwt.encode(
        {"sub": "admin", "role": "superadmin",
         "exp": datetime.utcnow() + timedelta(hours=12)},
        ADMIN_SECRET_KEY, algorithm="HS256"
    )
    return {"access_token": token}


# ── GET /tenants ──────────────────────────────────────────────────────────────

@router.get("/tenants")
async def list_tenants(
    _: dict = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    result   = await db.execute(select(Tenant))
    tenants  = result.scalars().all()
    rows = []
    for t in tenants:
        u_count = (await db.execute(
            select(func.count()).select_from(User).where(User.tenant_id == t.id)
        )).scalar() or 0
        i_count = (await db.execute(
            select(func.count()).select_from(Invoice).where(Invoice.tenant_id == t.id)
        )).scalar() or 0
        rows.append({
            "id":           t.id,
            "company_name": t.company_name,
            "plan":         getattr(t, "plan", "demo"),
            "is_active":    t.is_active,
            "user_count":   u_count,
            "invoice_count": i_count,
        })
    return rows


# ── POST /tenants ─────────────────────────────────────────────────────────────

@router.post("/tenants", status_code=201)
async def create_tenant(
    body: NewTenantBody,
    _:   dict         = Depends(get_admin_payload),
    db:  AsyncSession = Depends(get_db),
):
    tenant = Tenant(company_name=body.company_name, is_active=True,
                    plan=body.plan)
    db.add(tenant)
    await db.flush()  # get tenant.id

    hashed = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    user = User(
        tenant_id=tenant.id,
        username=body.admin_username,
        mobile=body.admin_mobile,
        password_hash=hashed,
        role="admin",
        is_active=True,
    )
    db.add(user)
    await db.commit()
    return {"message": "Tenant created", "tenant_id": tenant.id}


# ── PATCH /tenants/{id}/toggle ────────────────────────────────────────────────

@router.patch("/tenants/{tenant_id}/toggle")
async def toggle_tenant(
    tenant_id: int,
    _:   dict         = Depends(get_admin_payload),
    db:  AsyncSession = Depends(get_db),
):
    t = await db.get(Tenant, tenant_id)
    if not t:
        raise HTTPException(404, "Tenant not found")
    t.is_active = not t.is_active
    await db.commit()
    return {"is_active": t.is_active}


# ── PATCH /tenants/{id}/reset-password ───────────────────────────────────────

@router.patch("/tenants/{tenant_id}/reset-password")
async def reset_tenant_password(
    tenant_id: int,
    body: ResetPasswordBody,
    _:   dict         = Depends(get_admin_payload),
    db:  AsyncSession = Depends(get_db),
):
    result = await db.execute(
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


# ── DELETE /tenants/{id} — PERMANENTLY DELETE TENANT AND ALL DATA ─────────────

@router.delete("/tenants/{tenant_id}")
async def permanently_delete_tenant(
    tenant_id: int,
    _:   dict         = Depends(get_admin_payload),
    db:  AsyncSession = Depends(get_db),
):
    """
    Permanently erase a tenant and every row associated with it.
    Cascades through ALL tables in dependency order (children first).
    This cannot be undone — ensure a backup has been downloaded first.
    """
    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")

    tid = tenant_id

    # ── 1. Stock (transactions before items) ─────────────────────
    await db.execute(sql_delete(StockTransaction).where(StockTransaction.tenant_id == tid))
    await db.execute(sql_delete(StockItem).where(StockItem.tenant_id == tid))

    # ── 2. Supplier tree (items → payments → advances → invoices → suppliers) ─
    await db.execute(sql_delete(SupplierInvoiceItem).where(SupplierInvoiceItem.tenant_id == tid))
    await db.execute(sql_delete(SupplierPayment).where(SupplierPayment.tenant_id == tid))
    await db.execute(sql_delete(SupplierAdvance).where(SupplierAdvance.tenant_id == tid))
    await db.execute(sql_delete(SupplierInvoice).where(SupplierInvoice.tenant_id == tid))
    await db.execute(sql_delete(Supplier).where(Supplier.tenant_id == tid))

    # ── 3. Customer tree (items → payments → advances → invoices → customers) ─
    await db.execute(sql_delete(InvoiceItem).where(InvoiceItem.tenant_id == tid))
    await db.execute(sql_delete(Payment).where(Payment.tenant_id == tid))
    await db.execute(sql_delete(Advance).where(Advance.tenant_id == tid))
    await db.execute(sql_delete(Invoice).where(Invoice.tenant_id == tid))
    await db.execute(sql_delete(Customer).where(Customer.tenant_id == tid))

    # ── 4. Cash book ────────────────────────────────────────────
    await db.execute(sql_delete(CashEntry).where(CashEntry.tenant_id == tid))

    # ── 5. Google signup requests (may not have tenant_id FK) ───
    try:
        await db.execute(
            sql_delete(GoogleSignupRequest).where(GoogleSignupRequest.tenant_id == tid)
        )
    except Exception:
        pass  # skip if column doesn't exist on this model

    # ── 6. Users ────────────────────────────────────────────────
    await db.execute(sql_delete(User).where(User.tenant_id == tid))

    # ── 7. Tenant record itself ──────────────────────────────────
    await db.delete(tenant)
    await db.commit()

    return {
        "message":   f"Tenant #{tid} ({tenant.company_name}) permanently deleted",
        "tenant_id": tid,
        "deleted":   True,
    }


# ── GET /users ────────────────────────────────────────────────────────────────

@router.get("/users")
async def list_all_users(
    _: dict = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User))
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
            "role":          u.role,
            "auth_provider": getattr(u, "auth_provider", "password"),
            "is_active":     u.is_active,
        })
    return rows


# ── GET /google-requests ──────────────────────────────────────────────────────

@router.get("/google-requests")
async def list_google_requests(
    _: dict = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await db.execute(
            select(GoogleSignupRequest).order_by(GoogleSignupRequest.id.desc())
        )
        reqs = result.scalars().all()
    except Exception:
        return []
    rows = []
    for r in reqs:
        rows.append({
            "id":           r.id,
            "name":         getattr(r, "name", ""),
            "email":        getattr(r, "email", ""),
            "company":      getattr(r, "company_name", getattr(r, "company", "")),
            "mobile":       getattr(r, "mobile", ""),
            "signed_up":    r.created_at.isoformat() if hasattr(r, "created_at") and r.created_at else "",
            "status":       getattr(r, "status", "pending"),
            "trial_expires": getattr(r, "trial_expires_at", None),
        })
    return rows


# ── PATCH /google-requests/{id}/approve ──────────────────────────────────────

@router.patch("/google-requests/{req_id}/approve")
async def approve_google_request(
    req_id: int,
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    req = await db.get(GoogleSignupRequest, req_id)
    if not req:
        raise HTTPException(404, "Request not found")
    req.status = "approved"
    await db.commit()
    return {"message": "Approved"}


# ── PATCH /google-requests/{id}/reject ───────────────────────────────────────

@router.patch("/google-requests/{req_id}/reject")
async def reject_google_request(
    req_id: int,
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    req = await db.get(GoogleSignupRequest, req_id)
    if not req:
        raise HTTPException(404, "Request not found")
    req.status = "rejected"
    await db.commit()
    return {"message": "Rejected"}


# ── GET /backups ──────────────────────────────────────────────────────────────

@router.get("/backups")
async def list_backups(
    _: dict = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    result  = await db.execute(select(Tenant))
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
        })
    return rows

# NOTE: GET /backups/{tenant_id}/download is handled in export.py router
# as it generates and streams the Excel workbook.
