# routers/invoices.py
# Changes vs original:
#  Issue 1  — Auto-save customer to master when creating invoice (upsert)
#  Issue 2  — GET /{id}/items endpoint so PDF preview can load line items
#  Issue 3  — PUT /{id}/amend  endpoint for non-financial field edits
#  Issue 9  — Deduct stock qty_on_hand when invoice created; restore on cancel
#  P11      — TCS removed; PAN mandatory when invoice value > ₹2,00,000
#
# ISSUE 2 FIX (sale edit) — edit_invoice stock handling:
#   Old wrong approach:
#     1. Restore old stock via qty += old_qty (creates "Edit Reversal" IN lot)
#     2. Delete old sale transaction
#     3. Call _deduct_stock (creates new OUT sale transaction)
#   This left ghost IN adjustment rows in the stock movement register.
#
#   Correct approach:
#     1. Find the existing sale StockTransaction for this invoice
#     2. Compute FIFO-weighted avg rate for the NEW qty being sold
#     3. UPDATE that row in-place (qty, purchase_rate)
#     4. Adjust stock.qty_on_hand by the qty delta (new - old)
#     5. NO new rows, NO reversal rows — register stays clean
#
# ISSUE 4 FIX (sale cancellation) — _restore_stock:
#   Old wrong approach: created a new adjustment IN lot at fifo_avg_rate.
#   The stored purchase_rate on the sale transaction IS the FIFO-weighted avg
#   at the time of the original sale.  On cancellation the stock is restored at
#   EXACTLY that rate so FIFO valuation is correct.
#   The restored lot is tagged lot_remaining = item.qty so FIFO can re-draw from
#   it in future sales — this is already what the original code did and is correct.
#   Confirmed no change needed; documented and kept.

import re
from decimal import Decimal
from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from models import (
    Invoice, InvoiceItem, Customer, StockItem, StockTransaction,
    InvoiceStatus, PaymentStatus, StockTxnType, CategoryEnum,
)
from utils.auth import get_current_user_payload
from utils.business import (
    calculate_gst, generate_invoice_no,
    is_sft_flagged, pan_is_mandatory,
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
    polish_charges: Decimal = Decimal("0")
    making_charges: Decimal = Decimal("0")

class InvoiceCreate(BaseModel):
    invoice_date:    date
    customer_mobile: str = Field(..., pattern=r"^\d{10}$")
    customer_name:   str
    customer_pan:    Optional[str] = None
    customer_state:  str = "Delhi"
    customer_gstin:  Optional[str] = None
    pay_mode:        str
    gst_type:        str = "CGST+SGST"
    gst_rate:        Decimal = Decimal("3")
    round_off:       Decimal = Decimal("0")
    items:           list[InvoiceItemIn]
    notes:           Optional[str] = None

class InvoiceAmend(BaseModel):
    """Fields that can be edited after invoice creation."""
    invoice_date:    Optional[date]   = None
    customer_pan:    Optional[str]    = None
    customer_gstin:  Optional[str]    = None
    pay_mode:        Optional[str]    = None
    notes:           Optional[str]    = None
    amendment_note:  Optional[str]    = None

class InvoiceOut(BaseModel):
    id:              int
    invoice_no:      str
    invoice_date:    date
    customer_mobile: str
    customer_name:   str
    customer_pan:    Optional[str]
    customer_state:  Optional[str]
    customer_gstin:  Optional[str]
    pay_mode:        str
    subtotal:        Decimal
    cgst:            Decimal
    sgst:            Decimal
    igst:            Decimal
    tcs_applicable:  bool
    tcs_amount:      Decimal
    round_off:       Optional[Decimal] = Decimal("0")
    grand_total:     Decimal
    outstanding:     Decimal
    payment_status:  str
    status:          str

    class Config:
        from_attributes = True


# ── Helpers ───────────────────────────────────────────────────

PAN_THRESHOLD = Decimal("200000")   # ₹2,00,000 — PAN mandatory above this

async def _upsert_customer(
    db: AsyncSession,
    tenant_id: int,
    mobile: str,
    name: str,
    state: str,
    pan: Optional[str],
    gstin: Optional[str],
) -> tuple["Customer", bool]:
    customer = await db.get(Customer, (mobile, tenant_id))
    created = False
    if not customer:
        customer = Customer(
            mobile=mobile, tenant_id=tenant_id, name=name, state=state,
            pan=pan or None, gstin=gstin or None,
            cash_receipts_fy=Decimal("0"), sft_flagged=False,
        )
        db.add(customer)
        created = True
    else:
        if pan and not customer.pan:
            customer.pan = pan
        if gstin and not customer.gstin:
            customer.gstin = gstin
        customer.name = name
    return customer, created


async def _find_stock(db: "AsyncSession", tenant_id: int, category, purity: "str | None"):
    from sqlalchemy import case as sa_case, or_
    filters = [
        StockItem.tenant_id == tenant_id,
        StockItem.category  == category,
        StockItem.is_active == True,
    ]
    if purity:
        filters.append(or_(StockItem.purity == purity, StockItem.purity.is_(None)))
    stmt = (
        select(StockItem)
        .where(*filters)
        .order_by(
            sa_case((StockItem.purity == purity, 0), else_=1) if purity else StockItem.id
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _check_stock_availability(
    db: AsyncSession,
    tenant_id: int,
    items: list["InvoiceItem"],
) -> None:
    for item in items:
        cat_val  = getattr(item.category, 'value', str(item.category))
        unit_val = getattr(item.unit,     'value', str(item.unit))
        if cat_val == "Polish Charges":
            continue
        stock = await _find_stock(db, tenant_id, item.category, item.purity)
        if not stock:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"No stock item found for {cat_val}"
                    f"{' / ' + item.purity if item.purity else ''}. "
                    "Please add the item to Stock Master first."
                ),
            )
        if stock.qty_on_hand < item.qty:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Insufficient stock for {cat_val}"
                    f"{' / ' + item.purity if item.purity else ''}: "
                    f"available {float(stock.qty_on_hand):.3f} {unit_val}, "
                    f"requested {float(item.qty):.3f} {unit_val}."
                ),
            )


