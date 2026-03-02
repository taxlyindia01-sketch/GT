# routers/reports.py — All business reports: Sales, TCS, SFT, GSTR-1, Account, Cash, FIFO

from datetime import date
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from models import Invoice, InvoiceItem, Customer, Payment, CashEntry, StockItem, StockTransaction
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
    PAN is mandatory for these customers.
    """
    tenant_id = payload["tenant_id"]

    result = await db.execute(
        select(Customer).where(
            Customer.tenant_id  == tenant_id,
            Customer.sft_flagged == True,
        )
    )
    customers = result.scalars().all()

    rows = [{
        "customer_name":   c.name,
        "mobile":          c.mobile,
        "pan":             c.pan or None,
        "cash_receipts_fy":float(c.cash_receipts_fy),
        "sft_threshold":   float(SFT_THRESHOLD),
        "pan_missing":     not c.pan,
        "status":          "PAN Required" if not c.pan else "Flag for SFT",
    } for c in customers]

    return {
        "rows":                  rows,
        "total_flagged":         len(rows),
        "total_pan_missing":     sum(1 for r in rows if r["pan_missing"]),
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
    as_of:   Optional[date] = Query(None),
    payload: dict           = Depends(get_current_user_payload),
    db:      AsyncSession   = Depends(get_db),
):
    """
    FIFO stock valuation report.
    Polish Charges are excluded.
    Returns: per-item avg cost and total inventory value.
    """
    tenant_id = payload["tenant_id"]
    cutoff = as_of or date.today()

    stocks_result = await db.execute(
        select(StockItem).where(
            StockItem.tenant_id   == tenant_id,
            StockItem.category    != "Polish Charges",
            StockItem.is_active   == True,
        )
    )
    stocks = stocks_result.scalars().all()

    report = []
    for stock in stocks:
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
        report.append({
            "category":    stock.category.value,
            "purity":      stock.purity or "—",
            "description": stock.description,
            "unit":        stock.unit.value,
            "qty_on_hand": float(stock.qty_on_hand),
            "avg_rate":    float(valuation["avg_rate"]),
            "total_value": float(valuation["total_value"]),
        })

    return {
        "as_of":       cutoff.isoformat(),
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


# ── Section 269ST Register ────────────────────────────────────

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
    of ₹2,00,000 or more:
      (a) in aggregate from a person in a day; or
      (b) in respect of a single transaction; or
      (c) in respect of transactions relating to one event/occasion from a person.

    Penalty under Section 271DA: equal to the amount received in violation.
    """
    from decimal import Decimal
    from sqlalchemy import select

    tenant_id      = payload["tenant_id"]
    threshold      = Decimal("200000")

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

    # Build invoice number map for reference
    inv_nos: dict[int, str] = {}
    for p in payments:
        if p.invoice_id and p.invoice_id not in inv_nos:
            inv = await db.get(Invoice, p.invoice_id)
            inv_nos[p.invoice_id] = inv.invoice_no if inv else "—"

    # Fetch customer PAN for each row
    from models import Customer as CustomerModel
    rows = []
    for p in payments:
        cust = await db.get(CustomerModel, (p.customer_mobile, tenant_id))
        rows.append({
            "payment_id":     p.id,
            "payment_date":   p.payment_date.isoformat(),
            "invoice_no":     inv_nos.get(p.invoice_id, "—"),
            "customer_mobile":p.customer_mobile,
            "customer_name":  (cust.name if cust else "—"),
            "customer_pan":   (cust.pan  if cust else None),
            "amount":         float(p.amount),
            "reference_no":   p.reference_no or "—",
            "notes":          p.notes or "—",
            "section":        "269ST",
            "penalty_risk":   float(p.amount),   # penalty = amount received
        })

    return {
        "rows":                rows,
        "total_violations":    len(rows),
        "total_amount":        sum(r["amount"]       for r in rows),
        "total_penalty_risk":  sum(r["penalty_risk"] for r in rows),
        "threshold":           float(threshold),
        "note": (
            "Section 269ST of Income Tax Act, 1961: No person shall receive "
            "₹2,00,000 or more in cash in a single transaction. "
            "Penalty u/s 271DA: Amount equal to cash received."
        ),
    }
