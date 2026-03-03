# routers/reports.py
# Changes vs v4 original:
#  Issue 11 — /section-269st: customer_name and customer_pan fetched from master
#  Issue 12 — /fifo: qty_in and qty_out populated from StockTransaction records
#  Issue 13 — /cashbook, /payments, /itemwise endpoints added (were 404-ing)
#  P11      — TCS register kept but TCS values will be 0; added section-269st endpoint

from datetime import date
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from models import (
    Invoice, InvoiceItem, Customer, Payment,
    CashEntry, StockItem, StockTransaction,
)
from utils.auth import get_current_user_payload
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
    """Invoice-wise sales register with GST breakdown."""
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
        "invoice_no":      inv.invoice_no,
        "invoice_date":    inv.invoice_date.isoformat(),
        "customer_name":   inv.customer_name,
        "customer_mobile": inv.customer_mobile,
        "customer_pan":    inv.customer_pan or "—",
        "pay_mode":        inv.pay_mode.value,
        "subtotal":        float(inv.subtotal),
        "cgst":            float(inv.cgst),
        "sgst":            float(inv.sgst),
        "igst":            float(inv.igst),
        "tcs_amount":      float(inv.tcs_amount),
        "grand_total":     float(inv.grand_total),
        "payment_status":  inv.payment_status.value,
    } for inv in invoices]

    return {
        "rows":           rows,
        "total_subtotal": sum(r["subtotal"]  for r in rows),
        "total_gst":      sum(r["cgst"] + r["sgst"] + r["igst"] for r in rows),
        "total_tcs":      sum(r["tcs_amount"] for r in rows),
        "grand_total":    sum(r["grand_total"] for r in rows),
    }


# ── SFT Register ──────────────────────────────────────────────

@router.get("/sft")
async def sft_register(
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """SFT report — customers with cash receipts > ₹2,00,000 in current FY."""
    tenant_id = payload["tenant_id"]
    result = await db.execute(
        select(Customer).where(
            Customer.tenant_id   == tenant_id,
            Customer.sft_flagged == True,
        )
    )
    customers = result.scalars().all()
    rows = [{
        "customer_name":    c.name,
        "mobile":           c.mobile,
        "pan":              c.pan or None,
        "cash_receipts_fy": float(c.cash_receipts_fy),
        "sft_threshold":    float(SFT_THRESHOLD),
        "pan_missing":      not c.pan,
        "status":           "PAN Required" if not c.pan else "Flag for SFT",
    } for c in customers]

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
        "rows":          rows,
        "total_taxable": sum(r["taxable_value"] for r in rows),
        "total_cgst":    sum(r["cgst_amount"]   for r in rows),
        "total_sgst":    sum(r["sgst_amount"]   for r in rows),
        "total_igst":    sum(r["igst_amount"]   for r in rows),
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
        Invoice.tenant_id      == tenant_id,
        Invoice.payment_status != "paid",
        Invoice.status         == "active",
    ).order_by(Invoice.invoice_date.desc())

    if mobile:
        stmt = stmt.where(Invoice.customer_mobile == mobile)

    result   = await db.execute(stmt)
    invoices = result.scalars().all()

    rows = [{
        "invoice_no":      inv.invoice_no,
        "invoice_date":    inv.invoice_date.isoformat(),
        "customer_name":   inv.customer_name,
        "customer_mobile": inv.customer_mobile,
        "grand_total":     float(inv.grand_total),
        "amount_paid":     float(inv.amount_paid),
        "outstanding":     float(inv.outstanding),
    } for inv in invoices]

    return {
        "rows":              rows,
        "total_outstanding": sum(r["outstanding"] for r in rows),
    }


# ── Cash Register Summary (dashboard KPI) ────────────────────

