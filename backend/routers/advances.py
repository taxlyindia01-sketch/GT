# routers/advances.py — Advance payment recording and allocation
from datetime import date
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models import Advance, AdvanceAllocation, Invoice
from utils.auth import get_tenant_payload as get_current_user_payload

router = APIRouter()

class AdvanceCreate(BaseModel):
    customer_mobile: str
    amount:          Decimal
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
    advance = Advance(
        tenant_id=payload["tenant_id"],
        customer_mobile=body.customer_mobile,
        amount=body.amount,
        remaining=body.amount,
        advance_date=body.advance_date,
        pay_mode=body.pay_mode,
        notes=body.notes,
        created_by=int(payload["sub"]),
    )
    db.add(advance)
    await db.commit()
    return {"message": "Advance recorded", "advance_id": advance.id}

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
    return [{"id": a.id, "customer_mobile": a.customer_mobile, "amount": float(a.amount),
             "remaining": float(a.remaining), "date": a.advance_date.isoformat(),
             "mode": a.pay_mode.value, "notes": a.notes} for a in advances]
