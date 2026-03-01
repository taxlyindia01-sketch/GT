# routers/reports.py — All business reports: Sales, TCS, SFT, GSTR-1, Account, Cash, FIFO

from datetime import date
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from models import Invoice, InvoiceItem, Customer, Payment, CashEntry, StockItem, StockTransaction
from utils.auth import get_tenant_payload as get_current_user_payload
from utils.business import current_fy, fifo_valuation, summarise_cash, SFT_THRESHOLD

router = APIRouter()


# ── Sales Register ────────────────────────────────────────────

@router.get("/sales")
async def sales_register(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Invoice-wise sales register with GST and TCS breakdown."""
    tenant_id = payload["tenant_id"]
    stmt = (
        select(Invoice)
        .where(Invoice.tenant_id == tenant_id, Invoice.status == "active")
        .order_by(Invoice.invoice_date.desc())
    )
    if from_date:
        stmt = stmt.where(Invoice.invoice_date >= from_date)
    if to_date:
        stmt = stmt.where(Invoice.invoice_date <= to_date)

    result   = await db.execute(stmt)
    invoices = result.scalars().all()

    rows = []
    for inv in invoices:
        rows.append({
            "invoice_no":     inv.invoice_no,
            "invoice_date":   inv.invoice_date.isoformat(),
            "customer_name":  inv.customer_name,
            "customer_mobile":inv.customer_mobile,
            "pay_mode":       inv.pay_mode.value,
            "subtotal":       float(inv.subtotal),
            "cgst":           float(inv.cgst),
            "sgst":           float(inv.sgst),
            "igst":           float(inv.igst),
            "tcs_amount":     float(inv.tcs_amount),
            "grand_total":    float(inv.grand_total),
            "payment_status": inv.payment_status.value,
        })

    return {
        "rows":          rows,
        "total_subtotal":sum(r["subtotal"]  for r in rows),
        "total_gst":     sum(r["cgst"]+r["sgst"]+r["igst"] for r in rows),
        "total_tcs":     sum(r["tcs_amount"] for r in rows),
        "grand_total":   sum(r["grand_total"] for r in rows),
    }


# ── TCS Register — Section 206C(1F) ───────────────────────────

@router.get("/tcs")
async def tcs_register(
    fy:      Optional[str] = Query(None, description="e.g. '2025-26'"),
    payload: dict          = Depends(get_current_user_payload),
    db:      AsyncSession  = Depends(get_db),
):
    """
    TCS register for 26Q filing.
    Shows all invoices where TCS was collected (cash payment > ₹5L).
    """
    tenant_id = payload["tenant_id"]
    fy_start, fy_end = current_fy()

    result = await db.execute(
        select(Invoice).where(
            Invoice.tenant_id     == tenant_id,
            Invoice.tcs_applicable == True,
            Invoice.status        == "active",
            Invoice.invoice_date  >= fy_start,
            Invoice.invoice_date  <= fy_end,
        ).order_by(Invoice.invoice_date.desc())
    )
    invoices = result.scalars().all()

    rows = [{
        "invoice_no":     inv.invoice_no,
        "invoice_date":   inv.invoice_date.isoformat(),
        "customer_name":  inv.customer_name,
        "customer_mobile":inv.customer_mobile,
        "customer_pan":   inv.customer_pan or "⚠ MISSING",
        "invoice_value":  float(inv.grand_total),
        "tcs_base":       float(inv.tcs_base),
        "tcs_amount":     float(inv.tcs_amount),
        "pay_mode":       inv.pay_mode.value,
    } for inv in invoices]

    return {
        "rows":              rows,
        "total_tcs_collected": sum(r["tcs_amount"] for r in rows),
        "total_taxable_value": sum(r["tcs_base"]   for r in rows),
        "fy":                  f"FY {fy_start.year}-{str(fy_start.year+1)[2:]}",
    }


# ── SFT Register ──────────────────────────────────────────────

@router.get("/sft")
async def sft_register(
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """
    SFT report — customers with cash receipts > ₹2,00,000 in the current FY.

    FIX #5: Compute cash from INVOICES only (not customer.cash_receipts_fy).
    Each invoice is counted ONCE — when it is created in Cash pay_mode.
    Receiving a payment against a cash invoice does NOT double-count.
    """
    from models import InvoiceStatus
    tenant_id = payload["tenant_id"]
    fy_start, fy_end = current_fy()

    # Step 1: Sum cash invoices per customer in current FY (each invoice counted once)
    inv_result = await db.execute(
        select(Invoice).where(
            Invoice.tenant_id    == tenant_id,
            Invoice.pay_mode     == "Cash",
            Invoice.status       != InvoiceStatus.cancelled,
            Invoice.invoice_date >= fy_start,
            Invoice.invoice_date <= fy_end,
        )
    )
    invoices = inv_result.scalars().all()

    # Aggregate by customer mobile
    from collections import defaultdict
    cash_by_mobile: dict = defaultdict(Decimal)
    name_by_mobile: dict = {}
    for inv in invoices:
        cash_by_mobile[inv.customer_mobile] += inv.grand_total
        name_by_mobile[inv.customer_mobile] = inv.customer_name

    # Step 2: Get customer PAN for flagged customers
    flagged_mobiles = [m for m, total in cash_by_mobile.items() if total >= SFT_THRESHOLD]
    pan_map: dict = {}
    if flagged_mobiles:
        cust_res = await db.execute(
            select(Customer).where(
                Customer.tenant_id == tenant_id,
                Customer.mobile.in_(flagged_mobiles),
            )
        )
        for c in cust_res.scalars():
            pan_map[c.mobile] = c.pan

    rows = []
    for mobile in flagged_mobiles:
        pan = pan_map.get(mobile)
        total = float(cash_by_mobile[mobile])
        rows.append({
            "customer_name":   name_by_mobile.get(mobile, mobile),  # FIX: include name
            "mobile":          mobile,
            "pan":             pan or None,
            "cash_receipts_fy":total,
            "sft_threshold":   float(SFT_THRESHOLD),
            "pan_missing":     not pan,
            "status":          "PAN Required" if not pan else "Flag for SFT",
        })
    rows.sort(key=lambda r: -r["cash_receipts_fy"])

    return {
        "rows":              rows,
        "total_flagged":     len(rows),
        "total_pan_missing": sum(1 for r in rows if r["pan_missing"]),
    }


# ── GSTR-1 Register ───────────────────────────────────────────

@router.get("/gstr1")
async def gstr1_register(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """GSTR-1 register with HSN-wise taxable value and GST breakdown."""
    tenant_id = payload["tenant_id"]
    stmt = (
        select(Invoice)
        .where(Invoice.tenant_id == tenant_id, Invoice.status == "active")
        .order_by(Invoice.invoice_date.desc())
    )
    if from_date:
        stmt = stmt.where(Invoice.invoice_date >= from_date)
    if to_date:
        stmt = stmt.where(Invoice.invoice_date <= to_date)

    result   = await db.execute(stmt)
    invoices = result.scalars().all()

    rows = [{
        "invoice_no":     inv.invoice_no,
        "invoice_date":   inv.invoice_date.isoformat(),
        "customer_name":  inv.customer_name,
        "customer_gstin": inv.customer_gstin or "Unregistered",
        "customer_state": inv.customer_state or "",
        "hsn_code":       "7113",
        "gst_type":       inv.gst_type.value,
        "taxable_value":  float(inv.subtotal),
        "cgst_rate":      float(inv.gst_rate / 2),
        "cgst_amount":    float(inv.cgst),
        "sgst_rate":      float(inv.gst_rate / 2),
        "sgst_amount":    float(inv.sgst),
        "igst_amount":    float(inv.igst),
        "grand_total":    float(inv.grand_total),
    } for inv in invoices]

    return {
        "rows":            rows,
        "total_taxable":   sum(r["taxable_value"] for r in rows),
        "total_cgst":      sum(r["cgst_amount"]   for r in rows),
        "total_sgst":      sum(r["sgst_amount"]   for r in rows),
        "total_igst":      sum(r["igst_amount"]   for r in rows),
    }


# ── Account Register ──────────────────────────────────────────

@router.get("/account")
async def account_register(
    view:      str            = Query("invoice", regex="^(invoice|item)$"),
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """
    Account register — two views:
    - invoice: Full invoice-level details
    - item:    Per line-item details
    """
    tenant_id = payload["tenant_id"]
    stmt = (
        select(Invoice)
        .where(Invoice.tenant_id == tenant_id, Invoice.status == "active")
        .order_by(Invoice.invoice_date.desc())
    )
    if from_date:
        stmt = stmt.where(Invoice.invoice_date >= from_date)
    if to_date:
        stmt = stmt.where(Invoice.invoice_date <= to_date)

    result   = await db.execute(stmt)
    invoices = result.scalars().all()

    if view == "invoice":
        return [{
            "invoice_no":     inv.invoice_no,
            "invoice_date":   inv.invoice_date.isoformat(),
            "customer_name":  inv.customer_name,
            "customer_mobile":inv.customer_mobile,
            "customer_state": inv.customer_state,
            "gst_type":       inv.gst_type.value,
            "subtotal":       float(inv.subtotal),
            "cgst":           float(inv.cgst),
            "sgst":           float(inv.sgst),
            "igst":           float(inv.igst),
            "tcs_amount":     float(inv.tcs_amount),
            "grand_total":    float(inv.grand_total),
            "pay_mode":       inv.pay_mode.value,
        } for inv in invoices]
    else:
        # Item-wise view — fetch items for each invoice
        rows = []
        for inv in invoices:
            items_result = await db.execute(
                select(InvoiceItem).where(InvoiceItem.invoice_id == inv.id)
            )
            for item in items_result.scalars():
                rows.append({
                    "invoice_no":    inv.invoice_no,
                    "invoice_date":  inv.invoice_date.isoformat(),
                    "category":      item.category.value,
                    "purity":        item.purity or "—",
                    "description":   item.description,
                    "hsn_code":      item.hsn_code,
                    "qty":           float(item.qty),
                    "unit":          item.unit.value,
                    "rate":          float(item.rate),
                    "making_charges":float(item.making_charges),
                    "amount":        float(item.amount),
                })
        return rows


# ── Cash Register Summary ─────────────────────────────────────

@router.get("/cash/summary")
async def cash_summary(
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """Return FY cash KPIs: collected, deposited in bank, cash on hand."""
    tenant_id = payload["tenant_id"]
    result = await db.execute(
        select(CashEntry).where(CashEntry.tenant_id == tenant_id).order_by(CashEntry.entry_date)
    )
    entries = result.scalars().all()

    raw = [{"entry_date": e.entry_date, "entry_type": e.entry_type.value, "amount": e.amount}
           for e in entries]
    summary = summarise_cash(raw)

    return {
        "cash_collected_fy":  float(summary["cash_collected_fy"]),
        "cash_deposited_fy":  float(summary["cash_deposited_fy"]),
        "cash_on_hand":       float(summary["cash_on_hand"]),
    }


# ── FIFO Stock Valuation ──────────────────────────────────────

@router.get("/fifo")
async def fifo_report(
    as_of:     Optional[date] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """
    FIFO stock valuation report.
    FIX #6: Added from_date/to_date range + stock movements (IN/OUT) per item.
    Polish Charges are excluded.
    """
    from models import StockTxnType
    tenant_id = payload["tenant_id"]
    cutoff    = as_of or to_date or date.today()
    range_start = from_date  # None = all time

    stocks_result = await db.execute(
        select(StockItem).where(
            StockItem.tenant_id == tenant_id,
            StockItem.category  != "Polish Charges",
            StockItem.is_active == True,
        )
    )
    stocks = stocks_result.scalars().all()

    report = []
    for stock in stocks:
        # FIFO valuation lots (purchase/opening up to cutoff)
        txns_result = await db.execute(
            select(StockTransaction).where(
                StockTransaction.stock_item_id == stock.id,
                StockTransaction.txn_date     <= cutoff,
                StockTransaction.txn_type.in_(["purchase", "opening"]),
                StockTransaction.lot_remaining > 0,
            ).order_by(StockTransaction.txn_date)
        )
        lots = [
            {"qty_remaining": t.lot_remaining, "purchase_rate": t.purchase_rate}
            for t in txns_result.scalars()
            if t.purchase_rate is not None
        ]
        valuation = fifo_valuation(lots)

        # Stock movements in selected date range
        mvt_q = select(StockTransaction).where(
            StockTransaction.stock_item_id == stock.id,
            StockTransaction.txn_date     <= cutoff,
        )
        if range_start:
            mvt_q = mvt_q.where(StockTransaction.txn_date >= range_start)
        mvt_q = mvt_q.order_by(StockTransaction.txn_date.asc())
        mvt_result = await db.execute(mvt_q)
        movements = []
        qty_in = qty_out = Decimal("0")
        for t in mvt_result.scalars():
            is_in = t.txn_type in ("purchase", "opening", "adjustment") and t.qty_change > 0
            is_out = t.qty_change < 0
            if is_in:
                qty_in += t.qty_change
            elif is_out:
                qty_out += abs(t.qty_change)
            movements.append({
                "date":      t.txn_date.isoformat(),
                "type":      t.txn_type.value if hasattr(t.txn_type, "value") else str(t.txn_type),
                "qty":       float(t.qty_change),
                "rate":      float(t.purchase_rate) if t.purchase_rate else None,
                "reason":    t.reason or "",
            })

        report.append({
            "category":    stock.category.value,
            "purity":      stock.purity or "—",
            "description": stock.description,
            "unit":        stock.unit.value,
            "qty_on_hand": float(stock.qty_on_hand),
            "qty_in":      float(qty_in),
            "qty_out":     float(qty_out),
            "avg_rate":    float(valuation["avg_rate"]),
            "total_value": float(valuation["total_value"]),
            "movements":   movements,
        })

    return {
        "as_of":       cutoff.isoformat(),
        "from_date":   range_start.isoformat() if range_start else None,
        "rows":        report,
        "grand_total": sum(r["total_value"] for r in report),
    }


# ── Outstanding ───────────────────────────────────────────────

@router.get("/outstanding")
async def outstanding(
    mobile:  Optional[str] = Query(None),
    payload: dict          = Depends(get_current_user_payload),
    db:      AsyncSession  = Depends(get_db),
):
    """List all invoices with outstanding balances."""
    tenant_id = payload["tenant_id"]
    stmt = select(Invoice).where(
        Invoice.tenant_id     == tenant_id,
        Invoice.payment_status != "paid",
        Invoice.status         == "active",
    ).order_by(Invoice.invoice_date.desc())

    if mobile:
        stmt = stmt.where(Invoice.customer_mobile == mobile)

    result   = await db.execute(stmt)
    invoices = result.scalars().all()

    rows = [{
        "invoice_no":     inv.invoice_no,
        "invoice_date":   inv.invoice_date.isoformat(),
        "customer_name":  inv.customer_name,
        "customer_mobile":inv.customer_mobile,
        "grand_total":    float(inv.grand_total),
        "amount_paid":    float(inv.amount_paid),
        "outstanding":    float(inv.outstanding),
    } for inv in invoices]

    return {
        "rows":             rows,
        "total_outstanding":sum(r["outstanding"] for r in rows),
    }


# ── Cash Book Register (replica of Cash Book page) ────────────

@router.get("/cashbook")
async def cashbook_report(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Full cash book register with running balance — replica of Cash Book page."""
    tid = payload["tenant_id"]
    q = select(CashEntry).where(CashEntry.tenant_id == tid).order_by(CashEntry.entry_date.asc(), CashEntry.id.asc())
    if from_date: q = q.where(CashEntry.entry_date >= from_date)
    if to_date:   q = q.where(CashEntry.entry_date <= to_date)
    result = await db.execute(q)
    entries = result.scalars().all()

    rows = []
    for e in entries:
        rows.append({
            "id":             e.id,
            "date":           e.entry_date.isoformat(),
            "type":           e.entry_type.value,
            "description":    e.description,
            "amount":         float(e.amount),
            "bank_reference": e.bank_reference or "",
            "running_balance": float(e.running_balance) if e.running_balance is not None else None,
        })

    return {"rows": rows, "total_rows": len(rows)}


# ── Payments Register ─────────────────────────────────────────

@router.get("/payments")
async def payments_report(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Full payment register — replica of Payments page."""
    from models import Invoice as InvoiceM
    tid = payload["tenant_id"]
    q = select(Payment).where(Payment.tenant_id == tid).order_by(Payment.payment_date.desc())
    if from_date: q = q.where(Payment.payment_date >= from_date)
    if to_date:   q = q.where(Payment.payment_date <= to_date)
    result = await db.execute(q)
    payments = result.scalars().all()

    # Batch-load invoice numbers
    inv_ids = list({p.invoice_id for p in payments if p.invoice_id})
    inv_map = {}
    if inv_ids:
        inv_res = await db.execute(select(InvoiceM).where(InvoiceM.id.in_(inv_ids)))
        for inv in inv_res.scalars().all():
            inv_map[inv.id] = {"no": inv.invoice_no, "name": inv.customer_name}

    rows = [{
        "id":           p.id,
        "date":         p.payment_date.isoformat(),
        "invoice_no":   inv_map.get(p.invoice_id, {}).get("no", f"ID:{p.invoice_id}"),
        "customer_name":inv_map.get(p.invoice_id, {}).get("name", ""),
        "mobile":       p.customer_mobile,
        "amount":       float(p.amount),
        "pay_mode":     p.pay_mode,
        "reference_no": p.reference_no or "",
        "notes":        p.notes or "",
    } for p in payments]

    return {
        "rows":  rows,
        "total": sum(r["amount"] for r in rows),
    }


# ── Item-wise Invoice Summary ─────────────────────────────────

@router.get("/itemwise")
async def itemwise_report(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Item-wise accounting summary: every invoice line with header fields."""
    from models import InvoiceStatus
    tid = payload["tenant_id"]
    q = (
        select(InvoiceItem, Invoice)
        .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
        .where(Invoice.tenant_id == tid, Invoice.status != InvoiceStatus.cancelled)
    )
    if from_date: q = q.where(Invoice.invoice_date >= from_date)
    if to_date:   q = q.where(Invoice.invoice_date <= to_date)
    q = q.order_by(Invoice.invoice_date.desc(), Invoice.id, InvoiceItem.id)
    result = await db.execute(q)
    rows_raw = result.all()

    rows = [{
        "invoice_no":     inv.invoice_no,
        "invoice_date":   inv.invoice_date.isoformat(),
        "customer_name":  inv.customer_name,
        "mobile":         inv.customer_mobile,
        "pan":            inv.customer_pan or "",
        "pay_mode":       inv.pay_mode,
        "category":       item.category.value if hasattr(item.category, "value") else str(item.category),
        "purity":         item.purity or "",
        "description":    item.description,
        "hsn_code":       item.hsn_code,
        "qty":            float(item.qty),
        "unit":           item.unit.value if hasattr(item.unit, "value") else str(item.unit),
        "rate":           float(item.rate),
        "making_charges": float(item.making_charges),
        "amount":         float(item.amount),
        "invoice_total":  float(inv.grand_total),
    } for item, inv in rows_raw]

    return {
        "rows":        rows,
        "grand_total": sum(r["amount"] for r in rows),
    }
