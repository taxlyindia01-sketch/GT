# routers/invoices.py — Invoice CRUD + TCS auto-calculation

from decimal import Decimal
from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from models import Invoice, InvoiceItem, Customer, InvoiceStatus, PaymentStatus
from utils.auth import get_current_user_payload
from utils.business import (
    calculate_gst, calculate_tcs, generate_invoice_no,
    is_sft_flagged, pan_is_mandatory
)

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────

class InvoiceItemIn(BaseModel):
    category:       str
    purity:         Optional[str] = None
    description:    str
    hsn_code:       str = "7113"
    qty:            Decimal
    unit:           str
    rate:           Decimal
    making_charges: Decimal = Decimal("0")

class InvoiceCreate(BaseModel):
    invoice_date:    date
    customer_mobile: str = Field(..., pattern=r"^\d{10}$")
    customer_name:   str
    customer_pan:    Optional[str] = None
    customer_state:  str
    customer_gstin:  Optional[str] = None
    pay_mode:        str
    gst_type:        str = "CGST+SGST"
    gst_rate:        Decimal = Decimal("3")
    items:           list[InvoiceItemIn]
    notes:           Optional[str] = None

class InvoiceOut(BaseModel):
    id:             int
    invoice_no:     str
    invoice_date:   date
    customer_mobile:str
    customer_name:  str
    customer_pan:   Optional[str]
    pay_mode:       str
    subtotal:       Decimal
    cgst:           Decimal
    sgst:           Decimal
    igst:           Decimal
    tcs_applicable: bool
    tcs_amount:     Decimal
    grand_total:    Decimal
    outstanding:    Decimal
    payment_status: str
    status:         str

    class Config:
        from_attributes = True


# ── Create Invoice ────────────────────────────────────────────

@router.post("/", response_model=InvoiceOut, status_code=201)
async def create_invoice(
    body:    InvoiceCreate,
    payload: dict = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """
    Create a new invoice with auto-calculated TCS and GST.

    Business rules applied automatically:
    - TCS 1% if Cash payment > ₹5,00,000  (Section 206C(1F))
    - PAN warning if customer cash FY total > ₹2,00,000
    - Invoice number auto-generated per tenant (INV-{tenant_id}-{seq})
    """
    tenant_id = payload["tenant_id"]

    # Validate items
    if not body.items:
        raise HTTPException(status_code=400, detail="Invoice must have at least one item.")

    # Check PAN requirement (SFT: customer cash FY > ₹2,00,000)
    cust_result = await db.execute(
        select(Customer).where(
            Customer.tenant_id == tenant_id,
            Customer.mobile    == body.customer_mobile,
        )
    )
    customer = cust_result.scalar_one_or_none()
    if customer and pan_is_mandatory(customer.cash_receipts_fy) and not body.customer_pan:
        raise HTTPException(
            status_code=422,
            detail="PAN is mandatory — customer's cash receipts in this FY exceed ₹2,00,000.",
        )

    # Calculate line item totals
    subtotal = Decimal("0")
    item_rows = []
    for item in body.items:
        amount = (item.qty * item.rate + item.making_charges).quantize(Decimal("0.01"))
        subtotal += amount
        item_rows.append(InvoiceItem(
            tenant_id=tenant_id,
            category=item.category,
            purity=item.purity,
            description=item.description,
            hsn_code=item.hsn_code,
            qty=item.qty,
            unit=item.unit,
            rate=item.rate,
            making_charges=item.making_charges,
            amount=amount,
        ))

    # GST
    gst = calculate_gst(subtotal, body.gst_rate, body.gst_type)

    # TCS removed — always zero
    tcs = calculate_tcs(subtotal, body.pay_mode)

    grand_total = subtotal + gst["total_gst"] + tcs["tcs_amount"]

    # ── PAN MANDATORY if invoice value > ₹2,00,000 (any payment mode) ──────
    # Section: Income Tax Act requirement — PAN mandatory on transactions > ₹2L
    PAN_THRESHOLD = Decimal("200000")
    if grand_total > PAN_THRESHOLD and not body.customer_pan:
        raise HTTPException(
            status_code=422,
            detail=(
                f"PAN is mandatory — invoice value ₹{grand_total:,.0f} exceeds ₹2,00,000. "
                "Update PAN in Customer Master or enter it in this invoice before proceeding."
            ),
        )
    # Also validate PAN format if provided
    import re
    if body.customer_pan and not re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]$', body.customer_pan):
        raise HTTPException(status_code=422, detail="PAN format invalid. Expected format: ABCDE1234F")

    # Auto-generate invoice number
    count_result = await db.execute(
        select(func.count()).where(Invoice.tenant_id == tenant_id)
    )
    seq = (count_result.scalar() or 0) + 1
    invoice_no = generate_invoice_no(tenant_id, seq)

    invoice = Invoice(
        tenant_id=tenant_id,
        invoice_no=invoice_no,
        invoice_date=body.invoice_date,
        customer_mobile=body.customer_mobile,
        customer_name=body.customer_name,
        customer_pan=body.customer_pan,
        customer_state=body.customer_state,
        customer_gstin=body.customer_gstin,
        pay_mode=body.pay_mode,
        gst_type=body.gst_type,
        gst_rate=body.gst_rate,
        subtotal=subtotal,
        cgst=gst["cgst"],
        sgst=gst["sgst"],
        igst=gst["igst"],
        tcs_applicable=tcs["tcs_applicable"],
        tcs_base=tcs["tcs_base"],
        tcs_amount=tcs["tcs_amount"],
        grand_total=grand_total,
        outstanding=grand_total,
        status=InvoiceStatus.active,
        payment_status=PaymentStatus.unpaid,
        notes=body.notes,
        created_by=int(payload["sub"]),
    )
    db.add(invoice)
    await db.flush()

    for item in item_rows:
        item.invoice_id = invoice.id
        db.add(item)

    await db.commit()
    await db.refresh(invoice)
    return invoice


# ── List Invoices ─────────────────────────────────────────────

@router.get("/", response_model=list[InvoiceOut])
async def list_invoices(
    from_date: Optional[date]  = Query(None),
    to_date:   Optional[date]  = Query(None),
    mobile:    Optional[str]   = Query(None),
    status:    Optional[str]   = Query(None),
    payload:   dict            = Depends(get_current_user_payload),
    db:        AsyncSession    = Depends(get_db),
):
    """List invoices with optional date range and customer mobile filter."""
    tenant_id = payload["tenant_id"]
    q = select(Invoice).where(
        Invoice.tenant_id == tenant_id,
        Invoice.status    != InvoiceStatus.cancelled,
    ).order_by(Invoice.invoice_date.desc())

    if from_date:
        q = q.where(Invoice.invoice_date >= from_date)
    if to_date:
        q = q.where(Invoice.invoice_date <= to_date)
    if mobile:
        q = q.where(Invoice.customer_mobile == mobile)
    if status:
        q = q.where(Invoice.payment_status == status)

    result = await db.execute(q)
    return result.scalars().all()


# ── Get Invoice ───────────────────────────────────────────────

@router.get("/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(
    invoice_id: int,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    invoice = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != payload["tenant_id"]:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


# ── Cancel Invoice ────────────────────────────────────────────

@router.put("/{invoice_id}/cancel")
async def cancel_invoice(
    invoice_id: int,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    invoice = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != payload["tenant_id"]:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status == InvoiceStatus.cancelled:
        raise HTTPException(status_code=400, detail="Already cancelled")
    invoice.status = InvoiceStatus.cancelled
    await db.commit()
    return {"message": f"Invoice {invoice.invoice_no} cancelled"}
