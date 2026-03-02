# routers/payments.py — Payment recording and advance allocation
from datetime import date
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models import Invoice, Payment, Advance, AdvanceAllocation, Customer, PaymentStatus, CashEntry, CashEntryType
from utils.auth import get_current_user_payload
from utils.business import is_sft_flagged, SFT_THRESHOLD

router = APIRouter()


class PaymentCreate(BaseModel):
    invoice_id:      int
    customer_mobile: str
    amount:          Decimal
    payment_date:    date
    pay_mode:        str
    reference_no:    Optional[str] = None
    notes:           Optional[str] = None


@router.post("/", status_code=201)
async def record_payment(
    body:    PaymentCreate,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """
    Record a payment against an invoice.
    - Updates invoice.amount_paid, invoice.outstanding, invoice.payment_status
    - If Cash payment, updates customer.cash_receipts_fy and SFT flag
    - Creates matching cash_register entry if payment mode is Cash
    """
    tenant_id = payload["tenant_id"]
    invoice   = await db.get(Invoice, body.invoice_id)
    if not invoice or invoice.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if float(body.amount) > float(invoice.outstanding):
        raise HTTPException(status_code=400, detail="Payment exceeds outstanding amount")

    # ── Section 269ST compliance check ─────────────────────────────────────
    # Cash payments ≥ ₹2,00,000 per transaction are prohibited under
    # Section 269ST of the Income Tax Act, 1961. If the frontend override
    # is used, the payment is saved but flagged for the 269ST report.
    SEC_269ST_THRESHOLD = Decimal("200000")
    sec_269st_violation = (
        body.pay_mode == "Cash" and body.amount >= SEC_269ST_THRESHOLD
    )

    payment = Payment(
        tenant_id=tenant_id,
        invoice_id=body.invoice_id,
        customer_mobile=body.customer_mobile,
        amount=body.amount,
        payment_date=body.payment_date,
        pay_mode=body.pay_mode,
        reference_no=body.reference_no,
        notes=body.notes,
        created_by=int(payload["sub"]),
    )
    db.add(payment)

    # Update invoice outstanding
    invoice.amount_paid += body.amount
    invoice.outstanding -= body.amount
    if float(invoice.outstanding) <= 0:
        invoice.payment_status = PaymentStatus.paid
    else:
        invoice.payment_status = PaymentStatus.partial

    # Update customer cash FY total + SFT flag
    if body.pay_mode == "Cash":
        customer = await db.get(Customer, (body.customer_mobile, tenant_id))
        if customer:
            customer.cash_receipts_fy += body.amount
            customer.sft_flagged = is_sft_flagged(customer.cash_receipts_fy)

        # Create cash_register entry
        db.add(CashEntry(
            tenant_id=tenant_id,
            entry_date=body.payment_date,
            entry_type=CashEntryType.cash_in,
            amount=body.amount,
            description=f"Payment — {invoice.customer_name} ({invoice.invoice_no})",
            invoice_id=body.invoice_id,
            created_by=int(payload["sub"]),
        ))

    await db.commit()
    response = {
        "message":          "Payment recorded",
        "outstanding":      float(invoice.outstanding),
        "sec_269st_violation": sec_269st_violation,
    }
    if sec_269st_violation:
        response["warning"] = (
            f"⚠️ Section 269ST Alert: Cash payment of ₹{float(body.amount):,.0f} "
            f"on {invoice.invoice_no} has been recorded. Cash receipts ≥ ₹2,00,000 "
            "are prohibited under Section 269ST of the Income Tax Act. "
            "This transaction appears in the Section 269ST Violation Report."
        )
    return response
