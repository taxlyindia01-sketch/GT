# routers/export.py
# Changes vs v4 original:
#  Issue 7/10 — /payments-excel: uses date-filtered payments data (was broken)
#  Issue 8    — /advances-excel: new endpoint for Advances page download
#  P11        — TCS sheet replaced with Section 269ST sheet in full backup

from io import BytesIO
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from database import get_db
from models import (
    Invoice, InvoiceItem, Customer, Payment,
    CashEntry, Advance, StockItem, StockTransaction,
    Supplier, SupplierInvoice, SupplierInvoiceItem, SupplierPayment, SupplierAdvance,
)
from utils.auth import get_current_user_payload
from utils.business import current_fy, SFT_THRESHOLD

router = APIRouter()

# ── Styling ───────────────────────────────────────────────────

GOLD_FILL   = PatternFill("solid", fgColor="C8900A")
HEADER_FONT = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
BODY_FONT   = Font(name="Calibri", size=10)
BORDER      = Border(
    bottom=Side(style="thin", color="E8B840"),
    top=Side(style="thin",    color="E8B840"),
)


def style_header_row(ws, row_num: int, num_cols: int):
    for col in range(1, num_cols + 1):
        cell = ws.cell(row=row_num, column=col)
        cell.fill = GOLD_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")


def auto_col_width(ws):
    for col in ws.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=10)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(max_len + 4, 40)


def add_sheet(wb, title: str, headers: list[str], rows: list[list]) -> None:
    ws = wb.create_sheet(title=title[:31])
    ws.append(headers)
    style_header_row(ws, 1, len(headers))
    for row in rows:
        ws.append(row)
    auto_col_width(ws)

async def add_account_sheet(
    wb, db, tenant_id: int,
    from_date=None, to_date=None,
    invoices_cache=None,
) -> None:
    """Add Account Register sheet to workbook (category-wise invoice breakdown)."""
    from decimal import Decimal
    if invoices_cache is not None:
        invoices = invoices_cache
    else:
        stmt = select(Invoice).where(Invoice.tenant_id == tenant_id, Invoice.status == "active")
        if from_date: stmt = stmt.where(Invoice.invoice_date >= from_date)
        if to_date:   stmt = stmt.where(Invoice.invoice_date <= to_date)
        result   = await db.execute(stmt.order_by(Invoice.invoice_date.desc()))
        invoices = result.scalars().all()

    headers = [
        "Invoice Date", "Invoice No", "Customer Name", "Customer Mobile",
        "Gold (₹)", "Silver (₹)", "Diamond (₹)", "Polish Charges (₹)",
        "Making Charges (₹)", "CGST (₹)", "SGST (₹)", "IGST (₹)", "Grand Total (₹)"
    ]
    ws = wb.create_sheet(title="Account Register")
    ws.append(headers)
    style_header_row(ws, 1, len(headers))

    tot = {k: Decimal("0") for k in ["gold","silver","diamond","polish","making","cgst","sgst","igst","grand"]}

    for inv in invoices:
        items_result = await db.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == inv.id))
        items = items_result.scalars().all()
        gold_amt = silver_amt = diamond_amt = polish_amt = making_total = Decimal("0")
        for item in items:
            cat = item.category.value
            item_base = item.amount - item.making_charges
            making_total += item.making_charges
            if cat == "Gold":             gold_amt    += item_base
            elif cat == "Silver":         silver_amt  += item_base
            elif cat == "Diamond":        diamond_amt += item_base
            elif cat == "Polish Charges": polish_amt  += item_base

        tot["gold"]    += gold_amt;    tot["silver"]  += silver_amt
        tot["diamond"] += diamond_amt; tot["polish"]  += polish_amt
        tot["making"]  += making_total; tot["cgst"]   += inv.cgst
        tot["sgst"]    += inv.sgst;    tot["igst"]    += inv.igst
        tot["grand"]   += inv.grand_total

        ws.append([
            inv.invoice_date.isoformat(), inv.invoice_no,
            inv.customer_name, inv.customer_mobile,
            float(gold_amt), float(silver_amt), float(diamond_amt), float(polish_amt),
            float(making_total),
            float(inv.cgst), float(inv.sgst), float(inv.igst), float(inv.grand_total),
        ])

    total_row_vals = [
        "TOTAL", "", "", "",
        float(tot["gold"]), float(tot["silver"]), float(tot["diamond"]), float(tot["polish"]),
        float(tot["making"]), float(tot["cgst"]), float(tot["sgst"]), float(tot["igst"]), float(tot["grand"]),
    ]
    ws.append(total_row_vals)
    tr = ws.max_row
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=tr, column=col)
        cell.font    = Font(name="Calibri", bold=True, size=10)
        cell.fill    = PatternFill("solid", fgColor="FFF0CC")
    auto_col_width(ws)


async def add_dashboard_sheet(
    wb, db, tenant_id: int,
    from_date=None, to_date=None,
    invoices_cache=None,
) -> None:
    """Add Dashboard summary sheet to workbook."""
    from decimal import Decimal
    from datetime import date as date_type

    if invoices_cache is not None:
        invoices = invoices_cache
    else:
        stmt = select(Invoice).where(Invoice.tenant_id == tenant_id, Invoice.status == "active")
        if from_date: stmt = stmt.where(Invoice.invoice_date >= from_date)
        if to_date:   stmt = stmt.where(Invoice.invoice_date <= to_date)
        result   = await db.execute(stmt.order_by(Invoice.invoice_date.desc()))
        invoices = result.scalars().all()

    ws = wb.create_sheet(title="Dashboard")
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 22

    def kv(label, value):
        r = ws.max_row + 1
        ws.cell(r, 1, label).font = Font(name="Calibri", bold=True, size=10)
        cell = ws.cell(r, 2, value)
        cell.font = Font(name="Calibri", size=10)
        return cell

    def section(title):
        ws.append([])
        r = ws.max_row + 1
        c = ws.cell(r, 1, title)
        c.font = Font(name="Calibri", bold=True, size=12, color="C8900A")
        c.fill = PatternFill("solid", fgColor="FFF8E1")
        ws.cell(r, 2).fill = PatternFill("solid", fgColor="FFF8E1")

    today_dt = date_type.today()
    section("📊 Report Summary")
    kv("Generated On", today_dt.isoformat())
    kv("Period From",  from_date.isoformat() if from_date else "All time")
    kv("Period To",    to_date.isoformat()   if to_date   else today_dt.isoformat())
    kv("Total Invoices", len(invoices))

    # Financial summary
    total_subtotal  = sum(float(i.subtotal)    for i in invoices)
    total_cgst      = sum(float(i.cgst)        for i in invoices)
    total_sgst      = sum(float(i.sgst)        for i in invoices)
    total_igst      = sum(float(i.igst)        for i in invoices)
    total_gst       = total_cgst + total_sgst + total_igst
    total_grand     = sum(float(i.grand_total) for i in invoices)
    total_collected = sum(float(i.amount_paid) for i in invoices)
    total_outstanding = sum(float(i.outstanding) for i in invoices)

    section("💰 Financial Summary")
    kv("Total Subtotal (Taxable Value)",    f"Rs. {total_subtotal:,.2f}")
    kv("Total CGST",                        f"Rs. {total_cgst:,.2f}")
    kv("Total SGST",                        f"Rs. {total_sgst:,.2f}")
    kv("Total IGST",                        f"Rs. {total_igst:,.2f}")
    kv("Total GST",                         f"Rs. {total_gst:,.2f}")
    kv("Grand Total (All Invoices)",        f"Rs. {total_grand:,.2f}")
    kv("Total Collected (Payments)",        f"Rs. {total_collected:,.2f}")
    kv("Total Outstanding",                 f"Rs. {total_outstanding:,.2f}")

    # Payment mode breakdown
    from collections import Counter
    mode_totals: dict[str, float] = {}
    for inv in invoices:
        m = inv.pay_mode.value
        mode_totals[m] = mode_totals.get(m, 0) + float(inv.grand_total)

    section("💳 Sales by Payment Mode")
    for mode, amt in sorted(mode_totals.items(), key=lambda x: -x[1]):
        kv(mode, f"Rs. {amt:,.2f}")

    # Category breakdown (needs items)
    cat_totals: dict[str, float] = {"Gold": 0, "Silver": 0, "Diamond": 0, "Polish Charges": 0, "Making Charges": 0}
    for inv in invoices:
        items_result = await db.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == inv.id))
        for item in items_result.scalars():
            cat = item.category.value
            base = float(item.amount - item.making_charges)
            if cat in cat_totals: cat_totals[cat] += base
            cat_totals["Making Charges"] += float(item.making_charges)

    section("🏷️ Sales by Category")
    for cat, amt in cat_totals.items():
        if amt > 0: kv(cat, f"Rs. {amt:,.2f}")

    # 269ST violations
    violations = [inv for inv in invoices if inv.pay_mode.value == "Cash" and float(inv.grand_total) >= 200000]
    section("⚠️ Section 269ST Compliance")
    kv("Threshold",                   "Rs. 2,00,000 per transaction")
    kv("Cash Invoices ≥ ₹2L (Violations)", len(violations))
    kv("Total Cash Violation Amount", f"Rs. {sum(float(i.grand_total) for i in violations):,.2f}")

    # Outstanding summary
    unpaid  = [i for i in invoices if i.payment_status.value == "unpaid"]
    partial = [i for i in invoices if i.payment_status.value == "partial"]
    paid    = [i for i in invoices if i.payment_status.value == "paid"]

    section("📋 Invoice Status")
    kv("Paid",    len(paid))
    kv("Partial", len(partial))
    kv("Unpaid",  len(unpaid))
    kv("Outstanding (Unpaid + Partial)", f"Rs. {total_outstanding:,.2f}")


