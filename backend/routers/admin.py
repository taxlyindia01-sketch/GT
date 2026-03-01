# routers/admin.py — Taxly super-admin endpoints
# Admin credentials: username=Taxly, password=@Gsf025@

import re
import io
from datetime import datetime, timezone, date
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from models import (
    Tenant, User, Invoice, InvoiceItem, Customer, Payment, CashEntry,
    Advance, StockItem, ApprovalStatus
)
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
        if not re.search(r"[!@#$%^&*()_+\-=\[\]{};':\"|,.<>/?`~]", v):
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


@router.delete("/tenants/{tenant_id}")
async def delete_tenant(
    tenant_id: int,
    _:   dict         = Depends(require_taxly_admin),
    db:  AsyncSession = Depends(get_db),
):
    """
    FIX #6: Permanently delete a tenant and ALL related data.
    IRREVERSIBLE. Cascades to users, invoices, customers, payments, cash, advances, stock.
    """
    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    company_name = tenant.company_name
    await db.delete(tenant)   # CASCADE deletes all related rows via FK constraints
    await db.commit()
    return {
        "message": f"Tenant '{company_name}' (ID {tenant_id}) permanently deleted.",
        "tenant_id": tenant_id,
    }


@router.patch("/tenants/{tenant_id}/reset-password")
async def reset_tenant_password(
    tenant_id: int,
    body:      ResetPasswordRequest,
    _:   dict         = Depends(require_taxly_admin),
    db:  AsyncSession = Depends(get_db),
):
    """Reset the admin password for a tenant."""
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

    await db.commit()
    return rows


@router.patch("/google-requests/{user_id}/approve")
async def approve_google_user(
    user_id:   int,
    body:      dict = {},
    _:   dict         = Depends(require_taxly_admin),
    db:  AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if not user or user.auth_provider.value != "google":
        raise HTTPException(status_code=404, detail="Google user not found")

    user.approval_status  = ApprovalStatus.approved
    user.trial_expires_at = None
    user.role             = body.get("role", user.role.value)
    await db.commit()

    if user.email:
        import asyncio
        asyncio.create_task(
            send_approval_email(user.email, user.username or "there", user.company_name or "your business")
        )

    return {"message": f"{user.username} approved successfully.", "email_sent": bool(user.email)}


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
            "tenant_id":     t.id,
            "company_name":  t.company_name,
            "is_active":     t.is_active,
            "invoice_count": inv_count,
            "customer_count":cust_count,
        })
    return rows