async def _compute_fifo_avg_rate(
    db: AsyncSession,
    stock: "StockItem",
    qty: Decimal,
) -> Decimal:
    """
    Walk open FIFO lots oldest-first and return the weighted-average
    purchase_rate for `qty` units.  Used both when creating a new sale
    and when editing an existing sale (to recompute rate for new qty).
    """
    batches_result = await db.execute(
        select(StockTransaction)
        .where(
            StockTransaction.stock_item_id == stock.id,
            StockTransaction.qty > 0,
            StockTransaction.txn_type.in_([
                StockTxnType.purchase,
                StockTxnType.opening,
                StockTxnType.adjustment,
            ]),
        )
        .order_by(StockTransaction.txn_date, StockTransaction.id)
    )
    batches = batches_result.scalars().all()

    qty_to_consume = qty
    weighted_value = Decimal("0")
    for batch in batches:
        if qty_to_consume <= 0:
            break
        available = (
            batch.lot_remaining
            if batch.lot_remaining is not None
            else abs(batch.qty)
        )
        if available <= 0:
            continue
        take  = min(available, qty_to_consume)
        rate  = batch.purchase_rate or Decimal("0")
        weighted_value  += take * rate
        qty_to_consume  -= take

    if qty > 0 and weighted_value > 0:
        return (weighted_value / qty).quantize(Decimal("0.01"))
    return Decimal("0")


async def _deduct_stock(
    db: AsyncSession,
    tenant_id: int,
    created_by: int,
    invoice_id: int,
    invoice_date: date,
    items: list["InvoiceItem"],
) -> None:
    """
    Deduct sold quantities from stock on hand (FIFO basis).
    Records FIFO-weighted average purchase_rate on the sale transaction
    so cancellations can restore stock at the ORIGINAL purchase value.
    """
    for item in items:
        cat_val = getattr(item.category, 'value', str(item.category))
        if cat_val == "Polish Charges":
            continue

        stock = await _find_stock(db, tenant_id, item.category, item.purity)
        if not stock:
            continue

        fifo_avg_rate = await _compute_fifo_avg_rate(db, stock, item.qty)

        stock.qty_on_hand = stock.qty_on_hand - item.qty

        db.add(StockTransaction(
            tenant_id=tenant_id,
            stock_item_id=stock.id,
            txn_type=StockTxnType.sale,
            qty=-item.qty,
            purchase_rate=fifo_avg_rate,   # stored for use on cancellation
            txn_date=invoice_date,
            reason=f"Sale — Invoice ID {invoice_id}",
            created_by=created_by,
        ))


