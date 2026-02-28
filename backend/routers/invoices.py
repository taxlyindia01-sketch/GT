# routers/invoices.py — Invoice CRUD + TCS + Credit Note (P0 fix)
"""
Invoice & Credit Note Router
=============================

P0 Change (Invoice Gap Fix):
  - PUT /{id}/cancel  now requires a `reason` body.
                      Creates a Credit Note (CN series) alongside the void,
                      so the audit trail is gap-free as required by GST Rule 53.
  - GET /credit-notes/list   returns all CNs for GSTR-1 Section 9B.
"""

from decimal import Decimal
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from models import (
    Invoice, InvoiceItem, Customer, CreditNote,
    InvoiceStatus, PaymentStatus,
)
from utils.auth import get_tenant_payload as get_current_user_payload
from utils.business import (
    calculate_gst, calculate_tcs, generate_invoice_no,
    is_sft_flagged, pan_is_mandatory,
)

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────

class InvoiceItemIn(BaseModel):
    category:       str
    purity:         Optional[str]  = None
    description:    str
    hsn_code:       str            = "7113"
    qty:            Decimal
    unit:           str
    rate:           Decimal
    making_charges: Decimal        = Decimal("0")


class InvoiceCreate(BaseModel):
    invoice_date:    date
    customer_mobile: str    = Field(..., pattern=r"^\d{10}$")
    customer_name:   str
    customer_pan:    Optional[str]  = None
    customer_state:  str
    customer_gstin:  Optional[str]  = None
    pay_mode:        str
    gst_type:        str            = "CGST+SGST"
    gst_rate:        Decimal        = Decimal("3")
    items:           list[InvoiceItemIn]
    notes:           Optional[str]  = None


class InvoiceOut(BaseModel):
    id:              int
    invoice_no:      str
    invoice_date:    date
    customer_mobile: str
    customer_name:   str
    customer_pan:    Optional[str]
    customer_state:  Optional[str]
    pay_mode:        str
    gst_type:        str
    gst_rate:        Decimal
    subtotal:        Decimal
    cgst:            Decimal
    sgst:            Decimal
    igst:            Decimal
    tcs_applicable:  bool
    tcs_amount:      Decimal
    grand_total:     Decimal
    amount_paid:     Decimal
    outstanding:     Decimal
    payment_status:  str
    status:          str
    notes:           Optional[str]

    class Config:
        from_attributes = True


class CancelRequest(BaseModel):
    """Reason is mandatory — stored on the Credit Note for GST audit."""
    reason: str = Field(..., min_length=5, max_length=500)


class CreditNoteOut(BaseModel):
    id:                  int
    cn_no:               str
    cn_date:             date
    original_invoice_no: str
    customer_name:       str
    reason:              str
    total_reversed:      Decimal

    class Config:
        from_attributes = True


# ── Create Invoice ────────────────────────────────────────────