def _stream_workbook(wb, filename: str) -> StreamingResponse:
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── Full Backup (15 sheets) ───────────────────────────────────

@router.get("/excel")
async def export_full_backup(
    tenant_id: Optional[int] = Query(None),
    payload:   dict          = Depends(get_current_user_payload),
    db:        AsyncSession  = Depends(get_db),
):
    """
    Full Excel backup — 15 sheets.
    P11: TCS sheet replaced with Section 269ST violations sheet.
    """
    tid = tenant_id or payload["tenant_id"]
    wb  = openpyxl.Workbook()
    wb.remove(wb.active)

    fy_start, fy_end = current_fy()

    # ── Sheet 1: Invoices ─────────────────────────────────────
    result   = await db.execute(select(Invoice).where(Invoice.tenant_id == tid).order_by(Invoice.invoice_date.desc()))
    invoices = result.scalars().all()

    add_sheet(wb, "Invoices", [
        "Invoice No", "Date", "Customer Name", "Mobile", "PAN", "Pay Mode",
        "Subtotal", "CGST", "SGST", "IGST", "Grand Total", "Paid", "Outstanding", "Status"
    ], [
        [inv.invoice_no, inv.invoice_date.isoformat(), inv.customer_name, inv.customer_mobile,
         inv.customer_pan or "", inv.pay_mode.value, float(inv.subtotal),
         float(inv.cgst), float(inv.sgst), float(inv.igst),
         float(inv.grand_total), float(inv.amount_paid), float(inv.outstanding), inv.status.value]
        for inv in invoices
    ])

    # ── Sheet 2: Invoice Items ────────────────────────────────
    result = await db.execute(select(InvoiceItem).where(InvoiceItem.tenant_id == tid))
    items  = result.scalars().all()
    inv_no_map = {inv.id: inv.invoice_no for inv in invoices}

    add_sheet(wb, "Invoice_Items", [
        "Invoice No", "Category", "Purity", "Description", "HSN", "Qty", "Unit", "Rate", "Making", "Amount"
    ], [
        [inv_no_map.get(i.invoice_id, ""), i.category.value, i.purity or "", i.description,
         i.hsn_code, float(i.qty), i.unit.value, float(i.rate), float(i.making_charges), float(i.amount)]
        for i in items
    ])

    # ── Sheet 3: Payments ─────────────────────────────────────
    result   = await db.execute(select(Payment).where(Payment.tenant_id == tid).order_by(Payment.payment_date.desc()))
    payments = result.scalars().all()

    add_sheet(wb, "Payments", [
        "ID", "Invoice No", "Customer Name", "Customer Mobile", "Amount", "Date", "Mode", "Reference"
    ], [
        [p.id, inv_no_map.get(p.invoice_id, ""),
         getattr(p, "customer_name", "") or "",
         p.customer_mobile,
         float(p.amount), p.payment_date.isoformat(), p.pay_mode.value, p.reference_no or ""]
        for p in payments
    ])

    # ── Sheet 4: Customers ────────────────────────────────────
    result    = await db.execute(select(Customer).where(Customer.tenant_id == tid).order_by(Customer.name))
    customers = result.scalars().all()

    add_sheet(wb, "Customers", [
        "Mobile (PK)", "Name", "PAN", "State", "GSTIN", "Address", "Cash Receipts FY", "SFT Flagged"
    ], [
        [c.mobile, c.name, c.pan or "", c.state, c.gstin or "", c.address or "",
         float(c.cash_receipts_fy), "Yes" if c.sft_flagged else "No"]
        for c in customers
    ])

    # ── Sheet 5: Stock Items ──────────────────────────────────
    result = await db.execute(select(StockItem).where(StockItem.tenant_id == tid))
    stocks = result.scalars().all()

    add_sheet(wb, "Stock_Items", [
        "ID", "Category", "Purity", "Description", "Unit", "Qty on Hand"
    ], [
        [s.id, s.category.value, s.purity or "", s.description, s.unit.value, float(s.qty_on_hand)]
        for s in stocks
    ])

    # ── Sheet 6: Cash Register ────────────────────────────────
    result  = await db.execute(select(CashEntry).where(CashEntry.tenant_id == tid).order_by(CashEntry.entry_date.desc()))
    entries = result.scalars().all()

    add_sheet(wb, "Cash_Register", [
        "Date", "Type", "Description", "Amount", "Bank Reference"
    ], [
        [e.entry_date.isoformat(), e.entry_type.value, e.description,
         float(e.amount), e.bank_reference or ""]
        for e in entries
    ])

    # ── Sheet 7: Advances ─────────────────────────────────────
    result   = await db.execute(select(Advance).where(Advance.tenant_id == tid))
    advances = result.scalars().all()

    add_sheet(wb, "Advances", [
        "ID", "Customer Name", "Customer Mobile", "Amount", "Remaining", "Date", "Mode", "Notes"
    ], [
        [a.id,
         getattr(a, "customer_name", "") or "",
         a.customer_mobile,
         float(a.amount), float(a.remaining),
         a.advance_date.isoformat(), a.pay_mode.value, a.notes or ""]
        for a in advances
    ])

    # ── Sheet 8: Stock Transactions ───────────────────────────
    result = await db.execute(select(StockTransaction).where(StockTransaction.tenant_id == tid).order_by(StockTransaction.txn_date.desc()))
    txns   = result.scalars().all()

    add_sheet(wb, "Stock_Transactions", [
        "ID", "Stock Item ID", "Type", "Qty", "Purchase Rate", "Date", "Reason"
    ], [
        [t.id, t.stock_item_id, t.txn_type.value, float(t.qty),
         float(t.purchase_rate) if t.purchase_rate else "", t.txn_date.isoformat(), t.reason or ""]
        for t in txns
    ])

    # ── Report Sheet 9: Sales Register ───────────────────────
    fy_invoices = [inv for inv in invoices if fy_start <= inv.invoice_date <= fy_end and inv.status.value == "active"]

    add_sheet(wb, "Report_Sales", [
        "Invoice No", "Date", "Customer", "Mobile", "PAN", "HSN",
        "Subtotal", "CGST", "SGST", "IGST", "Grand Total", "Mode"
    ], [
        [inv.invoice_no, inv.invoice_date.isoformat(), inv.customer_name, inv.customer_mobile,
         inv.customer_pan or "", "7113", float(inv.subtotal),
         float(inv.cgst), float(inv.sgst), float(inv.igst),
         float(inv.grand_total), inv.pay_mode.value]
        for inv in fy_invoices
    ])

    # ── Report Sheet 10: Section 269ST (replaces TCS — P11) ───
    thresh_269st = Decimal("200000")
    viol_payments = [
        p for p in payments
        if p.pay_mode.value == "Cash" and p.amount >= thresh_269st
        and fy_start <= p.payment_date <= fy_end
    ]

    add_sheet(wb, "Report_Sec269ST", [
        "Date", "Invoice No", "Customer Name", "Mobile", "PAN", "Cash Amount", "Penalty Risk", "Reference"
    ], [])

    ws_269 = wb["Report_Sec269ST"]
    for p in viol_payments:
        cust  = await db.get(Customer, (p.customer_mobile, tid))
        cname = (getattr(p, "customer_name", None) or (cust.name if cust else "—"))
        cpan  = cust.pan if cust else ""
        ws_269.append([
            p.payment_date.isoformat(),
            inv_no_map.get(p.invoice_id, "—"),
            cname, p.customer_mobile, cpan or "MISSING",
            float(p.amount), float(p.amount), p.reference_no or "",
        ])
    auto_col_width(ws_269)

    # ── Report Sheet 11: SFT Register ────────────────────────
    sft_customers = [c for c in customers if c.sft_flagged]

    add_sheet(wb, "Report_SFT", [
        "Customer", "Mobile", "PAN", "Cash Receipts FY", "SFT Threshold", "PAN Missing"
    ], [
        [c.name, c.mobile, c.pan or "", float(c.cash_receipts_fy),
         float(SFT_THRESHOLD), "YES" if not c.pan else "No"]
        for c in sft_customers
    ])

    # ── Report Sheet 12: GSTR-1 ───────────────────────────────
    add_sheet(wb, "Report_GSTR1", [
        "Invoice No", "Date", "Customer", "GSTIN", "State", "HSN",
        "Taxable", "CGST%", "CGST", "SGST%", "SGST", "Total"
    ], [
        [inv.invoice_no, inv.invoice_date.isoformat(), inv.customer_name,
         inv.customer_gstin or "Unregistered", inv.customer_state or "",
         "7113", float(inv.subtotal),
         float(inv.gst_rate / 2), float(inv.cgst),
         float(inv.gst_rate / 2), float(inv.sgst),
         float(inv.grand_total)]
        for inv in fy_invoices
    ])

    # ── Report Sheet 13: Outstanding ──────────────────────────
    outstanding_invoices = [inv for inv in invoices if float(inv.outstanding) > 0]

    add_sheet(wb, "Report_Outstanding", [
        "Invoice No", "Date", "Customer", "Mobile", "Grand Total", "Paid", "Outstanding"
    ], [
        [inv.invoice_no, inv.invoice_date.isoformat(), inv.customer_name,
         inv.customer_mobile, float(inv.grand_total), float(inv.amount_paid), float(inv.outstanding)]
        for inv in outstanding_invoices
    ])

    # ── Report Sheet 14: Cash Book ────────────────────────────
    fy_cash_entries = [e for e in entries if fy_start <= e.entry_date <= fy_end]

    add_sheet(wb, "Report_Cash_Book", [
        "Date", "Type", "Description", "Cash In", "Cash Out", "Bank In", "Balance"
    ], [
        [e.entry_date.isoformat(), e.entry_type.value, e.description,
         float(e.amount) if e.entry_type.value == "cash_in"                          else 0,
         float(e.amount) if e.entry_type.value in ("cash_out", "cash_to_bank")       else 0,
         float(e.amount) if e.entry_type.value == "bank_in"                          else 0,
         float(e.running_balance or 0)]
        for e in fy_cash_entries
    ])

    # ── Report Sheet 15: Payments Register ────────────────────
    add_sheet(wb, "Report_Payments", [
        "Date", "Invoice No", "Customer Name", "Mobile", "Amount", "Mode", "Reference"
    ], [
        [p.payment_date.isoformat(),
         inv_no_map.get(p.invoice_id, "—"),
         getattr(p, "customer_name", "") or "",
         p.customer_mobile,
         float(p.amount), p.pay_mode.value, p.reference_no or ""]
        for p in payments
    ])

    filename = f"goldtrader_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return _stream_workbook(wb, filename)