async def _restore_stock(
    db: AsyncSession,
    tenant_id: int,
    created_by: int,
    invoice: "Invoice",
) -> None:
    """
    Restore stock when a sale invoice is cancelled.

    ISSUE 4 (confirmed correct):
    The sale StockTransaction row stores purchase_rate = FIFO-weighted avg at
    sale time.  On cancellation we restore stock at EXACTLY that rate so FIFO
    valuation re-enters at the original purchase value, not a recalculated one.
    The restored qty re-enters as a fresh FIFO lot (lot_remaining = item.qty)
    so future sales can draw from it correctly.
    """
    items_result = await db.execute(
        select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id)
    )
    items = items_result.scalars().all()

    for item in items:
        cat_val = getattr(item.category, 'value', str(item.category))
        if cat_val == "Polish Charges":
            continue

        stock = await _find_stock(db, tenant_id, item.category, item.purity)
        if not stock:
            continue

        # Recover the FIFO avg purchase_rate stored at sale time
        sale_txn_result = await db.execute(
            select(StockTransaction)
            .where(
                StockTransaction.stock_item_id == stock.id,
                StockTransaction.txn_type      == StockTxnType.sale,
                StockTransaction.reason        == f"Sale — Invoice ID {invoice.id}",
            )
            .order_by(StockTransaction.id.desc())
            .limit(1)
        )
        sale_txn = sale_txn_result.scalar_one_or_none()
        # ISSUE 4: use the original FIFO rate stored on the sale transaction
        original_rate = (
            sale_txn.purchase_rate
            if sale_txn and sale_txn.purchase_rate
            else Decimal("0")
        )

        stock.qty_on_hand += item.qty

        # Re-enter stock at the original FIFO purchase value
        db.add(StockTransaction(
            tenant_id=tenant_id,
            stock_item_id=stock.id,
            txn_type=StockTxnType.adjustment,
            qty=item.qty,
            purchase_rate=original_rate,   # exact original FIFO rate — ISSUE 4 fix
            lot_remaining=item.qty,        # fresh FIFO lot for future sales
            txn_date=date.today(),
            reason=f"Cancelled — Invoice {invoice.invoice_no}",
            created_by=created_by,
        ))


# ── Create Invoice ────────────────────────────────────────────

@router.post("/", status_code=201)
async def create_invoice(
    body:    InvoiceCreate,
    payload: dict          = Depends(get_current_user_payload),
    db:      AsyncSession  = Depends(get_db),
):
    tenant_id = payload["tenant_id"]

    if not body.items:
        raise HTTPException(status_code=400, detail="Invoice must have at least one item.")

    if body.customer_pan and not re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]$', body.customer_pan):
        raise HTTPException(status_code=422, detail="PAN format invalid. Expected: ABCDE1234F")

    subtotal  = Decimal("0")
    item_rows = []
    for item in body.items:
        amount = (item.qty * item.rate + item.polish_charges * item.rate + item.making_charges).quantize(Decimal("0.01"))
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
            polish_charges=item.polish_charges,
            making_charges=item.making_charges,
            amount=amount,
        ))

    gst = calculate_gst(subtotal, body.gst_rate, body.gst_type)

    round_off   = body.round_off.quantize(Decimal("0.01"))
    grand_total = subtotal + gst["total_gst"] + round_off

    SEC_269ST_THRESHOLD = Decimal("200000")
    sec_269st_violation = (body.pay_mode == "Cash" and grand_total >= SEC_269ST_THRESHOLD)

    if grand_total > PAN_THRESHOLD and not body.customer_pan:
        raise HTTPException(
            status_code=422,
            detail=(
                f"PAN is mandatory — invoice value ₹{grand_total:,.0f} exceeds ₹2,00,000. "
                "Enter customer PAN before proceeding."
            ),
        )

    existing_cust_res = await db.execute(
        select(Customer).where(
            Customer.tenant_id == tenant_id,
            Customer.mobile    == body.customer_mobile,
        )
    )
    existing_cust = existing_cust_res.scalar_one_or_none()
    if (existing_cust
            and pan_is_mandatory(existing_cust.cash_receipts_fy)
            and not body.customer_pan):
        raise HTTPException(
            status_code=422,
            detail="PAN is mandatory — customer's cumulative cash receipts this FY exceed ₹2,00,000.",
        )

    count_result = await db.execute(
        select(func.count()).where(Invoice.tenant_id == tenant_id)
    )
    seq        = (count_result.scalar() or 0) + 1
    invoice_no = generate_invoice_no(tenant_id, seq)

    await _check_stock_availability(db, tenant_id, item_rows)

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
        tcs_applicable=False,
        tcs_base=Decimal("0"),
        tcs_amount=Decimal("0"),
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

    await db.flush()

    _, customer_created = await _upsert_customer(
        db, tenant_id,
        body.customer_mobile, body.customer_name, body.customer_state,
        body.customer_pan, body.customer_gstin,
    )

    await _deduct_stock(
        db, tenant_id, int(payload["sub"]),
        invoice.id, body.invoice_date, item_rows,
    )

    await db.commit()
    await db.refresh(invoice)

    return {
        "id":               invoice.id,
        "invoice_no":       invoice.invoice_no,
        "invoice_date":     invoice.invoice_date.isoformat(),
        "customer_mobile":  invoice.customer_mobile,
        "customer_name":    invoice.customer_name,
        "customer_pan":     invoice.customer_pan,
        "pay_mode":         invoice.pay_mode.value,
        "subtotal":         float(invoice.subtotal),
        "cgst":             float(invoice.cgst),
        "sgst":             float(invoice.sgst),
        "igst":             float(invoice.igst),
        "tcs_applicable":   False,
        "tcs_amount":       0.0,
        "round_off":        float(invoice.round_off or 0),
        "sec_269st_violation": sec_269st_violation,
        "grand_total":      float(invoice.grand_total),
        "outstanding":      float(invoice.outstanding),
        "payment_status":   invoice.payment_status.value,
        "status":           invoice.status.value,
        "customer_created": customer_created,
    }


