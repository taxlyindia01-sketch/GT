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
    CashEntry, StockItem, StockTransaction, SupplierInvoice,
    InvoiceStatus,
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
    Comprehensive FIFO report — three sections:
      • invoice_movements  : every stock transaction linked to its invoice (IN/OUT)
      • category_summary   : category-wise qty & value totals
      • rows (valuation)   : per-item FIFO stock valuation with avg rate & total value
    Polish Charges excluded. Supports date range filter.
    """
    tenant_id = payload["tenant_id"]
    cutoff    = as_of or to_date or date.today()

    stocks_result = await db.execute(
        select(StockItem).where(
            StockItem.tenant_id == tenant_id,
            StockItem.category  != "Polish Charges",
            StockItem.is_active == True,
        ).order_by(StockItem.category, StockItem.purity, StockItem.description)
    )
    stocks = stocks_result.scalars().all()

    valuation_rows    = []   # one row per stock item  (existing "rows" key preserved)
    invoice_movements = []   # one row per transaction
    category_map      = {}   # category → running totals

    for stock in stocks:
        cat  = stock.category.value if hasattr(stock.category, "value") else str(stock.category)
        unit = stock.unit.value     if hasattr(stock.unit,     "value") else str(stock.unit)

        txn_stmt = (
            select(StockTransaction)
            .where(
                StockTransaction.stock_item_id == stock.id,
                StockTransaction.txn_date      <= cutoff,
            )
            .order_by(StockTransaction.txn_date, StockTransaction.id)
        )
        if from_date:
            txn_stmt = txn_stmt.where(StockTransaction.txn_date >= from_date)

        txns_result = await db.execute(txn_stmt)
        txns        = txns_result.scalars().all()

        qty_in  = Decimal("0")
        qty_out = Decimal("0")
        lots    = []

        for t in txns:
            qty      = Decimal(str(t.qty))
            txn_type = t.txn_type.value if hasattr(t.txn_type, "value") else str(t.txn_type)
            rate     = float(t.purchase_rate) if t.purchase_rate else 0.0

            # Resolve invoice number and party name
            inv_no = "—"; party = "—"
            if t.invoice_id:
                if qty > 0:
                    # Stock IN → purchase invoice (SupplierInvoice)
                    sinv = await db.get(SupplierInvoice, t.invoice_id)
                    if sinv:
                        inv_no = sinv.invoice_no or f"SINV-{sinv.id}"
                        party  = sinv.supplier_name or sinv.supplier_mobile
                else:
                    # Stock OUT → sales invoice
                    cinv = await db.get(Invoice, t.invoice_id)
                    if cinv:
                        inv_no = cinv.invoice_no or f"INV-{cinv.id}"
                        party  = cinv.customer_name or cinv.customer_mobile

            direction = "IN" if qty > 0 else "OUT"
            qty_abs   = abs(float(qty))
            value_in  = round(qty_abs * rate, 2) if qty > 0 else 0.0
            value_out = round(qty_abs * rate, 2) if qty < 0 else 0.0

            invoice_movements.append({
                "date":        t.txn_date.isoformat(),
                "direction":   direction,
                "txn_type":    txn_type,
                "invoice_no":  inv_no,
                "party":       party,
                "category":    cat,
                "purity":      stock.purity or "—",
                "description": stock.description,
                "unit":        unit,
                "qty":         qty_abs,
                "rate":        rate if qty > 0 else 0.0,
                "value_in":    value_in,
                "value_out":   value_out,
                "reason":      t.reason or "",
            })

            if qty > 0:
                qty_in += qty
                if t.purchase_rate is not None and t.lot_remaining is not None:
                    lots.append({
                        "qty_remaining": t.lot_remaining,
                        "purchase_rate": t.purchase_rate,
                    })
            else:
                qty_out += abs(qty)

        valuation = fifo_valuation(lots)

        valuation_rows.append({
            "category":    cat,
            "purity":      stock.purity or "—",
            "description": stock.description,
            "unit":        unit,
            "qty_in":      float(qty_in),
            "qty_out":     float(qty_out),
            "qty_on_hand": float(stock.qty_on_hand),
            "avg_rate":    float(valuation["avg_rate"]),
            "total_value": float(valuation["total_value"]),
        })

        if cat not in category_map:
            category_map[cat] = {"qty_in": Decimal("0"), "qty_out": Decimal("0"),
                                  "qty_on_hand": Decimal("0"), "total_value": Decimal("0")}
        category_map[cat]["qty_in"]      += qty_in
        category_map[cat]["qty_out"]     += qty_out
        category_map[cat]["qty_on_hand"] += stock.qty_on_hand
        category_map[cat]["total_value"] += Decimal(str(valuation["total_value"]))

    category_summary = [
        {
            "category":    cat,
            "qty_in":      float(v["qty_in"]),
            "qty_out":     float(v["qty_out"]),
            "qty_on_hand": float(v["qty_on_hand"]),
            "total_value": float(v["total_value"]),
        }
        for cat, v in sorted(category_map.items())
    ]

    grand_total = sum(r["total_value"] for r in valuation_rows)

    return {
        "as_of":             cutoff.isoformat(),
        "from_date":         from_date.isoformat() if from_date else "All time",
        "rows":              valuation_rows,      # preserved for backwards compat
        "invoice_movements": invoice_movements,
        "category_summary":  category_summary,
        "grand_total":       grand_total,
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


# ── 269ST Register — replaces legacy TCS tab ─────────────────
# The Reports tab's "269ST" tab calls /api/reports/tcs for backwards compat.
# Returns the Section 269ST cash-violation register (same data as /section-269st
# but served at /tcs so the frontend URL mapping doesn't need changing).

@router.get("/tcs")
async def section_269st_via_tcs(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """
    Section 269ST Violation Register served at /tcs (used by the 269ST report tab).
    Lists all individual CASH PAYMENTS of Rs.2,00,000 or more (payment-level check).
    Section 269ST, Income Tax Act 1961: No person shall receive Rs.2L+ in cash in
    a single transaction. Penalty u/s 271DA = amount received in violation.
    """
    tenant_id = payload["tenant_id"]
    threshold = Decimal("200000")

    # 269ST checked at PAYMENT level — each individual cash receipt >= 2L is a violation
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

    inv_cache: dict[int, Invoice] = {}
    for p in payments:
        if p.invoice_id and p.invoice_id not in inv_cache:
            inv_obj = await db.get(Invoice, p.invoice_id)
            if inv_obj:
                inv_cache[p.invoice_id] = inv_obj

    rows = []
    for p in payments:
        inv_obj = inv_cache.get(p.invoice_id)
        cust    = await db.get(Customer, (p.customer_mobile, tenant_id))
        cname   = (inv_obj.customer_name if inv_obj else None) or (cust.name if cust else "—")
        pan     = (inv_obj.customer_pan  if inv_obj else None) or (cust.pan  if cust else None)
        rows.append({
            "payment_date":    p.payment_date.isoformat(),
            "invoice_date":    inv_obj.invoice_date.isoformat() if inv_obj else p.payment_date.isoformat(),
            "invoice_no":      inv_obj.invoice_no if inv_obj else "—",
            "customer_mobile": p.customer_mobile,
            "customer_name":   cname,
            "customer_pan":    pan,
            "cash_amount":     float(p.amount),
            "penalty_risk":    float(p.amount),
            "reference_no":    p.reference_no or "—",
            "section":         "269ST",
        })

    return {
        "rows":               rows,
        "total_violations":   len(rows),
        "total_cash_amount":  sum(r["cash_amount"] for r in rows),
        "total_penalty_risk": sum(r["penalty_risk"] for r in rows),
        "threshold":          float(threshold),
        "note": (
            "Section 269ST — No person shall receive Rs.2,00,000 or more in cash "
            "in a single transaction. Penalty u/s 271DA = amount received in violation."
        ),
    }


# ── Account Register ──────────────────────────────────────────
# New report: one row per invoice with category-wise item totals
# Columns: Invoice Date, Invoice No, Customer Name, Customer Mobile,
#          Gold, Silver, Diamond, Polish Charges, Making Charges,
#          CGST Amount, SGST Amount, IGST Amount, Grand Total

@router.get("/account")
async def account_register(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """
    Account Register — one row per invoice, with amounts broken out by
    category (Gold / Silver / Diamond / Polish Charges) plus Making Charges,
    GST components, and Grand Total.
    """
    tenant_id = payload["tenant_id"]
    stmt = (
        select(Invoice)
        .where(Invoice.tenant_id == tenant_id, Invoice.status == "active")
        .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
    )
    if from_date:
        stmt = stmt.where(Invoice.invoice_date >= from_date)
    if to_date:
        stmt = stmt.where(Invoice.invoice_date <= to_date)

    result   = await db.execute(stmt)
    invoices = result.scalars().all()

    rows = []
    total_gold    = Decimal("0")
    total_silver  = Decimal("0")
    total_diamond = Decimal("0")
    total_polish  = Decimal("0")
    total_making  = Decimal("0")
    total_cgst    = Decimal("0")
    total_sgst    = Decimal("0")
    total_igst    = Decimal("0")
    total_grand   = Decimal("0")

    for inv in invoices:
        # Fetch all line items for this invoice
        items_result = await db.execute(
            select(InvoiceItem).where(InvoiceItem.invoice_id == inv.id)
        )
        items = items_result.scalars().all()

        # Aggregate amounts by category (amount = qty*rate; making_charges separate)
        gold_amt    = Decimal("0")
        silver_amt  = Decimal("0")
        diamond_amt = Decimal("0")
        polish_amt  = Decimal("0")
        making_total = Decimal("0")

        for item in items:
            cat = item.category.value  # "Gold" / "Silver" / "Diamond" / "Polish Charges"
            # item.amount includes making_charges already; subtract to get pure item value
            item_base = item.amount - item.making_charges
            making_total += item.making_charges

            if cat == "Gold":
                gold_amt    += item_base
            elif cat == "Silver":
                silver_amt  += item_base
            elif cat == "Diamond":
                diamond_amt += item_base
            elif cat == "Polish Charges":
                polish_amt  += item_base   # typically qty*rate for polish

        total_gold    += gold_amt
        total_silver  += silver_amt
        total_diamond += diamond_amt
        total_polish  += polish_amt
        total_making  += making_total
        total_cgst    += inv.cgst
        total_sgst    += inv.sgst
        total_igst    += inv.igst
        total_grand   += inv.grand_total

        rows.append({
            "invoice_date":    inv.invoice_date.isoformat(),
            "invoice_no":      inv.invoice_no,
            "customer_name":   inv.customer_name,
            "customer_mobile": inv.customer_mobile,
            "gold":            float(gold_amt),
            "silver":          float(silver_amt),
            "diamond":         float(diamond_amt),
            "polish_charges":  float(polish_amt),
            "making_charges":  float(making_total),
            "cgst":            float(inv.cgst),
            "sgst":            float(inv.sgst),
            "igst":            float(inv.igst),
            "grand_total":     float(inv.grand_total),
        })

    return {
        "rows": rows,
        "totals": {
            "gold":           float(total_gold),
            "silver":         float(total_silver),
            "diamond":        float(total_diamond),
            "polish_charges": float(total_polish),
            "making_charges": float(total_making),
            "cgst":           float(total_cgst),
            "sgst":           float(total_sgst),
            "igst":           float(total_igst),
            "grand_total":    float(total_grand),
        },
    }


# ── Customer Account Report ───────────────────────────────────

@router.get("/customer-account")
async def customer_account_report(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Customer Account Report — same format as /account (alias for front-end tab)."""
    tenant_id = payload["tenant_id"]
    stmt = (
        select(Invoice)
        .where(Invoice.tenant_id == tenant_id, Invoice.status == "active")
        .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
    )
    if from_date: stmt = stmt.where(Invoice.invoice_date >= from_date)
    if to_date:   stmt = stmt.where(Invoice.invoice_date <= to_date)
    result   = await db.execute(stmt)
    invoices = result.scalars().all()

    rows = []
    tot  = {k: Decimal("0") for k in ["gold","silver","diamond","polish","making","cgst","sgst","igst","grand"]}

    for inv in invoices:
        items_r = await db.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == inv.id))
        items   = items_r.scalars().all()
        gold = silver = diamond = polish = making = Decimal("0")
        for item in items:
            base = item.amount - item.making_charges
            making += item.making_charges
            cat = item.category.value
            if cat == "Gold":            gold    += base
            elif cat == "Silver":        silver  += base
            elif cat == "Diamond":       diamond += base
            elif cat == "Polish Charges": polish += base
        for k, v in [("gold",gold),("silver",silver),("diamond",diamond),("polish",polish),
                     ("making",making),("cgst",inv.cgst),("sgst",inv.sgst),("igst",inv.igst),("grand",inv.grand_total)]:
            tot[k] += v
        rows.append({
            "invoice_date": inv.invoice_date.isoformat(), "invoice_no": inv.invoice_no,
            "customer_name": inv.customer_name, "customer_mobile": inv.customer_mobile,
            "gold": float(gold), "silver": float(silver), "diamond": float(diamond),
            "making": float(making), "cgst": float(inv.cgst), "sgst": float(inv.sgst),
            "igst": float(inv.igst), "grand_total": float(inv.grand_total),
        })
    return {"rows": rows, "totals": {k: float(v) for k, v in tot.items()}}


