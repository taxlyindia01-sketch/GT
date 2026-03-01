# routers/stock.py — Stock management with FIFO lot tracking
from datetime import date
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models import StockItem, StockTransaction, StockTxnType
from utils.auth import get_tenant_payload as get_current_user_payload

router = APIRouter()

class StockCreate(BaseModel):
    category:      str
    purity:        Optional[str] = None
    description:   str
    unit:          str
    initial_qty:   Decimal = Decimal("0")
    purchase_rate: Optional[Decimal] = None

class StockAdjust(BaseModel):
    qty_change:    Decimal   # positive = in, negative = out
    purchase_rate: Optional[Decimal] = None
    reason:        Optional[str] = None
    txn_date:      date = date.today()

@router.post("/", status_code=201)
async def add_stock_item(
    body:    StockCreate,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    stock = StockItem(
        tenant_id=payload["tenant_id"],
        category=body.category,
        purity=body.purity,
        description=body.description,
        unit=body.unit,
        qty_on_hand=body.initial_qty,
        fifo_enabled=body.category != "Polish Charges",
    )
    db.add(stock)
    await db.flush()

    if body.initial_qty > 0:
        db.add(StockTransaction(
            tenant_id=payload["tenant_id"],
            stock_item_id=stock.id,
            txn_type=StockTxnType.opening,
            qty=body.initial_qty,
            purchase_rate=body.purchase_rate,
            txn_date=date.today(),
            lot_remaining=body.initial_qty,
        ))

    await db.commit()
    return {"message": "Stock item added", "stock_id": stock.id}

@router.post("/{stock_id}/adjust")
async def adjust_stock(
    stock_id: int,
    body:     StockAdjust,
    payload:  dict         = Depends(get_current_user_payload),
    db:       AsyncSession = Depends(get_db),
):
    stock = await db.get(StockItem, stock_id)
    if not stock or stock.tenant_id != payload["tenant_id"]:
        raise HTTPException(status_code=404, detail="Stock item not found")

    stock.qty_on_hand += body.qty_change
    if float(stock.qty_on_hand) < 0:
        raise HTTPException(status_code=400, detail="Insufficient stock quantity")

    txn_type = StockTxnType.purchase if body.qty_change > 0 else StockTxnType.adjustment
    db.add(StockTransaction(
        tenant_id=payload["tenant_id"],
        stock_item_id=stock_id,
        txn_type=txn_type,
        qty=body.qty_change,
        purchase_rate=body.purchase_rate,
        txn_date=body.txn_date,
        reason=body.reason,
        lot_remaining=body.qty_change if body.qty_change > 0 else None,
    ))
    await db.commit()
    return {"message": "Stock adjusted", "qty_on_hand": float(stock.qty_on_hand)}

@router.get("/")
async def list_stock(
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(StockItem).where(StockItem.tenant_id == payload["tenant_id"], StockItem.is_active == True)
    )
    stocks = result.scalars().all()
    return [{"id": s.id, "category": s.category.value, "purity": s.purity or "—",
             "description": s.description, "unit": s.unit.value,
             "qty_on_hand": float(s.qty_on_hand)} for s in stocks]
