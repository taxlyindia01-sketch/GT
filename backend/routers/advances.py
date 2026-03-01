# routers/advances.py — Advance payment recording and allocation
from datetime import date
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models import Advance, AdvanceAllocation, Invoice, CashEntry, CashEntryType, Customer
from utils.auth import get_tenant_payload as get_current_user_payload
from utils.business import is_sft_flagged, SFT_THRESHOLD

router = APIRouter()

class AdvanceCreate(BaseModel):
    customer_mobile: str
    amount:          Decimal  = Field(..., gt=0, description="Must be positive")
    advance_date:    date
    pay_mode:        str
    notes:           Optional[str] = None

class AllocationItem(BaseModel):
    invoice_id:       int
    allocated_amount: Decimal

class AllocateRequest(BaseModel):
    allocations: list[AllocationItem]

@router.post("/", status_code=201)
async def record_advance(
    body:    AdvanceCreate,
    payload: dict          = Depends(get_current_user_payload),
    db:      AsyncSession  = Depends(get_db),
):
    """
    Record a customer advance.
    P1 Fix: If pay_mode is Cash, also:
      - Creates a cash_register entry (cash_in) for cash book tracking
      - Updates customer.cash_receipts_fy and sft_flagged (mirrors payments behaviour)
    """
    tenant_id = payload["tenant_id"]

    # Look up customer for SFT tracking
    cust_res  = await db.execute(
        select(Customer).where(
            Customer.tenant_id == tenant_id,
            Customer.mobile    == body.customer_mobile,
        )
    )
    customer = cust_res.scalar_one_or_none()

    advance = Advance(
        tenant_id=tenant_id,
        customer_mobile=body.customer_mobile,
        amount=body.amount,
        remaining=body.amount,
        advance_date=body.advance_date,
        pay_mode=body.pay_mode,
        notes=body.notes,
        created_by=int(payload["sub"]),
    )
    db.add(advance)

    # P1 Fix: Cash advance → cash register + SFT
    if body.pay_mode == "Cash":
        cust_name = customer.name if customer else body.customer_mobile

        # Cash register entry
        db.add(CashEntry(
            tenant_id=tenant_id,
            entry_date=body.advance_date,
            entry_type=CashEntryType.cash_in,
            amount=body.amount,
            description=f"Advance — {cust_name} ({body.customer_mobile})",
            created_by=int(payload["sub"]),
        ))

        # SFT tracking
        if customer:
            customer.cash_receipts_fy += body.amount
            customer.sft_flagged       = is_sft_flagged(customer.cash_receipts_fy)

    await db.commit()
    return {
        "message":    "Advance recorded",
        "advance_id": advance.id,
        "cash_entry": body.pay_mode == "Cash",
    }

@router.post("/{advance_id}/allocate")
async def allocate_advance(
    advance_id: int,
    body:       AllocateRequest,
    payload:    dict          = Depends(get_current_user_payload),
    db:         AsyncSession  = Depends(get_db),
):
    """Allocate advance against one or more invoices."""
    advance = await db.get(Advance, advance_id)
    if not advance or advance.tenant_id != payload["tenant_id"]:
        raise HTTPException(status_code=404, detail="Advance not found")

    total_alloc = sum(a.allocated_amount for a in body.allocations)
    if total_alloc > advance.remaining:
        raise HTTPException(status_code=400, detail="Allocation exceeds remaining advance balance")

    for alloc in body.allocations:
        db.add(AdvanceAllocation(
            tenant_id=payload["tenant_id"],
            advance_id=advance_id,
            invoice_id=alloc.invoice_id,
            allocated_amount=alloc.allocated_amount,
            created_by=int(payload["sub"]),
        ))
        # Update invoice outstanding
        inv = await db.get(Invoice, alloc.invoice_id)
        if inv:
            inv.amount_paid += alloc.allocated_amount
            inv.outstanding -= alloc.allocated_amount

    advance.remaining -= total_alloc
    await db.commit()
    return {"message": "Advance allocated", "remaining": float(advance.remaining)}

@router.get("/")
async def list_advances(
    mobile:  Optional[str] = None,
    payload: dict          = Depends(get_current_user_payload),
    db:      AsyncSession  = Depends(get_db),
):
    q = select(Advance).where(Advance.tenant_id == payload["tenant_id"]).order_by(Advance.advance_date.desc())
    if mobile: q = q.where(Advance.customer_mobile == mobile)
    result   = await db.execute(q)
    advances = result.scalars().all()

    # Build mobile→name map for customer names
    mobiles = list({a.customer_mobile for a in advances})
    name_map = {}
    if mobiles:
        cust_res = await db.execute(
            select(Customer).where(
                Customer.tenant_id == payload["tenant_id"],
                Customer.mobile.in_(mobiles),
            )
        )
        for c in cust_res.scalars():
            name_map[c.mobile] = c.name

    return [{"id": a.id, "customer_name": name_map.get(a.customer_mobile, ""),
             "customer_mobile": a.customer_mobile, "amount": float(a.amount),
             "remaining": float(a.remaining), "date": a.advance_date.isoformat(),
             "mode": a.pay_mode.value, "notes": a.notes} for a in advances]
