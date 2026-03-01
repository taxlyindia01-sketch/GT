# routers/invoices.py — Invoice CRUD + TCS + Credit Note (P0 fix)
"""
Invoice & Credit Note Router
=============================

P0 Change (Invoice Gap Fix):
  - PUT /{id}/cancel  now requires a `reason` body.
                      Creates a Credit Note (CN series) alongside the void,
                      so the audit trail is gap-free as required by GST Rule 53.
  - GET /credit-notes/list   returns all CNs for GSTR-1 Section 9B.

P1 Additions:
  - GET  /{id}/pdf         — generate and download GST invoice PDF (reportlab)
  - POST /{id}/send-email  — email the PDF to a customer email address
"""

from decimal import Decimal
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from models import (
    Invoice, InvoiceItem, Customer, Tenant, CreditNote,
    InvoiceStatus, PaymentStatus, StockItem, StockTransaction, StockTxnType,
)
from utils.auth import get_tenant_payload as get_current_user_payload
from utils.business import (
    calculate_gst, calculate_tcs, generate_invoice_no,
    is_sft_flagged, pan_is_mandatory,
)
from utils.pdf import generate_invoice_pdf
from utils.email import send_invoice_email

SFT_CASH_THRESHOLD = Decimal("200000")  # ₹2,00,000

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
    • Auto-creates customer if not found in Customer Master.
    • Enforces stock availability (rejects if stock is 0 or insufficient).
    • Cash PAN warning: if cash_receipts_fy + grand_total > ₹2L, PAN is mandatory.
    Invoice number assigned after DB flush to avoid race conditions (Bug 9 fix).
    """
    tenant_id = payload["tenant_id"]

    if not body.items:
        raise HTTPException(status_code=400, detail="Invoice must have at least one item.")

    # ── 1. Look up or auto-create customer ──────────────────────
    cust_result = await db.execute(
        select(Customer).where(
            Customer.tenant_id == tenant_id,
            Customer.mobile    == body.customer_mobile,
        )
    )
    customer = cust_result.scalar_one_or_none()

    if not customer:
        # Auto-create customer from invoice data
        customer = Customer(
            mobile=body.customer_mobile,
            tenant_id=tenant_id,
            name=body.customer_name,
            pan=body.customer_pan,
            state=body.customer_state,
            gstin=body.customer_gstin,
            cash_receipts_fy=Decimal("0"),
            sft_flagged=False,
        )
        db.add(customer)
        await db.flush()   # get customer into session before we use it below
    else:
        # Update customer name/pan if provided
        if body.customer_name and customer.name != body.customer_name:
            customer.name = body.customer_name
        if body.customer_pan and not customer.pan:
            customer.pan = body.customer_pan

    # ── 2. Calculate totals first (needed for cash threshold check) ──
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

    # ── 3. Cash threshold PAN check (including this invoice) ────
    if body.pay_mode == "Cash":
        projected_cash = customer.cash_receipts_fy + grand_total
        if projected_cash > SFT_CASH_THRESHOLD and not body.customer_pan:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"PAN is mandatory — total FY cash receipts will reach "
                    f"₹{projected_cash:,.2f} (exceeds ₹2,00,000 limit). "
                    f"Please provide customer PAN."
                ),
            )
    elif pan_is_mandatory(customer.cash_receipts_fy) and not body.customer_pan:
        raise HTTPException(
            status_code=422,
            detail="PAN is mandatory — customer's FY cash receipts exceed ₹2,00,000.",
        )

    # ── 4. Stock availability check (HARD BLOCK) ────────────────
    for item_obj in body.items:
        if item_obj.category == "Polish Charges":
            continue   # service item, no stock required
        stock_q = (
            select(StockItem)
            .where(
                StockItem.tenant_id  == tenant_id,
                StockItem.is_active  == True,
                StockItem.category   == item_obj.category,
                StockItem.qty_on_hand > 0,
            )
        )
        if item_obj.purity:
            stock_q = stock_q.where(StockItem.purity == item_obj.purity)
        stock_q = stock_q.order_by(StockItem.id)

        stock_result = await db.execute(stock_q)
        stock_item   = stock_result.scalars().first()

        if not stock_item:
            purity_str = f" ({item_obj.purity})" if item_obj.purity else ""
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Insufficient stock: {item_obj.category}{purity_str} — "
                    f"no stock available. Please add stock before creating this invoice."
                ),
            )
        if stock_item.qty_on_hand < item_obj.qty:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Insufficient stock: {item_obj.category} ({item_obj.purity or '—'}) "
                    f"— requested {item_obj.qty} {stock_item.unit.value}, "
                    f"only {float(stock_item.qty_on_hand):.3f} available."
                ),
            )

    # ── 5. Create invoice record ────────────────────────────────
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

    # ── 6. Update customer cash tracking + SFT ──────────────────
    if body.pay_mode == "Cash":
        customer.cash_receipts_fy += grand_total
        customer.sft_flagged       = is_sft_flagged(customer.cash_receipts_fy)
        if not pan_is_mandatory(customer.cash_receipts_fy - grand_total) and pan_is_mandatory(customer.cash_receipts_fy):
            customer.pan_mandatory = True
        if body.customer_pan and not customer.pan:
            customer.pan = body.customer_pan

    # ── 7. Auto-decrement stock (FIFO) ──────────────────────────
    for item_obj, item_row in zip(body.items, item_rows):
        if item_obj.category == "Polish Charges":
            continue
        stock_q = (
            select(StockItem)
            .where(
                StockItem.tenant_id  == tenant_id,
                StockItem.is_active  == True,
                StockItem.category   == item_obj.category,
                StockItem.qty_on_hand > 0,
            )
        )
        if item_obj.purity:
            stock_q = stock_q.where(StockItem.purity == item_obj.purity)
        stock_q = stock_q.order_by(StockItem.id)

        stock_result = await db.execute(stock_q)
        stock_item   = stock_result.scalars().first()

        if stock_item:
            deduct = min(item_obj.qty, stock_item.qty_on_hand)
            if deduct > 0:
                stock_item.qty_on_hand -= deduct
                db.add(StockTransaction(
                    tenant_id=tenant_id,
                    stock_item_id=stock_item.id,
                    txn_type=StockTxnType.sale,
                    qty=-deduct,
                    purchase_rate=None,
                    invoice_id=invoice.id,
                    reason=f"Auto: Invoice {invoice.invoice_no}",
                    txn_date=body.invoice_date,
                ))

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

@router.get("/{invoice_id}/items")
async def get_invoice_items(
    invoice_id: int,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    """Return line items for a specific invoice — used by PDF preview."""
    invoice = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != payload["tenant_id"]:
        raise HTTPException(status_code=404, detail="Invoice not found")

    result = await db.execute(
        select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id)
    )
    items = result.scalars().all()
    return {
        "items": [{
            "id":             it.id,
            "category":       it.category.value if hasattr(it.category, "value") else str(it.category),
            "purity":         it.purity or "",
            "description":    it.description,
            "hsn_code":       it.hsn_code,
            "qty":            float(it.qty),
            "unit":           it.unit.value if hasattr(it.unit, "value") else str(it.unit),
            "rate":           float(it.rate),
            "making_charges": float(it.making_charges),
            "amount":         float(it.amount),
        } for it in items]
    }


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

    # FIX #6: Add back stock for each invoice item on cancellation
    items_result = await db.execute(
        select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id)
    )
    for item in items_result.scalars():
        if item.category and item.category.value == "Polish Charges":
            continue
        # Find the stock item matching category + purity
        stock_q = (
            select(StockItem)
            .where(
                StockItem.tenant_id == tenant_id,
                StockItem.category  == item.category,
                StockItem.is_active == True,
            )
        )
        if item.purity:
            stock_q = stock_q.where(StockItem.purity == item.purity)
        stock_q = stock_q.order_by(StockItem.id)
        stock_result = await db.execute(stock_q)
        stock_item = stock_result.scalars().first()
        if stock_item:
            stock_item.qty_on_hand += item.qty
            db.add(StockTransaction(
                tenant_id=tenant_id,
                stock_item_id=stock_item.id,
                txn_type=StockTxnType.adjustment,
                qty=item.qty,
                txn_date=date.today(),
                reason=f"Cancellation: {invoice.invoice_no} (CN: {cn.cn_no})",
            ))

    await db.commit()

    return {
        "message":        f"Invoice {invoice.invoice_no} cancelled.",
        "credit_note_no": cn.cn_no,
        "credit_note_id": cn.id,
        "total_reversed": float(cn.total_reversed),
        "note": (
            "Credit Note issued per GST Rule 53(3). "
            "Original invoice retained in ledger (status=cancelled). "
            "Stock quantities restored. "
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


# ── Invoice PDF (P1) ─────────────────────────────────────────

class EmailInvoiceRequest(BaseModel):
    to_email: EmailStr
    customer_name: Optional[str] = None


@router.get("/{invoice_id}/pdf")
async def download_invoice_pdf(
    invoice_id: int,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    """
    Generate and stream a GST-compliant invoice PDF.
    Uses reportlab — no system fonts or wkhtmltopdf required.

    GET /api/invoices/42/pdf
    → Content-Type: application/pdf
    → Content-Disposition: attachment; filename=INV-1-0042.pdf
    """
    tenant_id = payload["tenant_id"]
    invoice   = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # Load tenant for company details
    tenant = await db.get(Tenant, tenant_id)

    # Load line items
    items_res = await db.execute(
        select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id)
        .order_by(InvoiceItem.id)
    )
    items = items_res.scalars().all()

    try:
        pdf_bytes = generate_invoice_pdf(
            invoice=invoice,
            items=items,
            company_name=tenant.company_name if tenant else "GoldTrader Pro",
            company_gstin=tenant.gstin if tenant else None,
            company_address=tenant.address if tenant else None,
            company_phone=tenant.phone if tenant else None,
            company_state=tenant.state if tenant else None,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {e}")

    filename = f"{invoice.invoice_no}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(pdf_bytes)),
        },
    )


@router.post("/{invoice_id}/send-email")
async def email_invoice(
    invoice_id: int,
    body:       EmailInvoiceRequest,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    """
    Generate invoice PDF and email it to the customer.

    POST /api/invoices/42/send-email
    Body: { "to_email": "customer@example.com" }

    Returns immediately. Email is sent asynchronously in background.
    """
    import asyncio

    tenant_id = payload["tenant_id"]
    invoice   = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Invoice not found")

    tenant = await db.get(Tenant, tenant_id)

    items_res = await db.execute(
        select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id)
        .order_by(InvoiceItem.id)
    )
    items = items_res.scalars().all()

    try:
        pdf_bytes = generate_invoice_pdf(
            invoice=invoice,
            items=items,
            company_name=tenant.company_name if tenant else "GoldTrader Pro",
            company_gstin=tenant.gstin if tenant else None,
            company_address=tenant.address if tenant else None,
            company_phone=tenant.phone if tenant else None,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {e}")

    cust_name = body.customer_name or invoice.customer_name
    company   = tenant.company_name if tenant else "GoldTrader Pro"

    # Fire-and-forget email
    asyncio.create_task(
        send_invoice_email(
            to_email=body.to_email,
            customer_name=cust_name,
            invoice_no=invoice.invoice_no,
            company_name=company,
            grand_total=float(invoice.grand_total),
            pdf_bytes=pdf_bytes,
        )
    )

    return {
        "message":    "Invoice email queued successfully.",
        "to_email":   body.to_email,
        "invoice_no": invoice.invoice_no,
        "note":       "Delivery depends on SMTP_USER/SMTP_PASSWORD being configured in environment.",
    }


# ── Amend Invoice (P2) ────────────────────────────────────────

class InvoiceAmendRequest(BaseModel):
    """
    Amend an existing active invoice.
    Only notes, pay_mode, customer_pan, customer_gstin, and invoice_date are editable.
    To change items or amounts: cancel the invoice and create a new one.
    """
    invoice_date:   Optional[date] = None
    customer_pan:   Optional[str]  = None
    customer_gstin: Optional[str]  = None
    pay_mode:       Optional[str]  = None
    notes:          Optional[str]  = None
    amendment_note: str            = Field(..., min_length=5, max_length=500,
                                          description="Reason for amendment (stored for audit trail)")


@router.put("/{invoice_id}/amend", response_model=InvoiceOut)
async def amend_invoice(
    invoice_id: int,
    body:       InvoiceAmendRequest,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    """
    Amend a non-cancelled invoice.
    Allowed changes: date, PAN, GSTIN, pay_mode, notes.
    Amounts/items cannot be changed — cancel and re-create for those.
    Amendment note is appended to the invoice notes for audit trail.
    """
    tenant_id = payload["tenant_id"]
    invoice   = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status == InvoiceStatus.cancelled:
        raise HTTPException(status_code=400, detail="Cannot amend a cancelled invoice. Create a new invoice instead.")

    # Apply allowed changes
    if body.invoice_date is not None:
        invoice.invoice_date = body.invoice_date
    if body.customer_pan is not None:
        invoice.customer_pan = body.customer_pan.upper() if body.customer_pan else None
        # Also update customer master
        cust_result = await db.execute(
            select(Customer).where(
                Customer.tenant_id == tenant_id,
                Customer.mobile    == invoice.customer_mobile,
            )
        )
        customer = cust_result.scalar_one_or_none()
        if customer and body.customer_pan:
            customer.pan = body.customer_pan.upper()
    if body.customer_gstin is not None:
        invoice.customer_gstin = body.customer_gstin or None
    if body.pay_mode is not None:
        invoice.pay_mode = body.pay_mode
    if body.notes is not None:
        invoice.notes = body.notes

    # Append amendment note to notes for audit trail
    audit_stamp = f"[AMENDED: {body.amendment_note}]"
    if invoice.notes:
        invoice.notes = f"{invoice.notes} | {audit_stamp}"
    else:
        invoice.notes = audit_stamp

    await db.commit()
    await db.refresh(invoice)
    return invoice


# ── Stock availability check (pre-flight for frontend) ───────

@router.post("/check-stock")
async def check_stock_availability(
    body:    InvoiceCreate,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """
    Pre-flight stock availability check without creating the invoice.
    Returns list of items with their available quantity.
    """
    tenant_id = payload["tenant_id"]
    results   = []

    for item in body.items:
        if item.category == "Polish Charges":
            results.append({"category": item.category, "purity": item.purity, "available": None, "sufficient": True})
            continue

        stock_q = (
            select(StockItem)
            .where(
                StockItem.tenant_id  == tenant_id,
                StockItem.is_active  == True,
                StockItem.category   == item.category,
                StockItem.qty_on_hand > 0,
            )
        )
        if item.purity:
            stock_q = stock_q.where(StockItem.purity == item.purity)

        stock_result = await db.execute(stock_q)
        stocks = stock_result.scalars().all()
        total_available = sum(s.qty_on_hand for s in stocks)

        results.append({
            "category":  item.category,
            "purity":    item.purity,
            "requested": float(item.qty),
            "available": float(total_available),
            "sufficient": total_available >= item.qty,
        })

    return {"items": results, "all_sufficient": all(r["sufficient"] for r in results)}