@router.post("/", response_model=InvoiceOut, status_code=201)
async def create_invoice(
    body:    InvoiceCreate,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """
    Create a GST invoice. Auto-applies TCS, GST, and PAN enforcement.
    Invoice number assigned after DB flush to avoid race conditions (Bug 9 fix).
    """
    tenant_id = payload["tenant_id"]

    if not body.items:
        raise HTTPException(status_code=400, detail="Invoice must have at least one item.")

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
            detail="PAN is mandatory — customer's FY cash receipts exceed ₹2,00,000.",
        )

    subtotal  = Decimal("0")
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

    gst         = calculate_gst(subtotal, body.gst_rate, body.gst_type)
    tcs         = calculate_tcs(subtotal, body.pay_mode)
    grand_total = subtotal + gst["total_gst"] + tcs["tcs_amount"]

    invoice = Invoice(
        tenant_id=tenant_id,
        invoice_no="PENDING",
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
    await db.flush()                                           # PK assigned now

    invoice.invoice_no = f"INV-{tenant_id}-{str(invoice.id).zfill(4)}"   # Bug-9 fix

    for item in item_rows:
        item.invoice_id = invoice.id
        db.add(item)

    if customer and body.pay_mode == "Cash":
        customer.cash_receipts_fy += grand_total
        customer.sft_flagged       = is_sft_flagged(customer.cash_receipts_fy)
        if not customer.pan_mandatory and pan_is_mandatory(customer.cash_receipts_fy):
            customer.pan_mandatory = True

    await db.commit()
    await db.refresh(invoice)
    return invoice


# ── List Invoices ─────────────────────────────────────────────

@router.get("/", response_model=list[InvoiceOut])
async def list_invoices(
    from_date:         Optional[date] = Query(None),
    to_date:           Optional[date] = Query(None),
    mobile:            Optional[str]  = Query(None),
    status:            Optional[str]  = Query(None),
    include_cancelled: bool           = Query(False),
    payload:           dict           = Depends(get_current_user_payload),
    db:                AsyncSession   = Depends(get_db),
):
    tenant_id = payload["tenant_id"]
    q = select(Invoice).where(Invoice.tenant_id == tenant_id)
    if not include_cancelled:
        q = q.where(Invoice.status != InvoiceStatus.cancelled)
    if from_date: q = q.where(Invoice.invoice_date >= from_date)
    if to_date:   q = q.where(Invoice.invoice_date <= to_date)
    if mobile:    q = q.where(Invoice.customer_mobile == mobile)
    if status:    q = q.where(Invoice.payment_status == status)
    q = q.order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
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


# ── Cancel Invoice → Credit Note ─────────────────────────────

@router.put("/{invoice_id}/cancel")
async def cancel_invoice(
    invoice_id: int,
    body:       CancelRequest,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    """
    P0 Fix — Gap-free cancellation via Credit Note.

    Steps:
      1. Invoice → status=cancelled  (preserved in ledger, never deleted)
      2. Credit Note (CN-{tenant_id}-NNNN) created with identical financials
         — this is the GST Rule 53 reversal document that explains the gap
      3. Customer cash_receipts_fy decremented by cash collected (Bug-12 fix)
      4. sft_flagged and pan_mandatory re-evaluated after decrement

    The original invoice NUMBER is never reused or deleted.  The CN document
    satisfies GST auditors asking "where is INV-1-0005?".
    """
    tenant_id = payload["tenant_id"]

    invoice = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status == InvoiceStatus.cancelled:
        raise HTTPException(status_code=400, detail="Invoice already cancelled")

    # 1. Void invoice
    invoice.status = InvoiceStatus.cancelled

    # 2. Credit Note
    cn = CreditNote(
        tenant_id=tenant_id,
        cn_no="CN-PENDING",
        cn_date=date.today(),
        original_invoice_id=invoice.id,
        original_invoice_no=invoice.invoice_no,
        customer_mobile=invoice.customer_mobile,
        customer_name=invoice.customer_name,
        customer_pan=invoice.customer_pan,
        reason=body.reason,
        subtotal=invoice.subtotal,
        cgst=invoice.cgst,
        sgst=invoice.sgst,
        igst=invoice.igst,
        tcs_reversed=invoice.tcs_amount,
        total_reversed=invoice.grand_total,
        cash_fy_reversed=Decimal("0"),
        created_by=int(payload["sub"]),
    )
    db.add(cn)
    await db.flush()
    cn.cn_no = f"CN-{tenant_id}-{str(cn.id).zfill(4)}"

    # 3. Reverse FY cash tracking (Bug-12 fix extended)
    if invoice.pay_mode == "Cash":
        cust_result = await db.execute(
            select(Customer).where(
                Customer.tenant_id == tenant_id,
                Customer.mobile    == invoice.customer_mobile,
            )
        )
        customer = cust_result.scalar_one_or_none()
        if customer:
            cash_collected = min(invoice.amount_paid, invoice.grand_total)
            decrement      = min(cash_collected, customer.cash_receipts_fy)
            customer.cash_receipts_fy -= decrement
            customer.sft_flagged       = is_sft_flagged(customer.cash_receipts_fy)
            cn.cash_fy_reversed        = decrement
            if customer.pan_mandatory and not pan_is_mandatory(customer.cash_receipts_fy):
                customer.pan_mandatory = False

    await db.commit()

    return {
        "message":        f"Invoice {invoice.invoice_no} cancelled.",
        "credit_note_no": cn.cn_no,
        "credit_note_id": cn.id,
        "total_reversed": float(cn.total_reversed),
        "note": (
            "Credit Note issued per GST Rule 53(3). "
            "Original invoice retained in ledger (status=cancelled). "
            "Report this CN under GSTR-1 → Section 9B."
        ),
    }


# ── Credit Note endpoints ─────────────────────────────────────

@router.get("/credit-notes/list", response_model=list[CreditNoteOut])
async def list_credit_notes(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """List credit notes — use for GSTR-1 Section 9B filing."""
    q = select(CreditNote).where(CreditNote.tenant_id == payload["tenant_id"])
    if from_date: q = q.where(CreditNote.cn_date >= from_date)
    if to_date:   q = q.where(CreditNote.cn_date <= to_date)
    q = q.order_by(CreditNote.cn_date.desc())
    result = await db.execute(q)
    return result.scalars().all()


@router.get("/credit-notes/{cn_id}", response_model=CreditNoteOut)
async def get_credit_note(
    cn_id:   int,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    cn = await db.get(CreditNote, cn_id)
    if not cn or cn.tenant_id != payload["tenant_id"]:
        raise HTTPException(status_code=404, detail="Credit note not found")
    return cn