# ── List Invoices ─────────────────────────────────────────────

@router.get("/", response_model=list[InvoiceOut])
async def list_invoices(
    from_date:        Optional[date] = Query(None),
    to_date:          Optional[date] = Query(None),
    mobile:           Optional[str]  = Query(None),
    status:           Optional[str]  = Query(None),
    include_cancelled: bool          = Query(False),
    payload:          dict           = Depends(get_current_user_payload),
    db:               AsyncSession   = Depends(get_db),
):
    tenant_id = payload["tenant_id"]
    q = select(Invoice).where(Invoice.tenant_id == tenant_id)

    if not include_cancelled:
        q = q.where(Invoice.status != InvoiceStatus.cancelled)

    q = q.order_by(Invoice.invoice_date.desc(), Invoice.id.desc())

    if from_date: q = q.where(Invoice.invoice_date >= from_date)
    if to_date:   q = q.where(Invoice.invoice_date <= to_date)
    if mobile:    q = q.where(Invoice.customer_mobile == mobile)
    if status:    q = q.where(Invoice.payment_status == status)

    result = await db.execute(q)
    return result.scalars().all()


# ── Get Single Invoice ────────────────────────────────────────

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


# ── Get Invoice Items ─────────────────────────────────────────

@router.get("/{invoice_id}/items")
async def get_invoice_items(
    invoice_id: int,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    invoice = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != payload["tenant_id"]:
        raise HTTPException(status_code=404, detail="Invoice not found")

    result = await db.execute(
        select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id)
    )
    items = result.scalars().all()
    return {
        "invoice_id": invoice_id,
        "invoice_no": invoice.invoice_no,
        "items": [
            {
                "id":             item.id,
                "category":       item.category.value,
                "purity":         item.purity or "",
                "description":    item.description,
                "hsn_code":       item.hsn_code,
                "qty":            float(item.qty),
                "unit":           item.unit.value,
                "rate":           float(item.rate),
                "polish_charges": float(item.polish_charges) if item.polish_charges else 0.0,
                "making_charges": float(item.making_charges),
                "amount":         float(item.amount),
            }
            for item in items
        ],
    }


# ── Amend / Edit Invoice (non-financial fields only) ──────────