# ── Payments Excel (standalone) ───────────────────────────────
# Issue 7/10 fix — now uses date-range filtered data

@router.get("/payments-excel")
async def export_payments_excel(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Export payment register to Excel with optional date range."""
    tenant_id = payload["tenant_id"]
    stmt = (
        select(Payment)
        .where(Payment.tenant_id == tenant_id)
        .order_by(Payment.payment_date.desc())
    )
    if from_date:
        stmt = stmt.where(Payment.payment_date >= from_date)
    if to_date:
        stmt = stmt.where(Payment.payment_date <= to_date)

    result   = await db.execute(stmt)
    payments = result.scalars().all()

    # Build invoice_no map
    inv_result = await db.execute(select(Invoice).where(Invoice.tenant_id == tenant_id))
    inv_no_map = {inv.id: inv.invoice_no for inv in inv_result.scalars().all()}

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Payments"

    headers = ["Date", "Invoice No", "Customer Name", "Mobile", "Amount", "Mode", "Reference"]
    ws.append(headers)
    style_header_row(ws, 1, len(headers))

    for p in payments:
        cname = getattr(p, "customer_name", None)
        if not cname:
            cust  = await db.get(Customer, (p.customer_mobile, tenant_id))
            cname = cust.name if cust else "—"
        ws.append([
            p.payment_date.isoformat(),
            inv_no_map.get(p.invoice_id, "—"),
            cname,
            p.customer_mobile,
            float(p.amount),
            p.pay_mode.value,
            p.reference_no or "",
        ])

    auto_col_width(ws)

    date_range = f"{from_date or 'all'}_{to_date or 'all'}"
    # Add Account Register and Dashboard sheets to every Excel export
    await add_account_sheet(wb, db, tenant_id, from_date, to_date)
    await add_dashboard_sheet(wb, db, tenant_id, from_date, to_date)
    # Move Dashboard to first position, Account Register to second
    wb.move_sheet("Dashboard", offset=-len(wb.sheetnames)+1)
    wb.move_sheet("Account Register", offset=-len(wb.sheetnames)+2)
    return _stream_workbook(wb, f"payment_register_{date_range}.xlsx")


# ── Advances Excel (new) ──────────────────────────────────────
# Issue 8 fix — new endpoint for Advances page "Download Excel" button

@router.get("/advances-excel")
async def export_advances_excel(
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """Export advances register to Excel."""
    tenant_id = payload["tenant_id"]
    result    = await db.execute(
        select(Advance)
        .where(Advance.tenant_id == tenant_id)
        .order_by(Advance.advance_date.desc())
    )
    advances = result.scalars().all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Advances"

    headers = ["Date", "Customer Name", "Mobile", "Amount", "Remaining", "Mode", "Notes"]
    ws.append(headers)
    style_header_row(ws, 1, len(headers))

    for a in advances:
        cname = getattr(a, "customer_name", None)
        if not cname:
            cust  = await db.get(Customer, (a.customer_mobile, tenant_id))
            cname = cust.name if cust else "—"
        ws.append([
            a.advance_date.isoformat(),
            cname,
            a.customer_mobile,
            float(a.amount),
            float(a.remaining),
            a.pay_mode.value,
            a.notes or "",
        ])

    auto_col_width(ws)
    return _stream_workbook(wb, f"advances_register_{date.today().isoformat()}.xlsx")


# ── Sales Excel ───────────────────────────────────────────────

@router.get("/sales-excel")
async def export_sales_excel(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Export sales register to Excel."""
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

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales"

    headers = [
        "Invoice No", "Date", "Customer", "Mobile", "PAN",
        "Pay Mode", "Subtotal", "CGST", "SGST", "IGST", "Grand Total", "Status"
    ]
    ws.append(headers)
    style_header_row(ws, 1, len(headers))

    for inv in invoices:
        ws.append([
            inv.invoice_no, inv.invoice_date.isoformat(), inv.customer_name,
            inv.customer_mobile, inv.customer_pan or "",
            inv.pay_mode.value, float(inv.subtotal),
            float(inv.cgst), float(inv.sgst), float(inv.igst),
            float(inv.grand_total), inv.payment_status.value,
        ])

    auto_col_width(ws)
    date_range = f"{from_date or 'all'}_{to_date or 'all'}"
    # Add Account Register and Dashboard sheets to every Excel export
    await add_account_sheet(wb, db, tenant_id, from_date, to_date)
    await add_dashboard_sheet(wb, db, tenant_id, from_date, to_date)
    # Move Dashboard to first position, Account Register to second
    wb.move_sheet("Dashboard", offset=-len(wb.sheetnames)+1)
    wb.move_sheet("Account Register", offset=-len(wb.sheetnames)+2)
    return _stream_workbook(wb, f"sales_register_{date_range}.xlsx")


# ── Cash Book Excel ───────────────────────────────────────────

@router.get("/cashbook-excel")
async def export_cashbook_excel(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Export cash book to Excel."""
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

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cash Book"

    headers = ["Date", "Type", "Description", "Cash In", "Cash Out", "Bank In", "Balance", "Reference"]
    ws.append(headers)
    style_header_row(ws, 1, len(headers))

    running = Decimal("0")
    for e in entries:
        amt   = Decimal(str(e.amount))
        etype = e.entry_type.value
        if etype in ("cash_in", "bank_in"):
            running += amt
        elif etype in ("cash_out", "cash_to_bank"):
            running -= amt

        ws.append([
            e.entry_date.isoformat(), etype, e.description or "",
            float(amt) if etype in ("cash_in", "bank_in") else 0,
            float(amt) if etype in ("cash_out", "cash_to_bank") else 0,
            float(amt) if etype == "bank_in" else 0,
            float(running),
            e.bank_reference or "",
        ])

    auto_col_width(ws)
    date_range = f"{from_date or 'all'}_{to_date or 'all'}"
    # Add Account Register and Dashboard sheets to every Excel export
    await add_account_sheet(wb, db, tenant_id, from_date, to_date)
    await add_dashboard_sheet(wb, db, tenant_id, from_date, to_date)
    # Move Dashboard to first position, Account Register to second
    wb.move_sheet("Dashboard", offset=-len(wb.sheetnames)+1)
    wb.move_sheet("Account Register", offset=-len(wb.sheetnames)+2)
    return _stream_workbook(wb, f"cash_book_{date_range}.xlsx")


# ── Item-wise Excel ───────────────────────────────────────────

@router.get("/itemwise-excel")
async def export_itemwise_excel(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Export item-wise sales report to Excel."""
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

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Item-wise"

    headers = [
        "Invoice No", "Date", "Customer", "Mobile", "PAN", "Mode",
        "Category", "Purity", "Description", "Qty", "Unit", "Rate", "Making", "Amount"
    ]
    ws.append(headers)
    style_header_row(ws, 1, len(headers))

    for inv in invoices:
        items_result = await db.execute(
            select(InvoiceItem).where(InvoiceItem.invoice_id == inv.id)
        )
        for item in items_result.scalars():
            ws.append([
                inv.invoice_no, inv.invoice_date.isoformat(),
                inv.customer_name, inv.customer_mobile, inv.customer_pan or "",
                inv.pay_mode.value, item.category.value, item.purity or "",
                item.description, float(item.qty), item.unit.value,
                float(item.rate), float(item.making_charges), float(item.amount),
            ])

    auto_col_width(ws)
    date_range = f"{from_date or 'all'}_{to_date or 'all'}"
    # Add Account Register and Dashboard sheets to every Excel export
    await add_account_sheet(wb, db, tenant_id, from_date, to_date)
    await add_dashboard_sheet(wb, db, tenant_id, from_date, to_date)
    # Move Dashboard to first position, Account Register to second
    wb.move_sheet("Dashboard", offset=-len(wb.sheetnames)+1)
    wb.move_sheet("Account Register", offset=-len(wb.sheetnames)+2)
    return _stream_workbook(wb, f"itemwise_summary_{date_range}.xlsx")


# ── SFT Excel ─────────────────────────────────────────────────

@router.get("/sft-excel")
async def export_sft_excel(
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """Export SFT register to Excel."""
    tenant_id = payload["tenant_id"]
    result    = await db.execute(
        select(Customer).where(Customer.tenant_id == tenant_id, Customer.sft_flagged == True)
    )
    customers = result.scalars().all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "SFT"

    headers = ["Customer", "Mobile", "PAN", "Cash Receipts FY", "SFT Threshold", "PAN Missing"]
    ws.append(headers)
    style_header_row(ws, 1, len(headers))

    for c in customers:
        ws.append([
            c.name, c.mobile, c.pan or "",
            float(c.cash_receipts_fy), float(SFT_THRESHOLD),
            "YES" if not c.pan else "No",
        ])

    auto_col_width(ws)
    # Add Account Register and Dashboard sheets to every Excel export
    await add_account_sheet(wb, db, tenant_id, from_date, to_date)
    await add_dashboard_sheet(wb, db, tenant_id, from_date, to_date)
    # Move Dashboard to first position, Account Register to second
    wb.move_sheet("Dashboard", offset=-len(wb.sheetnames)+1)
    wb.move_sheet("Account Register", offset=-len(wb.sheetnames)+2)
    return _stream_workbook(wb, f"sft_register_{date.today().isoformat()}.xlsx")


# ── Section 269ST Excel ───────────────────────────────────────

@router.get("/section-269st-excel")
async def export_section_269st_excel(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Export Section 269ST violation register to Excel.
    Uses INVOICE-level data: cash invoices with grand_total >= Rs. 2,00,000.
    Section 269ST prohibits receiving Rs. 2L+ in cash in a single transaction.
    """
    from decimal import Decimal
    tenant_id = payload["tenant_id"]
    threshold = Decimal("200000")

    stmt = (
        select(Invoice)
        .where(
            Invoice.tenant_id   == tenant_id,
            Invoice.status      == "active",
            Invoice.pay_mode    == "Cash",
            Invoice.grand_total >= threshold,
        )
        .order_by(Invoice.invoice_date.desc())
    )
    if from_date:
        stmt = stmt.where(Invoice.invoice_date >= from_date)
    if to_date:
        stmt = stmt.where(Invoice.invoice_date <= to_date)

    result   = await db.execute(stmt)
    invoices = result.scalars().all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sec 269ST Violations"

    headers = [
        "Invoice Date", "Invoice No", "Customer Name", "Mobile", "PAN",
        "Cash Amount (Rs.)", "Penalty Risk (Rs.)", "Notes"
    ]
    ws.append(headers)
    style_header_row(ws, 1, len(headers))

    for inv in invoices:
        cust = await db.get(Customer, (inv.customer_mobile, tenant_id))
        pan  = inv.customer_pan or (cust.pan if cust else "MISSING")
        ws.append([
            inv.invoice_date.isoformat(), inv.invoice_no,
            inv.customer_name, inv.customer_mobile, pan,
            float(inv.grand_total), float(inv.grand_total),
            inv.notes or "",
        ])

    auto_col_width(ws)
    date_range = f"{from_date or 'all'}_{to_date or 'all'}"

    # Add Account Register and Dashboard sheets to every Excel export
    await add_account_sheet(wb, db, tenant_id, from_date, to_date)
    await add_dashboard_sheet(wb, db, tenant_id, from_date, to_date)
    # Move Dashboard to first position, Account Register to second
    wb.move_sheet("Dashboard", offset=-len(wb.sheetnames)+1)
    wb.move_sheet("Account Register", offset=-len(wb.sheetnames)+2)
    return _stream_workbook(wb, f"section_269st_violations_{date_range}.xlsx")

# ── GSTR-1 Excel ──────────────────────────────────────────────
# Issue 4 fix — endpoint was missing, frontend Excel button returned 404

@router.get("/gstr1-excel")
async def export_gstr1_excel(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Export GSTR-1 register to Excel."""
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

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "GSTR-1"

    headers = [
        "Invoice No", "Date", "Customer Name", "GSTIN", "State", "HSN Code",
        "GST Type", "Taxable Value", "CGST Rate%", "CGST Amt", "SGST Rate%", "SGST Amt",
        "IGST Amt", "Grand Total"
    ]
    ws.append(headers)
    style_header_row(ws, 1, len(headers))

    for inv in invoices:
        ws.append([
            inv.invoice_no, inv.invoice_date.isoformat(), inv.customer_name,
            inv.customer_gstin or "Unregistered", inv.customer_state or "",
            "7113", inv.gst_type.value,
            float(inv.subtotal),
            float(inv.gst_rate / 2), float(inv.cgst),
            float(inv.gst_rate / 2), float(inv.sgst),
            float(inv.igst), float(inv.grand_total),
        ])

    auto_col_width(ws)
    date_range = f"{from_date or 'all'}_{to_date or 'all'}"
    # Add Account Register and Dashboard sheets to every Excel export
    await add_account_sheet(wb, db, tenant_id, from_date, to_date)
    await add_dashboard_sheet(wb, db, tenant_id, from_date, to_date)
    # Move Dashboard to first position, Account Register to second
    wb.move_sheet("Dashboard", offset=-len(wb.sheetnames)+1)
    wb.move_sheet("Account Register", offset=-len(wb.sheetnames)+2)
    return _stream_workbook(wb, f"gstr1_{date_range}.xlsx")


# ── Outstanding Register Excel ─────────────────────────────────
# Issue 4 fix — endpoint was missing

@router.get("/outstanding-excel")
async def export_outstanding_excel(
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """Export outstanding balances register to Excel."""
    tenant_id = payload["tenant_id"]
    result = await db.execute(
        select(Invoice).where(
            Invoice.tenant_id      == tenant_id,
            Invoice.payment_status != "paid",
            Invoice.status         == "active",
        ).order_by(Invoice.invoice_date.desc())
    )
    invoices = result.scalars().all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Outstanding"

    headers = ["Invoice No", "Date", "Customer Name", "Mobile", "Grand Total", "Amount Paid", "Outstanding"]
    ws.append(headers)
    style_header_row(ws, 1, len(headers))

    for inv in invoices:
        ws.append([
            inv.invoice_no, inv.invoice_date.isoformat(),
            inv.customer_name, inv.customer_mobile,
            float(inv.grand_total), float(inv.amount_paid), float(inv.outstanding),
        ])

    auto_col_width(ws)
    # Add Account Register and Dashboard sheets to every Excel export
    await add_account_sheet(wb, db, tenant_id, from_date, to_date)
    await add_dashboard_sheet(wb, db, tenant_id, from_date, to_date)
    # Move Dashboard to first position, Account Register to second
    wb.move_sheet("Dashboard", offset=-len(wb.sheetnames)+1)
    wb.move_sheet("Account Register", offset=-len(wb.sheetnames)+2)
    return _stream_workbook(wb, f"outstanding_register_{date.today().isoformat()}.xlsx")


# ── FIFO Valuation Excel ───────────────────────────────────────
# Issue 4 fix — endpoint was missing

@router.get("/fifo-excel")
async def export_fifo_excel(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Export FIFO stock valuation to Excel."""
    from utils.business import fifo_valuation
    from models import StockTransaction

    tenant_id = payload["tenant_id"]
    cutoff    = date.today()

    stocks_result = await db.execute(
        select(StockItem).where(
            StockItem.tenant_id == tenant_id,
            StockItem.category  != "Polish Charges",
            StockItem.is_active == True,
        )
    )
    stocks = stocks_result.scalars().all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "FIFO Valuation"

    headers = ["Category", "Purity", "Description", "Unit", "Qty on Hand", "Avg Rate", "Total Value"]
    ws.append(headers)
    style_header_row(ws, 1, len(headers))

    grand_total = 0.0
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
        grand_total += float(valuation["total_value"])

        ws.append([
            stock.category.value, stock.purity or "", stock.description,
            stock.unit.value, float(stock.qty_on_hand),
            float(valuation["avg_rate"]), float(valuation["total_value"]),
        ])

    # Total row
    ws.append(["", "", "", "TOTAL", "", "", grand_total])
    auto_col_width(ws)
    # Add Account Register and Dashboard sheets to every Excel export
    await add_account_sheet(wb, db, tenant_id, from_date, to_date)
    await add_dashboard_sheet(wb, db, tenant_id, from_date, to_date)
    # Move Dashboard to first position, Account Register to second
    wb.move_sheet("Dashboard", offset=-len(wb.sheetnames)+1)
    wb.move_sheet("Account Register", offset=-len(wb.sheetnames)+2)
    return _stream_workbook(wb, f"fifo_valuation_{cutoff.isoformat()}.xlsx")


# ── All Reports Excel (combined workbook) ──────────────────────
# Issue 4 fix — "All Reports Excel" button called this endpoint which was missing


# ── Supplier Excel Export ────────────────────────────────────

@router.get("/supplier-invoices-excel")
async def export_supplier_invoices_excel(
    mobile:    Optional[str]  = Query(None),
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Supplier purchase invoices Excel export."""
    tid  = payload["tenant_id"]
    stmt = select(SupplierInvoice).where(SupplierInvoice.tenant_id == tid, SupplierInvoice.status == "active")
    if mobile:    stmt = stmt.where(SupplierInvoice.supplier_mobile == mobile)
    if from_date: stmt = stmt.where(SupplierInvoice.invoice_date >= from_date)
    if to_date:   stmt = stmt.where(SupplierInvoice.invoice_date <= to_date)
    r    = await db.execute(stmt.order_by(SupplierInvoice.invoice_date.desc()))
    invs = r.scalars().all()

    wb = openpyxl.Workbook(); wb.remove(wb.active)
    add_sheet(wb, "Supplier Invoices", [
        "Invoice No", "Date", "Supplier Name", "Mobile",
        "Subtotal", "CGST", "SGST", "IGST", "Grand Total",
        "Amount Paid", "Outstanding", "Status",
    ], [
        [inv.invoice_no, inv.invoice_date.isoformat(), inv.supplier_name, inv.supplier_mobile,
         float(inv.subtotal), float(inv.cgst), float(inv.sgst), float(inv.igst),
         float(inv.grand_total), float(inv.amount_paid), float(inv.outstanding), inv.payment_status]
        for inv in invs
    ])
    fname = f"supplier_invoices_{from_date or 'all'}.xlsx"
    return _stream_workbook(wb, fname)


@router.get("/supplier-payments-excel")
async def export_supplier_payments_excel(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    tid  = payload["tenant_id"]
    stmt = select(SupplierPayment).where(SupplierPayment.tenant_id == tid)
    if from_date: stmt = stmt.where(SupplierPayment.payment_date >= from_date)
    if to_date:   stmt = stmt.where(SupplierPayment.payment_date <= to_date)
    r    = await db.execute(stmt.order_by(SupplierPayment.payment_date.desc()))
    pays = r.scalars().all()

    rows = []
    for p in pays:
        sup = await db.get(Supplier, (p.supplier_mobile, tid))
        rows.append([
            p.payment_date.isoformat(), sup.name if sup else "—", p.supplier_mobile,
            float(p.amount), p.pay_mode, p.reference_no or "—", p.notes or "",
        ])

    wb = openpyxl.Workbook(); wb.remove(wb.active)
    add_sheet(wb, "Supplier Payments", [
        "Date", "Supplier Name", "Mobile", "Amount", "Mode", "Reference", "Notes"
    ], rows)
    return _stream_workbook(wb, f"supplier_payments_{from_date or 'all'}.xlsx")


@router.get("/supplier-advances-excel")
async def export_supplier_advances_excel(
    payload: dict        = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """Download all supplier advances as Excel."""
    from models import SupplierAdvance, Supplier
    tid = payload["tenant_id"]
    wb  = openpyxl.Workbook()
    ws  = wb.active
    ws.title = "Supplier Advances"

    headers = ["Date", "Supplier Name", "Mobile", "Amount (₹)", "Remaining (₹)", "Mode", "Notes"]
    ws.append(headers)
    style_header_row(ws, 1, len(headers))

    result = await db.execute(
        select(SupplierAdvance)
        .where(SupplierAdvance.tenant_id == tid)
        .order_by(SupplierAdvance.advance_date.desc())
    )
    for a in result.scalars().all():
        sup = await db.get(Supplier, (a.supplier_mobile, tid))
        ws.append([
            a.advance_date.isoformat(),
            sup.name if sup else "—",
            a.supplier_mobile,
            float(a.amount),
            float(a.remaining),
            a.pay_mode,
            a.notes or "",
        ])

    auto_col_width(ws)
    return _stream_workbook(wb, f"supplier_advances.xlsx")


@router.get("/supplier-all-excel")
async def export_supplier_all_excel(
    payload: dict        = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """All-in-one Supplier Excel: Suppliers list, Invoices, Payments, Advances, Outstanding, GSTR-2B."""
    from models import (Supplier, SupplierInvoice, SupplierInvoiceItem,
                        SupplierPayment, SupplierAdvance)
    from datetime import date as _date
    tid      = payload["tenant_id"]
    today_dt = _date.today()
    wb       = openpyxl.Workbook()
    wb.remove(wb.active)  # remove default sheet

    # ── Sheet 1: Suppliers ──────────────────────────────────
    sup_r = await db.execute(
        select(Supplier).where(Supplier.tenant_id == tid).order_by(Supplier.name)
    )
    sup_list = sup_r.scalars().all()
    add_sheet(wb, "Suppliers", [
        "Name", "Mobile", "GSTIN", "PAN", "State", "Email", "Address"
    ], [
        [s.name, s.mobile, s.gstin or "", s.pan or "", s.state or "",
         s.email or "", s.address or ""]
        for s in sup_list
    ])

    # ── Sheet 2: Purchase Invoices ──────────────────────────
    inv_r = await db.execute(
        select(SupplierInvoice)
        .where(SupplierInvoice.tenant_id == tid, SupplierInvoice.status == "active")
        .order_by(SupplierInvoice.invoice_date.desc())
    )
    sup_invs = inv_r.scalars().all()
    add_sheet(wb, "Purchase Invoices", [
        "Invoice No", "Date", "Supplier Name", "Mobile",
        "Subtotal", "CGST", "SGST", "IGST", "Grand Total",
        "Amount Paid", "Outstanding", "Payment Status",
    ], [
        [inv.invoice_no, inv.invoice_date.isoformat(), inv.supplier_name, inv.supplier_mobile,
         float(inv.subtotal), float(inv.cgst), float(inv.sgst), float(inv.igst),
         float(inv.grand_total), float(inv.amount_paid), float(inv.outstanding), inv.payment_status]
        for inv in sup_invs
    ])

    # ── Sheet 3: Payments ───────────────────────────────────
    pay_r = await db.execute(
        select(SupplierPayment).where(SupplierPayment.tenant_id == tid)
        .order_by(SupplierPayment.payment_date.desc())
    )
    pay_rows = []
    for p in pay_r.scalars().all():
        sup = await db.get(Supplier, (p.supplier_mobile, tid))
        pay_rows.append([
            p.payment_date.isoformat(), sup.name if sup else "—", p.supplier_mobile,
            float(p.amount), p.pay_mode, p.reference_no or "—", p.notes or "",
        ])
    add_sheet(wb, "Payments", [
        "Date", "Supplier Name", "Mobile", "Amount", "Mode", "Reference", "Notes"
    ], pay_rows)

    # ── Sheet 4: Advances ───────────────────────────────────
    adv_r = await db.execute(
        select(SupplierAdvance).where(SupplierAdvance.tenant_id == tid)
        .order_by(SupplierAdvance.advance_date.desc())
    )
    adv_rows = []
    for a in adv_r.scalars().all():
        sup = await db.get(Supplier, (a.supplier_mobile, tid))
        adv_rows.append([
            a.advance_date.isoformat(), sup.name if sup else "—", a.supplier_mobile,
            float(a.amount), float(a.remaining), a.pay_mode, a.notes or "",
        ])
    add_sheet(wb, "Advances", [
        "Date", "Supplier Name", "Mobile", "Amount", "Remaining", "Mode", "Notes"
    ], adv_rows)

    # ── Sheet 5: Outstanding ────────────────────────────────
    out_r = await db.execute(
        select(SupplierInvoice)
        .where(SupplierInvoice.tenant_id == tid, SupplierInvoice.status == "active",
               SupplierInvoice.outstanding > 0)
        .order_by(SupplierInvoice.invoice_date)
    )
    add_sheet(wb, "Outstanding", [
        "Invoice No", "Invoice Date", "Supplier Name", "Mobile",
        "Grand Total", "Amount Paid", "Outstanding", "Days Overdue",
    ], [
        [inv.invoice_no, inv.invoice_date.isoformat(), inv.supplier_name, inv.supplier_mobile,
         float(inv.grand_total), float(inv.amount_paid), float(inv.outstanding),
         (today_dt - inv.invoice_date).days]
        for inv in out_r.scalars().all()
    ])

    # ── Sheet 6: GSTR-2B ────────────────────────────────────
    add_sheet(wb, "GSTR-2B Purchase", [
        "Invoice No", "Invoice Date", "Supplier Name", "Supplier GSTIN",
        "HSN Code", "Description", "Taxable Value",
        "CGST Rate%", "CGST Amt", "SGST Rate%", "SGST Amt",
        "IGST Rate%", "IGST Amt", "Invoice Total",
    ], [])
    ws_gstr = wb["GSTR-2B Purchase"]
    for inv in sup_invs:
        items_r = await db.execute(
            select(SupplierInvoiceItem).where(SupplierInvoiceItem.invoice_id == inv.id)
        )
        sup_obj   = await db.get(Supplier, (inv.supplier_mobile, tid))
        sup_gstin = sup_obj.gstin or "" if sup_obj else ""
        half_rate = float(inv.gst_rate) / 2
        for it in items_r.scalars().all():
            taxable  = float(it.amount)
            cgst_amt = round(taxable * half_rate / 100, 2) if inv.gst_type == "CGST+SGST" else 0
            sgst_amt = cgst_amt
            igst_amt = round(taxable * float(inv.gst_rate) / 100, 2) if inv.gst_type == "IGST" else 0
            ws_gstr.append([
                inv.invoice_no, inv.invoice_date.isoformat(),
                inv.supplier_name, sup_gstin,
                it.hsn_code, it.description, taxable,
                half_rate if inv.gst_type == "CGST+SGST" else 0, cgst_amt,
                half_rate if inv.gst_type == "CGST+SGST" else 0, sgst_amt,
                float(inv.gst_rate) if inv.gst_type == "IGST" else 0, igst_amt,
                float(inv.grand_total),
            ])
    auto_col_width(ws_gstr)

    return _stream_workbook(wb, f"GoldTrader_Supplier_All_{today_dt.isoformat()}.xlsx")


@router.get("/all-reports-excel")
async def export_all_reports_excel(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """
    Single Excel workbook containing all report sheets.
    Used by the 'All Reports Excel' button on the Reports page.
    """
    from utils.business import current_fy, fifo_valuation, SFT_THRESHOLD
    from models import StockTransaction, Advance
    from decimal import Decimal

    tid      = payload["tenant_id"]
    fy_start, fy_end = current_fy()
    today_dt = date.today()

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    # helper: date-filtered invoice query
    async def get_invoices(active_only=True):
        stmt = select(Invoice).where(Invoice.tenant_id == tid)
        if active_only:
            stmt = stmt.where(Invoice.status == "active")
        if from_date:
            stmt = stmt.where(Invoice.invoice_date >= from_date)
        if to_date:
            stmt = stmt.where(Invoice.invoice_date <= to_date)
        r = await db.execute(stmt.order_by(Invoice.invoice_date.desc()))
        return r.scalars().all()

    invoices = await get_invoices()

    # ── Sheet 1: Sales Register ───────────────────────────────
    add_sheet(wb, "Sales Register", [
        "Invoice No", "Date", "Customer", "Mobile", "PAN", "Pay Mode",
        "Subtotal", "CGST", "SGST", "IGST", "Grand Total", "Status"
    ], [
        [inv.invoice_no, inv.invoice_date.isoformat(), inv.customer_name,
         inv.customer_mobile, inv.customer_pan or "", inv.pay_mode.value,
         float(inv.subtotal), float(inv.cgst), float(inv.sgst), float(inv.igst),
         float(inv.grand_total), inv.payment_status.value]
        for inv in invoices
    ])

    # ── Sheet 2: GSTR-1 ──────────────────────────────────────
    add_sheet(wb, "GSTR-1", [
        "Invoice No", "Date", "Customer", "GSTIN", "State", "HSN",
        "Taxable Value", "CGST%", "CGST", "SGST%", "SGST", "IGST", "Total"
    ], [
        [inv.invoice_no, inv.invoice_date.isoformat(), inv.customer_name,
         inv.customer_gstin or "Unregistered", inv.customer_state or "", "7113",
         float(inv.subtotal), float(inv.gst_rate/2), float(inv.cgst),
         float(inv.gst_rate/2), float(inv.sgst), float(inv.igst), float(inv.grand_total)]
        for inv in invoices
    ])

    # ── Sheet 3: Outstanding ──────────────────────────────────
    outstanding = [inv for inv in invoices if float(inv.outstanding) > 0]
    add_sheet(wb, "Outstanding", [
        "Invoice No", "Date", "Customer", "Mobile", "Grand Total", "Paid", "Outstanding"
    ], [
        [inv.invoice_no, inv.invoice_date.isoformat(), inv.customer_name,
         inv.customer_mobile, float(inv.grand_total), float(inv.amount_paid), float(inv.outstanding)]
        for inv in outstanding
    ])

    # ── Sheet 4: Payments ─────────────────────────────────────
    pay_stmt = select(Payment).where(Payment.tenant_id == tid)
    if from_date: pay_stmt = pay_stmt.where(Payment.payment_date >= from_date)
    if to_date:   pay_stmt = pay_stmt.where(Payment.payment_date <= to_date)
    pay_result = await db.execute(pay_stmt.order_by(Payment.payment_date.desc()))
    payments   = pay_result.scalars().all()
    inv_no_map = {inv.id: inv.invoice_no for inv in invoices}

    pay_rows = []
    for p in payments:
        inv_obj = await db.get(Invoice, p.invoice_id) if p.invoice_id else None
        cname   = inv_obj.customer_name if inv_obj else "—"
        pay_rows.append([
            p.payment_date.isoformat(), inv_no_map.get(p.invoice_id,"—"),
            cname, p.customer_mobile, float(p.amount), p.pay_mode.value, p.reference_no or ""
        ])
    add_sheet(wb, "Payments", ["Date","Invoice No","Customer","Mobile","Amount","Mode","Reference"], pay_rows)

    # ── Sheet 5: Item-wise ────────────────────────────────────
    item_rows_data = []
    for inv in invoices:
        ir = await db.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == inv.id))
        for item in ir.scalars():
            item_rows_data.append([
                inv.invoice_no, inv.invoice_date.isoformat(), inv.customer_name,
                item.category.value, item.purity or "", item.description,
                float(item.qty), item.unit.value, float(item.rate),
                float(item.making_charges), float(item.amount)
            ])
    add_sheet(wb, "Item-wise", [
        "Invoice No","Date","Customer","Category","Purity","Description",
        "Qty","Unit","Rate","Making","Amount"
    ], item_rows_data)

    # ── Sheet 6: SFT ─────────────────────────────────────────
    cust_result = await db.execute(
        select(Customer).where(Customer.tenant_id == tid, Customer.sft_flagged == True)
    )
    sft_custs = cust_result.scalars().all()
    add_sheet(wb, "SFT Register", [
        "Customer","Mobile","PAN","Cash Receipts FY","Threshold","PAN Missing"
    ], [
        [c.name, c.mobile, c.pan or "", float(c.cash_receipts_fy),
         float(SFT_THRESHOLD), "YES" if not c.pan else "No"]
        for c in sft_custs
    ])

    # ── Sheet 7: Section 269ST ────────────────────────────────
    threshold = Decimal("200000")
    viol_invs = [
        inv for inv in invoices
        if inv.pay_mode.value == "Cash" and float(inv.grand_total) >= float(threshold)
    ]
    viol_rows = []
    for inv in viol_invs:
        cust = await db.get(Customer, (inv.customer_mobile, tid))
        viol_rows.append([
            inv.invoice_date.isoformat(), inv.invoice_no,
            inv.customer_name, inv.customer_mobile,
            inv.customer_pan or (cust.pan if cust else "MISSING"),
            float(inv.grand_total), float(inv.grand_total), inv.notes or ""
        ])
    add_sheet(wb, "Sec 269ST Violations", [
        "Date","Invoice No","Customer","Mobile","PAN","Cash Amount","Penalty Risk","Notes"
    ], viol_rows)


    # ── Sheet 8: Cash Book ────────────────────────────────────
    cash_stmt = select(CashEntry).where(CashEntry.tenant_id == tid)
    if from_date: cash_stmt = cash_stmt.where(CashEntry.entry_date >= from_date)
    if to_date:   cash_stmt = cash_stmt.where(CashEntry.entry_date <= to_date)
    cash_result = await db.execute(cash_stmt.order_by(CashEntry.entry_date, CashEntry.id))
    cash_entries = cash_result.scalars().all()

    running = Decimal("0")
    cash_rows = []
    for e in cash_entries:
        amt   = Decimal(str(e.amount))
        etype = e.entry_type.value
        if etype in ("cash_in", "bank_in"):
            running += amt
        elif etype in ("cash_out", "cash_to_bank"):
            running -= amt
        cash_rows.append([
            e.entry_date.isoformat(), etype, e.description or "",
            float(amt) if etype in ("cash_in","bank_in") else 0,
            float(amt) if etype in ("cash_out","cash_to_bank") else 0,
            float(running), e.bank_reference or ""
        ])
    add_sheet(wb, "Cash Book", [
        "Date","Type","Description","Cash In","Cash Out","Balance","Reference"
    ], cash_rows)

    # ── Supplier Invoices ────────────────────────────────────
    sup_inv_r = await db.execute(
        select(SupplierInvoice)
        .where(SupplierInvoice.tenant_id == tid, SupplierInvoice.status == "active")
        .order_by(SupplierInvoice.invoice_date.desc())
    )
    sup_invs = sup_inv_r.scalars().all()
    add_sheet(wb, "Supplier Invoices", [
        "Invoice No", "Date", "Supplier Name", "Mobile",
        "Subtotal", "CGST", "SGST", "IGST", "Grand Total",
        "Amount Paid", "Outstanding", "Payment Status",
    ], [
        [inv.invoice_no, inv.invoice_date.isoformat(), inv.supplier_name, inv.supplier_mobile,
         float(inv.subtotal), float(inv.cgst), float(inv.sgst), float(inv.igst),
         float(inv.grand_total), float(inv.amount_paid), float(inv.outstanding), inv.payment_status]
        for inv in sup_invs
    ])

    # ── Supplier Payments ─────────────────────────────────────
    sup_pay_r = await db.execute(
        select(SupplierPayment).where(SupplierPayment.tenant_id == tid)
        .order_by(SupplierPayment.payment_date.desc())
    )
    sup_pay_rows = []
    for p in sup_pay_r.scalars().all():
        sup_obj = await db.get(Supplier, (p.supplier_mobile, tid))
        sup_pay_rows.append([
            p.payment_date.isoformat(), sup_obj.name if sup_obj else "—", p.supplier_mobile,
            float(p.amount), p.pay_mode, p.reference_no or "—", p.notes or "",
        ])
    add_sheet(wb, "Supplier Payments", [
        "Date", "Supplier Name", "Mobile", "Amount", "Mode", "Reference", "Notes"
    ], sup_pay_rows)

    # ── Supplier Advances ─────────────────────────────────────
    sup_adv_r = await db.execute(
        select(SupplierAdvance).where(SupplierAdvance.tenant_id == tid)
        .order_by(SupplierAdvance.advance_date.desc())
    )
    sup_adv_rows = []
    for a in sup_adv_r.scalars().all():
        sup_obj = await db.get(Supplier, (a.supplier_mobile, tid))
        sup_adv_rows.append([
            a.advance_date.isoformat(), sup_obj.name if sup_obj else "—", a.supplier_mobile,
            float(a.amount), float(a.remaining), a.pay_mode, a.notes or "",
        ])
    add_sheet(wb, "Supplier Advances", [
        "Date", "Supplier Name", "Mobile", "Amount", "Remaining", "Mode", "Notes"
    ], sup_adv_rows)

    # ── Supplier Outstanding ──────────────────────────────────
    sup_out_r = await db.execute(
        select(SupplierInvoice)
        .where(SupplierInvoice.tenant_id == tid, SupplierInvoice.status == "active",
               SupplierInvoice.outstanding > 0)
        .order_by(SupplierInvoice.invoice_date)
    )
    add_sheet(wb, "Supplier Outstanding", [
        "Invoice No", "Invoice Date", "Supplier Name", "Mobile",
        "Grand Total", "Amount Paid", "Outstanding", "Days Overdue",
    ], [
        [inv.invoice_no, inv.invoice_date.isoformat(), inv.supplier_name, inv.supplier_mobile,
         float(inv.grand_total), float(inv.amount_paid), float(inv.outstanding),
         (today_dt - inv.invoice_date).days]
        for inv in sup_out_r.scalars().all()
    ])

    # ── GSTR-2B Purchase Register ─────────────────────────────
    add_sheet(wb, "GSTR-2B Purchase", [
        "Invoice No", "Invoice Date", "Supplier Name", "Supplier GSTIN",
        "HSN Code", "Description", "Taxable Value",
        "CGST Rate%", "CGST Amt", "SGST Rate%", "SGST Amt",
        "IGST Rate%", "IGST Amt", "Invoice Total",
    ], [])
    ws_gstr2b = wb["GSTR-2B Purchase"]
    for inv in sup_invs:
        items_r = await db.execute(
            select(SupplierInvoiceItem).where(SupplierInvoiceItem.invoice_id == inv.id)
        )
        sup_obj   = await db.get(Supplier, (inv.supplier_mobile, tid))
        sup_gstin = sup_obj.gstin or "" if sup_obj else ""
        half_rate = float(inv.gst_rate) / 2
        for it in items_r.scalars().all():
            taxable  = float(it.amount)
            cgst_amt = round(taxable * half_rate / 100, 2) if inv.gst_type == "CGST+SGST" else 0
            sgst_amt = cgst_amt
            igst_amt = round(taxable * float(inv.gst_rate) / 100, 2) if inv.gst_type == "IGST" else 0
            ws_gstr2b.append([
                inv.invoice_no, inv.invoice_date.isoformat(),
                inv.supplier_name, sup_gstin,
                it.hsn_code, it.description, taxable,
                half_rate if inv.gst_type == "CGST+SGST" else 0, cgst_amt,
                half_rate if inv.gst_type == "CGST+SGST" else 0, sgst_amt,
                float(inv.gst_rate) if inv.gst_type == "IGST" else 0, igst_amt,
                float(inv.grand_total),
            ])
    auto_col_width(ws_gstr2b)

    filename = f"GoldTrader_All_Reports_{today_dt.isoformat()}.xlsx"
    # Add Dashboard summary sheet and reorder: Dashboard first, Account second
    await add_dashboard_sheet(wb, db, tid, from_date, to_date)
    wb.move_sheet("Dashboard", offset=-len(wb.sheetnames)+1)
    if "Account Register" in wb.sheetnames:
        wb.move_sheet("Account Register", offset=-len(wb.sheetnames)+2)
    return _stream_workbook(wb, filename)


# ── Account Register Excel ─────────────────────────────────────
# New endpoint for Account report Excel download

@router.get("/account-excel")
async def export_account_excel(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """
    Export Account Register to Excel.
    One row per invoice: Invoice Date, Invoice No, Customer Name, Customer Mobile,
    Gold, Silver, Diamond, Polish Charges, Making Charges,
    CGST Amount, SGST Amount, IGST Amount, Grand Total.
    """
    from decimal import Decimal

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

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Account Register"

    headers = [
        "Invoice Date", "Invoice No", "Customer Name", "Customer Mobile",
        "Gold (₹)", "Silver (₹)", "Diamond (₹)", "Polish Charges (₹)",
        "Making Charges (₹)", "CGST (₹)", "SGST (₹)", "IGST (₹)", "Grand Total (₹)"
    ]
    ws.append(headers)
    style_header_row(ws, 1, len(headers))

    # Totals accumulators
    tot = {k: Decimal("0") for k in
           ["gold","silver","diamond","polish","making","cgst","sgst","igst","grand"]}

    for inv in invoices:
        items_result = await db.execute(
            select(InvoiceItem).where(InvoiceItem.invoice_id == inv.id)
        )
        items = items_result.scalars().all()

        gold_amt = silver_amt = diamond_amt = polish_amt = making_total = Decimal("0")
        for item in items:
            cat = item.category.value
            item_base = item.amount - item.making_charges
            making_total += item.making_charges
            if cat == "Gold":           gold_amt    += item_base
            elif cat == "Silver":       silver_amt  += item_base
            elif cat == "Diamond":      diamond_amt += item_base
            elif cat == "Polish Charges": polish_amt += item_base

        tot["gold"]   += gold_amt;    tot["silver"]  += silver_amt
        tot["diamond"]+= diamond_amt; tot["polish"]  += polish_amt
        tot["making"] += making_total; tot["cgst"]   += inv.cgst
        tot["sgst"]   += inv.sgst;    tot["igst"]    += inv.igst
        tot["grand"]  += inv.grand_total

        ws.append([
            inv.invoice_date.isoformat(), inv.invoice_no,
            inv.customer_name, inv.customer_mobile,
            float(gold_amt), float(silver_amt), float(diamond_amt), float(polish_amt),
            float(making_total),
            float(inv.cgst), float(inv.sgst), float(inv.igst),
            float(inv.grand_total),
        ])

    # Totals row
    total_row = [
        "TOTAL", "", "", "",
        float(tot["gold"]), float(tot["silver"]), float(tot["diamond"]), float(tot["polish"]),
        float(tot["making"]),
        float(tot["cgst"]), float(tot["sgst"]), float(tot["igst"]),
        float(tot["grand"]),
    ]
    ws.append(total_row)
    # Bold the totals row
    total_row_idx = ws.max_row
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=total_row_idx, column=col)
        cell.font = Font(name="Calibri", bold=True, size=10)
        cell.fill = PatternFill("solid", fgColor="FFF0CC")  # light gold background

    auto_col_width(ws)
    date_range = f"{from_date or 'all'}_{to_date or 'all'}"
    # Add Dashboard sheet and move it before the Account Register
    await add_dashboard_sheet(wb, db, tenant_id, from_date, to_date)
    wb.move_sheet("Dashboard", offset=-len(wb.sheetnames)+1)
    return _stream_workbook(wb, f"account_register_{date_range}.xlsx")