@router.get("/backups/{tenant_id}/download")
async def download_tenant_backup(
    tenant_id: int,
    _:   dict         = Depends(require_taxly_admin),
    db:  AsyncSession = Depends(get_db),
):
    """
    FIX #5: Download complete Excel backup for a tenant.
    Sheets: Dashboard, Invoices, Invoice Items, Customers, Payments, Cash Book, Advances, Stock.
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    wb = openpyxl.Workbook()

    GOLD = "C8900A"; WHITE = "FFFFFF"; DARK = "0A1628"
    HDR_FONT  = Font(bold=True, color=WHITE, name="Arial", size=10)
    HDR_FILL  = PatternFill("solid", fgColor=GOLD)
    thin = Side(style="thin", color="DDDDDD")
    BORDER = Border(left=thin, right=thin, top=thin, bottom=thin)

    def hdr(ws, cols, row=1):
        for ci, c in enumerate(cols, 1):
            cell = ws.cell(row=row, column=ci, value=c)
            cell.font = HDR_FONT; cell.fill = HDR_FILL
            cell.alignment = Alignment(horizontal="center"); cell.border = BORDER

    def dc(ws, row, col, val, bold=False, fmt=None):
        c = ws.cell(row=row, column=col, value=val)
        c.font = Font(bold=bold, name="Arial", size=9)
        c.border = BORDER
        if fmt: c.number_format = fmt
        return c

    def autofit(ws, mn=8, mx=40):
        for col in ws.columns:
            best = mn
            for cell in col:
                if cell.value:
                    best = min(max(best, len(str(cell.value)) + 2), mx)
            ws.column_dimensions[get_column_letter(col[0].column)].width = best

    # ── Sheet 1: Dashboard / Summary ─────────────────────────
    ws = wb.active; ws.title = "Dashboard"
    ws.sheet_view.showGridLines = False
    ws.merge_cells("A1:D1")
    ws["A1"].value = f"GoldTrader Pro — Backup: {tenant.company_name}"
    ws["A1"].font  = Font(bold=True, size=14, color=GOLD, name="Arial")
    ws["A2"].value = f"Generated: {datetime.now().strftime('%d %b %Y %H:%M')} | By: GoldTrader Pro CRM by Taxly India"
    ws["A2"].font  = Font(italic=True, size=9, color="666666", name="Arial")

    ws.append([])
    fields = [
        ("Company", tenant.company_name), ("GSTIN", tenant.gstin or ""),
        ("PAN", getattr(tenant,"pan","") or ""), ("Phone", tenant.phone or ""),
        ("Email", tenant.email or ""), ("Address", tenant.address or ""),
        ("State", tenant.state or ""), ("UPI ID", getattr(tenant,"upi_id","") or ""),
        ("Bank", getattr(tenant,"bank_name","") or ""), ("Account", getattr(tenant,"bank_account","") or ""),
        ("IFSC", getattr(tenant,"bank_ifsc","") or ""), ("Plan", tenant.plan.value),
    ]
    for r, (label, val) in enumerate(fields, 4):
        ws.cell(r, 1, label).font = Font(bold=True, name="Arial", size=10)
        ws.cell(r, 2, val).font   = Font(name="Arial", size=10)

    # KPI summary
    inv_total = await db.scalar(select(func.count()).where(Invoice.tenant_id == tenant_id)) or 0
    cust_total = await db.scalar(select(func.count()).where(Customer.tenant_id == tenant_id)) or 0
    inv_res = await db.execute(select(Invoice).where(Invoice.tenant_id == tenant_id, Invoice.status == "active"))
    all_invs = inv_res.scalars().all()
    total_sales = sum(float(i.grand_total) for i in all_invs)
    total_out   = sum(float(i.outstanding) for i in all_invs)
    kpis = [("Total Invoices", inv_total), ("Total Customers", cust_total),
            ("Total Sales (active)", f"Rs. {total_sales:,.2f}"), ("Total Outstanding", f"Rs. {total_out:,.2f}")]
    for r, (label, val) in enumerate(kpis, 18):
        ws.cell(r, 1, label).font = Font(bold=True, name="Arial", size=10)
        ws.cell(r, 2, str(val)).font = Font(name="Arial", size=10)
    ws.column_dimensions["A"].width = 22; ws.column_dimensions["B"].width = 40

    # ── Sheet 2: Invoices ─────────────────────────────────────
    ws2 = wb.create_sheet("Invoices")
    ws2.sheet_view.showGridLines = False
    cols2 = ["Invoice No","Date","Customer","Mobile","PAN","GSTIN","State","Pay Mode",
             "Subtotal","CGST","SGST","IGST","TCS","Grand Total","Paid","Outstanding","Status","Notes"]
    hdr(ws2, cols2)
    for ri, inv in enumerate(all_invs, 2):
        for ci, v in enumerate([
            inv.invoice_no, inv.invoice_date.isoformat(), inv.customer_name, inv.customer_mobile,
            inv.customer_pan or "", inv.customer_gstin or "", inv.customer_state or "",
            inv.pay_mode.value if hasattr(inv.pay_mode, 'value') else str(inv.pay_mode),
            float(inv.subtotal), float(inv.cgst), float(inv.sgst), float(inv.igst),
            float(inv.tcs_amount), float(inv.grand_total), float(inv.amount_paid),
            float(inv.outstanding), inv.payment_status.value, inv.notes or ""
        ], 1):
            dc(ws2, ri, ci, v, fmt='#,##0.00' if isinstance(v, float) else None)
    autofit(ws2)

    # ── Sheet 3: Invoice Items ────────────────────────────────
    ws3 = wb.create_sheet("Invoice Items")
    ws3.sheet_view.showGridLines = False
    inv_ids = [i.id for i in all_invs]
    all_items = []
    if inv_ids:
        ir = await db.execute(select(InvoiceItem).where(InvoiceItem.invoice_id.in_(inv_ids)))
        all_items = ir.scalars().all()
    inv_no_map = {i.id: i.invoice_no for i in all_invs}
    hdr(ws3, ["Invoice No","Category","Purity","Description","HSN","Qty","Unit","Rate","Making","Amount"])
    for ri, item in enumerate(all_items, 2):
        for ci, v in enumerate([
            inv_no_map.get(item.invoice_id, ""),
            item.category.value if hasattr(item.category, 'value') else str(item.category),
            item.purity or "", item.description, item.hsn_code,
            float(item.qty), item.unit.value if hasattr(item.unit, 'value') else str(item.unit),
            float(item.rate), float(item.making_charges), float(item.amount)
        ], 1):
            dc(ws3, ri, ci, v, fmt='#,##0.00' if isinstance(v, float) and ci > 5 else None)
    autofit(ws3)

    # ── Sheet 4: Customers ────────────────────────────────────
    ws4 = wb.create_sheet("Customers")
    ws4.sheet_view.showGridLines = False
    cust_res = await db.execute(select(Customer).where(Customer.tenant_id == tenant_id).order_by(Customer.name))
    customers = cust_res.scalars().all()
    hdr(ws4, ["Mobile","Name","Email","State","PAN","GSTIN","Cash Receipts FY","SFT Flagged","PAN Mandatory"])
    for ri, c in enumerate(customers, 2):
        for ci, v in enumerate([
            c.mobile, c.name, c.email or "", c.state or "", c.pan or "",
            c.gstin or "", float(c.cash_receipts_fy), c.sft_flagged, c.pan_mandatory
        ], 1):
            dc(ws4, ri, ci, v, fmt='#,##0.00' if isinstance(v, float) else None)
    autofit(ws4)

    # ── Sheet 5: Payments ─────────────────────────────────────
    ws5 = wb.create_sheet("Payments")
    ws5.sheet_view.showGridLines = False
    pmt_res = await db.execute(select(Payment).where(Payment.tenant_id == tenant_id).order_by(Payment.payment_date.desc()))
    payments = pmt_res.scalars().all()
    hdr(ws5, ["Date","Invoice ID","Customer Mobile","Amount","Pay Mode","Reference","Notes"])
    for ri, p in enumerate(payments, 2):
        for ci, v in enumerate([
            p.payment_date.isoformat(), p.invoice_id, p.customer_mobile,
            float(p.amount), p.pay_mode.value if hasattr(p.pay_mode, 'value') else str(p.pay_mode),
            p.reference_no or "", p.notes or ""
        ], 1):
            dc(ws5, ri, ci, v, fmt='#,##0.00' if isinstance(v, float) else None)
    autofit(ws5)

    # ── Sheet 6: Cash Book ────────────────────────────────────
    ws6 = wb.create_sheet("Cash Book")
    ws6.sheet_view.showGridLines = False
    cash_res = await db.execute(select(CashEntry).where(CashEntry.tenant_id == tenant_id).order_by(CashEntry.entry_date.asc()))
    entries = cash_res.scalars().all()
    hdr(ws6, ["Date","Type","Description","Amount","Bank Reference","Running Balance"])
    for ri, e in enumerate(entries, 2):
        rb = float(e.running_balance) if e.running_balance is not None else ""
        for ci, v in enumerate([
            e.entry_date.isoformat(), e.entry_type.value, e.description,
            float(e.amount), e.bank_reference or "", rb
        ], 1):
            dc(ws6, ri, ci, v, fmt='#,##0.00' if ci in (4,6) and isinstance(v, float) else None)
    autofit(ws6)

    # ── Sheet 7: Advances ─────────────────────────────────────
    ws7 = wb.create_sheet("Advances")
    ws7.sheet_view.showGridLines = False
    adv_res = await db.execute(select(Advance).where(Advance.tenant_id == tenant_id).order_by(Advance.advance_date.desc()))
    advances = adv_res.scalars().all()
    hdr(ws7, ["Date","Customer Mobile","Amount","Pay Mode","Remaining","Notes"])
    for ri, a in enumerate(advances, 2):
        for ci, v in enumerate([
            a.advance_date.isoformat(), a.customer_mobile, float(a.amount),
            a.pay_mode.value if hasattr(a.pay_mode, 'value') else str(a.pay_mode),
            float(a.remaining), a.notes or ""
        ], 1):
            dc(ws7, ri, ci, v, fmt='#,##0.00' if isinstance(v, float) else None)
    autofit(ws7)

    # ── Sheet 8: Stock ────────────────────────────────────────
    ws8 = wb.create_sheet("Stock")
    ws8.sheet_view.showGridLines = False
    stk_res = await db.execute(select(StockItem).where(StockItem.tenant_id == tenant_id, StockItem.is_active == True))
    stock = stk_res.scalars().all()
    hdr(ws8, ["Category","Purity","Description","Unit","Qty on Hand"])
    for ri, s in enumerate(stock, 2):
        for ci, v in enumerate([
            s.category.value if hasattr(s.category, 'value') else str(s.category),
            s.purity or "", s.description,
            s.unit.value if hasattr(s.unit, 'value') else str(s.unit),
            float(s.qty_on_hand)
        ], 1):
            dc(ws8, ri, ci, v, fmt='#,##0.000' if isinstance(v, float) else None)
    autofit(ws8)

    # ── Stream ────────────────────────────────────────────────
    buf = io.BytesIO()
    wb.save(buf); buf.seek(0)
    fname = f"backup_{tenant.company_name.replace(' ','_')}_{date.today().isoformat()}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