@router.put("/{invoice_id}/amend")
async def amend_invoice(
    invoice_id: int,
    body:       InvoiceAmend,
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    invoice = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != payload["tenant_id"]:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status == InvoiceStatus.cancelled:
        raise HTTPException(status_code=400, detail="Cannot amend a cancelled invoice")

    if body.invoice_date   is not None: invoice.invoice_date   = body.invoice_date
    if body.customer_gstin is not None: invoice.customer_gstin = body.customer_gstin or None
    if body.pay_mode       is not None: invoice.pay_mode       = body.pay_mode
    if body.notes          is not None: invoice.notes          = body.notes

    if body.customer_pan is not None:
        pan = body.customer_pan.upper().strip()
        if pan and not re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]$', pan):
            raise HTTPException(status_code=422, detail="PAN format invalid. Expected: ABCDE1234F")
        invoice.customer_pan = pan or None
        cust = await db.get(Customer, (invoice.customer_mobile, payload["tenant_id"]))
        if cust and pan:
            cust.pan = pan

    await db.commit()
    await db.refresh(invoice)

    return {
        "message":    "Invoice amended successfully",
        "invoice_no": invoice.invoice_no,
        "invoice_id": invoice.id,
    }


# ── Cancel Invoice ────────────────────────────────────────────

@router.put("/{invoice_id}/cancel")
async def cancel_invoice(
    invoice_id: int,
    body:       dict         = {},
    payload:    dict         = Depends(get_current_user_payload),
    db:         AsyncSession = Depends(get_db),
):
    invoice = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != payload["tenant_id"]:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status == InvoiceStatus.cancelled:
        raise HTTPException(status_code=400, detail="Invoice already cancelled")

    invoice.status = InvoiceStatus.cancelled

    # Restore stock at original FIFO purchase_rate (ISSUE 4 — confirmed correct)
    await _restore_stock(db, payload["tenant_id"], int(payload["sub"]), invoice)

    await db.commit()

    credit_note_no = f"CN-{invoice.invoice_no}"
    return {
        "message":        f"Invoice {invoice.invoice_no} cancelled",
        "credit_note_no": credit_note_no,
        "invoice_no":     invoice.invoice_no,
    }


# ── Full Invoice Edit ─────────────────────────────────────────
#
# ISSUE 2 FIX — stock handling during sale edit:
#
# OLD WRONG APPROACH (created ghost rows):
#   1. Loop old items → stock.qty_on_hand += old_qty
#      → db.add(StockTransaction adjustment IN lot_remaining=old_qty)  ← GHOST ROW
#   2. Delete old sale transaction rows
#   3. Call _deduct_stock() → new OUT sale transaction
#   Result: stock register showed "Edit Reversal" IN rows that should not exist.
#
# CORRECT APPROACH (in-place update):
#   1. For each item, find existing sale StockTransaction for this invoice
#   2. Compute new FIFO-weighted avg rate for the new qty
#   3. UPDATE that row: qty = -new_qty, purchase_rate = new_fifo_rate
#   4. Adjust stock.qty_on_hand by delta (new_qty - old_qty) only
#   5. Delete/add InvoiceItem rows (pure accounting records, not stock rows)
#   6. NO ghost IN rows, NO reversal rows, NO new OUT rows
#
# Edge case — item added in edit (not in original invoice):
#   Create a new sale transaction for that item (no prior row to update).
#
# Edge case — item removed in edit:
#   Delete its sale transaction; restore stock.qty_on_hand by removed qty.

class InvoiceEditBody(BaseModel):
    invoice_date:    Optional[date]    = None
    customer_mobile: Optional[str]     = None
    customer_name:   Optional[str]     = None
    customer_pan:    Optional[str]     = None
    customer_state:  Optional[str]     = None
    customer_gstin:  Optional[str]     = None
    pay_mode:        Optional[str]     = None
    gst_type:        Optional[str]     = None
    gst_rate:        Optional[float]   = None
    round_off:       Optional[float]   = None
    notes:           Optional[str]     = None
    items:           Optional[list[InvoiceItemIn]] = None


