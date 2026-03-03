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
    return _stream_workbook(wb, f"sft_register_{date.today().isoformat()}.xlsx")


# ── Section 269ST Excel ───────────────────────────────────────

@router.get("/section-269st-excel")
async def export_section_269st_excel(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Export Section 269ST violation register to Excel."""
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

    inv_result = await db.execute(select(Invoice).where(Invoice.tenant_id == tenant_id))
    inv_no_map = {inv.id: inv.invoice_no for inv in inv_result.scalars().all()}

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Section 269ST"

    headers = [
        "Date", "Invoice No", "Customer Name", "Mobile", "PAN",
        "Cash Amount (₹)", "Penalty Risk (₹)", "Reference"
    ]
    ws.append(headers)
    style_header_row(ws, 1, len(headers))

    for p in payments:
        cust  = await db.get(Customer, (p.customer_mobile, tenant_id))
        cname = (getattr(p, "customer_name", None) or (cust.name if cust else "—"))
        cpan  = cust.pan if cust else "MISSING"
        ws.append([
            p.payment_date.isoformat(),
            inv_no_map.get(p.invoice_id, "—"),
            cname, p.customer_mobile, cpan,
            float(p.amount), float(p.amount),
            p.reference_no or "",
        ])

    auto_col_width(ws)
    date_range = f"{from_date or 'all'}_{to_date or 'all'}"
    return _stream_workbook(wb, f"section_269st_violations_{date_range}.xlsx")