@router.get("/cash/summary")
async def cash_summary(
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """Return FY cash KPIs: collected, deposited in bank, cash on hand."""
    tenant_id = payload["tenant_id"]
    result = await db.execute(
        select(CashEntry)
        .where(CashEntry.tenant_id == tenant_id)
        .order_by(CashEntry.entry_date)
    )
    entries = result.scalars().all()
    raw     = [{"entry_date": e.entry_date, "entry_type": e.entry_type.value, "amount": e.amount}
               for e in entries]
    summary = summarise_cash(raw)
    return {
        "cash_collected_fy": float(summary["cash_collected_fy"]),
        "cash_deposited_fy": float(summary["cash_deposited_fy"]),
        "cash_on_hand":      float(summary["cash_on_hand"]),
    }


# ── Cash Book Report ──────────────────────────────────────────
# Issue 13 fix — endpoint was missing, frontend showed "Error: Not found"

@router.get("/cashbook")
async def cashbook_report(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Cash book with running balance — day-by-day cash in/out register."""
    tenant_id = payload["tenant_id"]
    stmt = (
        select(CashEntry)
        .where(CashEntry.tenant_id == tenant_id)
        .order_by(CashEntry.entry_date, CashEntry.id)
    )
    if from_date:
        stmt = stmt.where(CashEntry.entry_date >= from_date)
    if to_date:
        stmt = stmt.where(CashEntry.entry_date <= to_date)

    result  = await db.execute(stmt)
    entries = result.scalars().all()

    running = Decimal("0")
    rows    = []
    for e in entries:
        amount = Decimal(str(e.amount))
        etype  = e.entry_type.value
        if etype in ("cash_in", "bank_in"):
            running += amount
        elif etype in ("cash_out", "cash_to_bank"):
            running -= amount

        rows.append({
            "date":             e.entry_date.isoformat(),
            "type":             etype,
            "description":      e.description or "",
            "amount":           float(amount),
            "bank_reference":   e.bank_reference or "",
            "running_balance":  float(running),
        })

    total_in  = sum(r["amount"] for r in rows if r["type"] in ("cash_in", "bank_in"))
    total_out = sum(r["amount"] for r in rows if r["type"] in ("cash_out", "cash_to_bank"))

    return {
        "rows":      rows,
        "total_in":  total_in,
        "total_out": total_out,
        "balance":   float(running),
    }


# ── Payments Report ───────────────────────────────────────────
# Issue 13 fix — endpoint was missing, frontend showed "Error: Not found"
# Also used by Issue 5 (Payments page fetch)

@router.get("/payments")
async def payments_report(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    mobile:    Optional[str]  = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """
    Payments register — all payments with customer name, invoice number, mode.
    Used by both the Payments page (Issue 5) and the Reports tab (Issue 13).
    """
    tenant_id = payload["tenant_id"]
    stmt = (
        select(Payment)
        .where(Payment.tenant_id == tenant_id)
        .order_by(Payment.payment_date.desc(), Payment.id.desc())
    )
    if from_date:
        stmt = stmt.where(Payment.payment_date >= from_date)
    if to_date:
        stmt = stmt.where(Payment.payment_date <= to_date)
    if mobile:
        stmt = stmt.where(Payment.customer_mobile == mobile)

    result   = await db.execute(stmt)
    payments = result.scalars().all()

    # Build invoice_no map
    inv_nos: dict[int, str] = {}
    for p in payments:
        if p.invoice_id and p.invoice_id not in inv_nos:
            inv = await db.get(Invoice, p.invoice_id)
            inv_nos[p.invoice_id] = inv.invoice_no if inv else "—"

    rows = []
    for p in payments:
        cname = (p.customer_name
                 if hasattr(p, "customer_name") and p.customer_name
                 else None)
        if not cname:
            cust  = await db.get(Customer, (p.customer_mobile, tenant_id))
            cname = cust.name if cust else "—"

        rows.append({
            "id":              p.id,
            "date":            p.payment_date.isoformat(),
            "payment_date":    p.payment_date.isoformat(),
            "invoice_no":      inv_nos.get(p.invoice_id, "—"),
            "invoice_id":      p.invoice_id,
            "customer_name":   cname,
            "mobile":          p.customer_mobile,
            "customer_mobile": p.customer_mobile,
            "amount":          float(p.amount),
            "pay_mode":        p.pay_mode.value,
            "reference_no":    p.reference_no or "—",
        })

    return {
        "rows":  rows,
        "total": sum(r["amount"] for r in rows),
    }


# ── Item-wise Sales Report ────────────────────────────────────
# Issue 13 fix — endpoint was missing, frontend showed "Error: Not found"

@router.get("/itemwise")
async def itemwise_report(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Item-wise sales report — one row per invoice line item."""
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

    rows        = []
    grand_total = Decimal("0")

    for inv in invoices:
        items_result = await db.execute(
            select(InvoiceItem).where(InvoiceItem.invoice_id == inv.id)
        )
        for item in items_result.scalars():
            rows.append({
                "invoice_no":     inv.invoice_no,
                "invoice_date":   inv.invoice_date.isoformat(),
                "customer_name":  inv.customer_name,
                "customer_mobile":inv.customer_mobile,
                "pan":            inv.customer_pan or "—",
                "pay_mode":       inv.pay_mode.value,
                "category":       item.category.value,
                "purity":         item.purity or "—",
                "description":    item.description,
                "qty":            float(item.qty),
                "unit":           item.unit.value,
                "rate":           float(item.rate),
                "making_charges": float(item.making_charges),
                "amount":         float(item.amount),
            })
            grand_total += item.amount

    return {
        "rows":        rows,
        "grand_total": float(grand_total),
    }


# ── FIFO Stock Valuation ──────────────────────────────────────
# Issue 12 fix — qty_in and qty_out were showing dashes because
# StockTransaction records weren't being aggregated per stock item.

@router.get("/fifo")
async def fifo_report(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    as_of:     Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """
    FIFO stock valuation report with Stock In and Stock Out totals.
    Polish Charges excluded. Supports date range filter.
    """
    tenant_id = payload["tenant_id"]
    cutoff    = as_of or to_date or date.today()

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
        # All transactions for this stock item up to cutoff
        txn_stmt = (
            select(StockTransaction)
            .where(
                StockTransaction.stock_item_id == stock.id,
                StockTransaction.txn_date      <= cutoff,
            )
            .order_by(StockTransaction.txn_date)
        )
        if from_date:
            txn_stmt = txn_stmt.where(StockTransaction.txn_date >= from_date)

        txns_result = await db.execute(txn_stmt)
        txns        = txns_result.scalars().all()

        # Aggregate qty_in (purchases/opening/adjustments > 0)
        # and qty_out (sales/adjustments < 0)
        qty_in  = Decimal("0")
        qty_out = Decimal("0")
        lots    = []

        for t in txns:
            qty = Decimal(str(t.qty))
            if qty > 0:
                qty_in += qty
                if t.purchase_rate is not None and t.lot_remaining is not None:
                    lots.append({
                        "qty_remaining": t.lot_remaining,
                        "purchase_rate": t.purchase_rate,
                    })
            elif qty < 0:
                qty_out += abs(qty)

        valuation = fifo_valuation(lots)

        report.append({
            "category":    stock.category.value,
            "purity":      stock.purity or "—",
            "description": stock.description,
            "unit":        stock.unit.value,
            "qty_in":      float(qty_in),      # Issue 12 — was null/dash
            "qty_out":     float(qty_out),     # Issue 12 — was null/dash
            "qty_on_hand": float(stock.qty_on_hand),
            "avg_rate":    float(valuation["avg_rate"]),
            "total_value": float(valuation["total_value"]),
        })

    return {
        "as_of":       cutoff.isoformat(),
        "from_date":   from_date.isoformat() if from_date else "All time",
        "rows":        report,
        "grand_total": sum(r["total_value"] for r in report),
    }


# ── Section 269ST Violation Register ─────────────────────────
# Issue 11 fix — customer_name and customer_pan now fetched from Customer master
# P11 addition — this endpoint replaces the TCS register in the Reports tab

@router.get("/section-269st")
async def section_269st_register(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """
    Section 269ST Violation Register.
    Lists all cash payment transactions of ₹2,00,000 or more.

    Section 269ST of the Income Tax Act, 1961 prohibits receipt of cash
    of ₹2,00,000 or more in a single transaction.
    Penalty under Section 271DA: equal to the amount received in violation.
    """
    tenant_id = payload["tenant_id"]
    threshold = Decimal("200000")

    stmt = (
        select(Payment)
        .where(
            Payment.tenant_id == tenant_id,
            Payment.pay_mode  == "Cash",
            Payment.amount    >= threshold,
        )
        .order_by(Payment.payment_date.desc())
    )
    if from_date:
        stmt = stmt.where(Payment.payment_date >= from_date)
    if to_date:
        stmt = stmt.where(Payment.payment_date <= to_date)

    result   = await db.execute(stmt)
    payments = result.scalars().all()

    # Build invoice number map
    inv_nos: dict[int, str] = {}
    for p in payments:
        if p.invoice_id and p.invoice_id not in inv_nos:
            inv = await db.get(Invoice, p.invoice_id)
            inv_nos[p.invoice_id] = inv.invoice_no if inv else "—"

    rows = []
    for p in payments:
        # Issue 11: fetch customer name + PAN from Customer master
        cust       = await db.get(Customer, (p.customer_mobile, tenant_id))
        cust_name  = (cust.name if cust else None) or getattr(p, "customer_name", None) or "—"
        cust_pan   = (cust.pan  if cust else None)

        rows.append({
            "payment_id":      p.id,
            "payment_date":    p.payment_date.isoformat(),
            "invoice_no":      inv_nos.get(p.invoice_id, "—"),
            "customer_mobile": p.customer_mobile,
            "customer_name":   cust_name,           # Issue 11
            "customer_pan":    cust_pan,             # Issue 11
            "amount":          float(p.amount),
            "reference_no":    p.reference_no or "—",
            "notes":           p.notes or "—",
            "section":         "269ST",
            "penalty_risk":    float(p.amount),
        })

    return {
        "rows":               rows,
        "total_violations":   len(rows),
        "total_amount":       sum(r["amount"]       for r in rows),
        "total_penalty_risk": sum(r["penalty_risk"] for r in rows),
        "threshold":          float(threshold),
        "note": (
            "Section 269ST of Income Tax Act, 1961: No person shall receive "
            "₹2,00,000 or more in cash in a single transaction. "
            "Penalty u/s 271DA: Amount equal to cash received."
        ),
    }


# ── TCS Register (legacy — kept for backward compatibility) ───

@router.get("/tcs")
async def tcs_register(
    payload: dict         = Depends(get_current_user_payment := get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """
    TCS register — returns empty dataset as TCS has been removed (P11).
    Endpoint kept so any cached frontend calls do not 404.
    """
    fy_start, fy_end = current_fy()
    return {
        "rows":                [],
        "total_tcs_collected": 0,
        "total_taxable_value": 0,
        "fy":                  f"FY {fy_start.year}-{str(fy_start.year+1)[2:]}",
        "note":                "TCS collection removed as of P11. See Section 269ST report.",
    }