@router.put("/{invoice_id}/edit")
async def edit_invoice(
    invoice_id: int,
    body:       InvoiceEditBody,
    payload:    dict          = Depends(get_current_user_payload),
    db:         AsyncSession  = Depends(get_db),
):
    """
    Full invoice edit — header fields and/or replace all line items.

    Stock handling (ISSUE 2 FIX):
    - Existing sale StockTransaction rows for this invoice are UPDATED in-place.
    - stock.qty_on_hand is adjusted by the qty delta only.
    - No reversal IN rows, no ghost adjustment rows — register stays clean.
    """
    tenant_id = payload["tenant_id"]
    invoice   = await db.get(Invoice, invoice_id)
    if not invoice or invoice.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status.value == "cancelled":
        raise HTTPException(status_code=400, detail="Cannot edit a cancelled invoice")

    if body.customer_pan and not re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]$', body.customer_pan):
        raise HTTPException(status_code=422, detail="PAN format invalid. Expected: ABCDE1234F")

    # ── Update header fields ──────────────────────────────────
    if body.invoice_date    is not None: invoice.invoice_date    = body.invoice_date
    if body.customer_mobile is not None: invoice.customer_mobile = body.customer_mobile
    if body.customer_name   is not None: invoice.customer_name   = body.customer_name
    if body.customer_pan    is not None: invoice.customer_pan    = body.customer_pan
    if body.customer_state  is not None: invoice.customer_state  = body.customer_state
    if body.customer_gstin  is not None: invoice.customer_gstin  = body.customer_gstin
    if body.pay_mode        is not None: invoice.pay_mode        = body.pay_mode
    if body.gst_type        is not None: invoice.gst_type        = body.gst_type
    if body.gst_rate        is not None: invoice.gst_rate        = Decimal(str(body.gst_rate))
    if body.notes           is not None: invoice.notes           = body.notes

    if body.round_off is not None and body.items is None:
        new_round  = Decimal(str(body.round_off)).quantize(Decimal("0.01"))
        base_total = invoice.subtotal + invoice.cgst + invoice.sgst + invoice.igst + invoice.tcs_amount
        invoice.grand_total = base_total + new_round
        invoice.outstanding = max(Decimal("0"), invoice.grand_total - invoice.amount_paid)
        if invoice.outstanding <= 0:
            invoice.payment_status = PaymentStatus.paid
        elif invoice.amount_paid > 0:
            invoice.payment_status = PaymentStatus.partial
        else:
            invoice.payment_status = PaymentStatus.unpaid

    # ── Replace items if provided ─────────────────────────────
    if body.items is not None:
        if not body.items:
            raise HTTPException(status_code=400, detail="Invoice must have at least one item")

        # Load old items
        old_items_result = await db.execute(
            select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id)
        )
        old_items = old_items_result.scalars().all()

        # Load existing sale transactions for this invoice, keyed by stock_item_id
        sale_txns_result = await db.execute(
            select(StockTransaction).where(
                StockTransaction.txn_type == StockTxnType.sale,
                StockTransaction.reason  == f"Sale — Invoice ID {invoice_id}",
            )
        )
        sale_txns_by_stock: dict[int, StockTransaction] = {
            t.stock_item_id: t
            for t in sale_txns_result.scalars().all()
        }

        # ── ISSUE 2 FIX: update sale txn rows in-place ───────────────────
        for old_item in old_items:
            cat_val = getattr(old_item.category, 'value', str(old_item.category))
            if cat_val == "Polish Charges":
                continue

            stock = await _find_stock(db, tenant_id, old_item.category, old_item.purity)
            if not stock:
                continue

            sale_txn = sale_txns_by_stock.get(stock.id)

            # Find the corresponding new item (match by category + purity)
            new_item = next(
                (it for it in body.items
                 if it.category == cat_val
                 and (it.purity or None) == old_item.purity),
                None,
            )

            if new_item is None:
                # Item removed in this edit — restore its qty, delete its sale txn
                stock.qty_on_hand += old_item.qty
                if sale_txn:
                    await db.delete(sale_txn)
                    del sale_txns_by_stock[stock.id]
                continue

            # Item still present — compute new FIFO rate and update txn in-place
            new_qty  = new_item.qty
            old_qty  = old_item.qty
            qty_delta = new_qty - old_qty

            if sale_txn:
                # Temporarily restore the old qty so _compute_fifo_avg_rate
                # sees the correct available stock (the stock was deducted at
                # original sale time; we need to see it as if re-sold now)
                stock.qty_on_hand += old_qty     # virtual restore for rate calc
                await db.flush()                  # write so sub-query sees it

                new_fifo_rate = await _compute_fifo_avg_rate(db, stock, new_qty)

                # Now apply the real delta
                stock.qty_on_hand -= old_qty     # undo virtual restore
                stock.qty_on_hand -= qty_delta   # apply actual delta

                # Update the existing sale transaction row in-place — NO new rows
                sale_txn.qty           = -new_qty
                sale_txn.purchase_rate = new_fifo_rate
                if body.invoice_date:
                    sale_txn.txn_date = body.invoice_date
            else:
                # No prior sale txn for this item — create one (shouldn't happen
                # on a well-formed invoice, but handle defensively)
                stock.qty_on_hand -= qty_delta
                new_fifo_rate = await _compute_fifo_avg_rate(db, stock, new_qty)
                db.add(StockTransaction(
                    tenant_id=tenant_id,
                    stock_item_id=stock.id,
                    txn_type=StockTxnType.sale,
                    qty=-new_qty,
                    purchase_rate=new_fifo_rate,
                    txn_date=body.invoice_date or invoice.invoice_date,
                    reason=f"Sale — Invoice ID {invoice_id}",
                    created_by=int(payload["sub"]),
                ))

        # Handle brand-new items (present in new list but not old invoice)
        old_categories = {
            (getattr(oi.category, 'value', str(oi.category)), oi.purity or None)
            for oi in old_items
        }
        for new_item in body.items:
            key = (new_item.category, new_item.purity or None)
            if key in old_categories:
                continue   # already handled above
            cat_val = new_item.category
            if cat_val == "Polish Charges":
                continue

            stock = await _find_stock(db, tenant_id, new_item.category, new_item.purity)
            if not stock:
                continue

            new_fifo_rate = await _compute_fifo_avg_rate(db, stock, new_item.qty)
            stock.qty_on_hand -= new_item.qty
            db.add(StockTransaction(
                tenant_id=tenant_id,
                stock_item_id=stock.id,
                txn_type=StockTxnType.sale,
                qty=-new_item.qty,
                purchase_rate=new_fifo_rate,
                txn_date=body.invoice_date or invoice.invoice_date,
                reason=f"Sale — Invoice ID {invoice_id}",
                created_by=int(payload["sub"]),
            ))

        # ── Replace InvoiceItem rows ──────────────────────────────────────
        for old_item in old_items:
            await db.delete(old_item)
        await db.flush()

        new_gst_type = body.gst_type or invoice.gst_type.value
        new_gst_rate = Decimal(str(body.gst_rate)) if body.gst_rate else invoice.gst_rate

        subtotal = Decimal("0")
        new_rows = []
        for item in body.items:
            amount = (
                item.qty * item.rate
                + item.polish_charges * item.rate
                + item.making_charges
            ).quantize(Decimal("0.01"))
            subtotal += amount
            new_rows.append(InvoiceItem(
                tenant_id=tenant_id,
                invoice_id=invoice_id,
                category=item.category,
                purity=item.purity,
                description=item.description,
                hsn_code=item.hsn_code,
                qty=item.qty,
                unit=item.unit,
                rate=item.rate,
                polish_charges=item.polish_charges,
                making_charges=item.making_charges,
                amount=amount,
            ))

        gst       = calculate_gst(subtotal, new_gst_rate, new_gst_type)
        new_round = (
            Decimal(str(body.round_off))
            if body.round_off is not None
            else (invoice.round_off or Decimal("0"))
        )
        new_grand = subtotal + gst["total_gst"] + new_round

        if new_grand > PAN_THRESHOLD and not (body.customer_pan or invoice.customer_pan):
            raise HTTPException(
                status_code=422,
                detail=f"PAN is mandatory — invoice value Rs.{new_grand:,.0f} exceeds Rs.2,00,000.",
            )

        amount_already_paid = invoice.amount_paid
        invoice.subtotal    = subtotal
        invoice.cgst        = gst["cgst"]
        invoice.sgst        = gst["sgst"]
        invoice.igst        = gst["igst"]
        invoice.gst_rate    = new_gst_rate
        invoice.grand_total = new_grand
        invoice.outstanding = max(Decimal("0"), new_grand - amount_already_paid)
        if invoice.outstanding <= 0:
            invoice.payment_status = PaymentStatus.paid
        elif invoice.amount_paid > 0:
            invoice.payment_status = PaymentStatus.partial
        else:
            invoice.payment_status = PaymentStatus.unpaid

        for item in new_rows:
            db.add(item)
        await db.flush()

    await db.commit()
    await db.refresh(invoice)

    return {
        "id":             invoice.id,
        "invoice_no":     invoice.invoice_no,
        "message":        "Invoice updated successfully",
        "grand_total":    float(invoice.grand_total),
        "outstanding":    float(invoice.outstanding),
        "payment_status": invoice.payment_status.value,
    }