# ── Supplier Account Report ───────────────────────────────────

@router.get("/supplier-account")
async def supplier_account_report(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Supplier Account Report — mirrors Customer Account Report format for purchase invoices."""
    from models import SupplierInvoice, SupplierInvoiceItem
    tenant_id = payload["tenant_id"]
    stmt = (
        select(SupplierInvoice)
        .where(SupplierInvoice.tenant_id == tenant_id, SupplierInvoice.status == "active")
        .order_by(SupplierInvoice.invoice_date.desc(), SupplierInvoice.id.desc())
    )
    if from_date: stmt = stmt.where(SupplierInvoice.invoice_date >= from_date)
    if to_date:   stmt = stmt.where(SupplierInvoice.invoice_date <= to_date)
    result   = await db.execute(stmt)
    invoices = result.scalars().all()

    rows = []
    tot  = {k: Decimal("0") for k in ["gold","silver","diamond","polish","making","cgst","sgst","igst","grand","amount_paid","outstanding"]}

    for inv in invoices:
        items_r = await db.execute(select(SupplierInvoiceItem).where(SupplierInvoiceItem.invoice_id == inv.id))
        items   = items_r.scalars().all()
        gold = silver = diamond = polish = making = Decimal("0")
        for item in items:
            base = (item.amount or Decimal("0")) - (item.making_charges or Decimal("0"))
            making += item.making_charges or Decimal("0")
            cat = item.category.value if hasattr(item.category,"value") else str(item.category)
            if cat == "Gold":            gold    += base
            elif cat == "Silver":        silver  += base
            elif cat == "Diamond":       diamond += base
            elif cat == "Polish Charges": polish += base
        cgst = inv.cgst or Decimal("0")
        sgst = inv.sgst or Decimal("0")
        igst = inv.igst or Decimal("0")
        for k, v in [("gold",gold),("silver",silver),("diamond",diamond),("polish",polish),
                     ("making",making),("cgst",cgst),("sgst",sgst),("igst",igst),
                     ("grand",inv.grand_total),("amount_paid",inv.amount_paid),("outstanding",inv.outstanding)]:
            tot[k] += v
        rows.append({
            "invoice_date": inv.invoice_date.isoformat(),
            "invoice_no": inv.invoice_no, "invoice_number": inv.invoice_no,
            "supplier_name": inv.supplier_name or "", "supplier_mobile": inv.supplier_mobile,
            "gold": float(gold), "silver": float(silver), "diamond": float(diamond),
            "making": float(making), "cgst": float(cgst), "sgst": float(sgst), "igst": float(igst),
            "grand_total": float(inv.grand_total),
            "amount_paid": float(inv.amount_paid), "outstanding": float(inv.outstanding),
        })
    return {"rows": rows, "totals": {k: float(v) for k, v in tot.items()}}


# ── Cancelled Invoices Register ───────────────────────────────

@router.get("/cancelled-invoices")
async def cancelled_invoices_report(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """List all cancelled invoices. Used by the Cancelled tab in Reports."""
    tenant_id = payload["tenant_id"]
    stmt = (
        select(Invoice)
        .where(Invoice.tenant_id == tenant_id, Invoice.status == InvoiceStatus.cancelled)
        .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
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
        "cancelled_at":    None,
        "customer_name":   inv.customer_name,
        "customer_mobile": inv.customer_mobile,
        "customer_pan":    inv.customer_pan,
        "pay_mode":        inv.pay_mode.value,
        "grand_total":     float(inv.grand_total),
        "notes":           inv.notes or "",
    } for inv in invoices]

    return {
        "rows":            rows,
        "total_cancelled": len(rows),
        "total_value":     sum(r["grand_total"] for r in rows),
    }
