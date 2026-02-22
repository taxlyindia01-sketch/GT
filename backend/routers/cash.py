# routers/cash.py — Cash Register entries
from datetime import date
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models import CashEntry, CashEntryType
from utils.auth import get_current_user_payload

router = APIRouter()

class CashEntryCreate(BaseModel):
    entry_date:     date
    entry_type:     str
    amount:         Decimal
    description:    str
    invoice_id:     Optional[int] = None
    bank_reference: Optional[str] = None

@router.post("/", status_code=201)
async def create_cash_entry(
    body:    CashEntryCreate,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    entry = CashEntry(
        tenant_id=payload["tenant_id"],
        entry_date=body.entry_date,
        entry_type=CashEntryType(body.entry_type),
        amount=body.amount,
        description=body.description,
        invoice_id=body.invoice_id,
        bank_reference=body.bank_reference,
        created_by=int(payload["sub"]),
    )
    db.add(entry)
    await db.commit()
    return {"message": "Cash entry recorded"}

@router.get("/")
async def list_cash_entries(
    from_date: Optional[date] = None,
    to_date:   Optional[date] = None,
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    q = select(CashEntry).where(CashEntry.tenant_id == payload["tenant_id"]).order_by(CashEntry.entry_date.desc())
    if from_date: q = q.where(CashEntry.entry_date >= from_date)
    if to_date:   q = q.where(CashEntry.entry_date <= to_date)
    result = await db.execute(q)
    entries = result.scalars().all()
    return [{"id": e.id, "date": e.entry_date.isoformat(), "type": e.entry_type.value,
             "description": e.description, "amount": float(e.amount),
             "bank_reference": e.bank_reference} for e in entries]
