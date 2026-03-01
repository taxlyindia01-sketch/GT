# routers/export.py — Excel export: full backup (15 sheets) + individual reports

from io import BytesIO
from datetime import date, datetime
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
    CashEntry, Advance, StockItem, StockTransaction
)
from utils.auth import get_tenant_payload as get_current_user_payload
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


# ── Full Backup (15 sheets) ───────────────────────────────────

@router.get("/excel")
async def export_full_backup(
    tenant_id: Optional[int] = Query(None),
    payload:   dict          = Depends(get_current_user_payload),
    db:        AsyncSession  = Depends(get_db),
):
    """
    Export full backup as Excel workbook with 15 sheets:
    Data: invoices, invoice_items, payments, customers, stocks,
          cash_register, advances, stock_transactions
    Reports: sales, tcs, sft, gstr1, account_invoice, account_item, cash_summary, outstanding
    """
    tid = tenant_id or payload["tenant_id"]

    wb = openpyxl.Workbook()
    wb.remove(wb.active)   # remove default sheet

    # ── Sheet 1: Invoices ─────────────────────────────────────
    result = await db.execute(select(Invoice).where(Invoice.tenant_id == tid).order_by(Invoice.invoice_date.desc()))
    invoices = result.scalars().all()

    add_sheet(wb, "Invoices", [
        "Invoice No", "Date", "Customer Name", "Mobile", "PAN", "Pay Mode",
        "Subtotal", "CGST", "SGST", "IGST", "TCS", "Grand Total", "Paid", "Outstanding", "Status"
    ], [
        [inv.invoice_no, inv.invoice_date.isoformat(), inv.customer_name, inv.customer_mobile,
         inv.customer_pan or "", inv.pay_mode.value, float(inv.subtotal),
         float(inv.cgst), float(inv.sgst), float(inv.igst), float(inv.tcs_amount),
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
        "ID", "Invoice No", "Customer Mobile", "Amount", "Date", "Mode", "Reference", "Notes"
    ], [
        [p.id, inv_no_map.get(p.invoice_id, ""), p.customer_mobile,
         float(p.amount), p.payment_date.isoformat(), p.pay_mode.value,
         p.reference_no or "", p.notes or ""]
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
        "ID", "Customer Mobile", "Amount", "Remaining", "Date", "Mode", "Notes"
    ], [
        [a.id, a.customer_mobile, float(a.amount), float(a.remaining),
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
    fy_start, fy_end = current_fy()
    fy_invoices = [inv for inv in invoices if fy_start <= inv.invoice_date <= fy_end]

    add_sheet(wb, "Report_Sales", [
        "Invoice No", "Date", "Customer", "Mobile", "HSN", "Subtotal", "CGST", "SGST", "TCS", "Grand Total", "Mode"
    ], [
        [inv.invoice_no, inv.invoice_date.isoformat(), inv.customer_name, inv.customer_mobile,
         "7113", float(inv.subtotal), float(inv.cgst), float(inv.sgst),
         float(inv.tcs_amount), float(inv.grand_total), inv.pay_mode.value]
        for inv in fy_invoices
    ])

    # ── Report Sheet 10: TCS Register ─────────────────────────
    tcs_invoices = [inv for inv in invoices if inv.tcs_applicable and fy_start <= inv.invoice_date <= fy_end]

    add_sheet(wb, "Report_TCS_26Q", [
        "Invoice No", "Date", "Customer", "Mobile", "PAN", "Invoice Value", "TCS Base", "TCS @1%", "Mode"
    ], [
        [inv.invoice_no, inv.invoice_date.isoformat(), inv.customer_name, inv.customer_mobile,
         inv.customer_pan or "MISSING", float(inv.grand_total),
         float(inv.tcs_base), float(inv.tcs_amount), inv.pay_mode.value]
        for inv in tcs_invoices
    ])

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

    # ── Report Sheet 13: Account Register (Invoice-wise) ──────
    add_sheet(wb, "Report_Account_Invoice", [
        "Invoice No", "Date", "Customer", "Mobile", "State", "GST Type",
        "Subtotal", "CGST", "SGST", "TCS", "Grand Total", "Pay Mode"
    ], [
        [inv.invoice_no, inv.invoice_date.isoformat(), inv.customer_name,
         inv.customer_mobile, inv.customer_state or "", inv.gst_type.value,
         float(inv.subtotal), float(inv.cgst), float(inv.sgst),
         float(inv.tcs_amount), float(inv.grand_total), inv.pay_mode.value]
        for inv in invoices
    ])

    # ── Report Sheet 14: Outstanding ──────────────────────────
    outstanding_invoices = [inv for inv in invoices if float(inv.outstanding) > 0]

    add_sheet(wb, "Report_Outstanding", [
        "Invoice No", "Date", "Customer", "Mobile", "Grand Total", "Paid", "Outstanding"
    ], [
        [inv.invoice_no, inv.invoice_date.isoformat(), inv.customer_name,
         inv.customer_mobile, float(inv.grand_total), float(inv.amount_paid), float(inv.outstanding)]
        for inv in outstanding_invoices
    ])

    # ── Report Sheet 15: Cash Summary ────────────────────────
    fy_cash_entries = [e for e in entries if fy_start <= e.entry_date <= fy_end]

    add_sheet(wb, "Report_Cash_Register", [
        "Date", "Type", "Description", "Cash In", "Cash Out", "Bank In", "Balance"
    ], [
        [e.entry_date.isoformat(), e.entry_type.value, e.description,
         float(e.amount) if e.entry_type.value == "cash_in"      else 0,
         float(e.amount) if e.entry_type.value in ("cash_out","cash_to_bank") else 0,
         float(e.amount) if e.entry_type.value == "bank_in"      else 0,
         float(e.running_balance or 0)]
        for e in fy_cash_entries
    ])

    # ── Stream response ───────────────────────────────────────
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"goldtrader_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── Payment Register Excel ────────────────────────────────────

@router.get("/payments-excel")
async def export_payments_excel(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """
    Download Payment Register as Excel with date filter.
    Columns: Date, Invoice No, Customer Name, Mobile, Amount, Mode, Reference, Notes
    """
    from models import Payment as PaymentModel
    tid = payload["tenant_id"]

    q = select(PaymentModel).where(PaymentModel.tenant_id == tid).order_by(PaymentModel.payment_date.desc())
    if from_date: q = q.where(PaymentModel.payment_date >= from_date)
    if to_date:   q = q.where(PaymentModel.payment_date <= to_date)
    result = await db.execute(q)
    payments = result.scalars().all()

    # Get invoice numbers
    inv_ids = list({p.invoice_id for p in payments if p.invoice_id})
    inv_map = {}
    if inv_ids:
        from models import Invoice as InvoiceModel
        inv_res = await db.execute(select(InvoiceModel).where(InvoiceModel.id.in_(inv_ids)))
        for inv in inv_res.scalars().all():
            inv_map[inv.id] = inv.invoice_no

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Payment Register"

    # Title row
    ws.merge_cells("A1:H1")
    title = ws["A1"]
    title.value = f"Payment Register{' — ' + from_date.isoformat() if from_date else ''}{' to ' + to_date.isoformat() if to_date else ''}"
    title.font = Font(name="Calibri", bold=True, size=13, color="FFFFFF")
    title.fill = PatternFill("solid", fgColor="1A1A2E")
    title.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 26

    headers = ["Date", "Invoice No", "Customer Name", "Mobile", "Amount (₹)", "Mode", "Reference No", "Notes"]
    ws.append(headers)
    style_header_row(ws, 2, len(headers))

    total = 0.0
    for p in payments:
        ws.append([
            p.payment_date.isoformat(),
            inv_map.get(p.invoice_id, f"ID:{p.invoice_id}"),
            p.customer_name if hasattr(p, "customer_name") and p.customer_name else "",
            p.customer_mobile,
            float(p.amount),
            p.pay_mode,
            p.reference_no or "",
            p.notes or "",
        ])
        total += float(p.amount)

    # Totals row
    last_row = ws.max_row + 1
    ws.cell(row=last_row, column=1).value = "TOTAL"
    ws.cell(row=last_row, column=1).font = Font(bold=True, name="Calibri")
    ws.cell(row=last_row, column=5).value = total
    ws.cell(row=last_row, column=5).font = Font(bold=True, name="Calibri", color="1A7E3A")

    auto_col_width(ws)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"payment_register_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


# ── Cash Book Excel ───────────────────────────────────────────

@router.get("/cashbook-excel")
async def export_cashbook_excel(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """
    Download Cash Book as Excel with date filter.
    Replica of the Cash Book page with running balance.
    """
    from models import CashEntry as CashEntryModel, CashEntryType
    tid = payload["tenant_id"]

    q = select(CashEntryModel).where(CashEntryModel.tenant_id == tid).order_by(CashEntryModel.entry_date.asc(), CashEntryModel.id.asc())
    if from_date: q = q.where(CashEntryModel.entry_date >= from_date)
    if to_date:   q = q.where(CashEntryModel.entry_date <= to_date)
    result = await db.execute(q)
    entries = result.scalars().all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cash Book"

    # Title
    ws.merge_cells("A1:H1")
    title = ws["A1"]
    title.value = f"Cash Book{' — ' + from_date.isoformat() if from_date else ''}{' to ' + to_date.isoformat() if to_date else ''}"
    title.font = Font(name="Calibri", bold=True, size=13, color="FFFFFF")
    title.fill = PatternFill("solid", fgColor="1A1A2E")
    title.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 26

    headers = ["Date", "Type", "Description", "Cash In (₹)", "Cash Out (₹)", "Bank Withdraw→Cash (₹)", "Bank Ref.", "Running Balance (₹)"]
    ws.append(headers)
    style_header_row(ws, 2, len(headers))

    GREEN_FILL = PatternFill("solid", fgColor="E8F5E9")
    RED_FILL   = PatternFill("solid", fgColor="FFEBEE")

    total_in = total_out = total_bank = 0.0
    for e in entries:
        amt = float(e.amount)
        is_in    = e.entry_type == CashEntryType.cash_in
        is_out   = e.entry_type in (CashEntryType.cash_out, CashEntryType.cash_to_bank)
        is_bk    = e.entry_type == CashEntryType.bank_in
        type_label = {
            "cash_in":      "Cash In",
            "cash_out":     "Cash Out",
            "cash_to_bank": "Cash to Bank",
            "bank_in":      "Bank Withdraw → Cash",
        }.get(e.entry_type.value, e.entry_type.value)

        row_data = [
            e.entry_date.isoformat(), type_label, e.description,
            amt if is_in  else "",
            amt if is_out else "",
            amt if is_bk  else "",
            e.bank_reference or "",
            float(e.running_balance) if e.running_balance is not None else "",
        ]
        ws.append(row_data)
        row_idx = ws.max_row
        fill = GREEN_FILL if is_in or is_bk else RED_FILL
        for col in range(1, 9):
            ws.cell(row=row_idx, column=col).fill = fill

        if is_in:  total_in  += amt
        if is_out: total_out += amt
        if is_bk:  total_bank += amt

    # Totals
    last_row = ws.max_row + 1
    for col, val in [(1, "TOTAL"), (4, total_in), (5, total_out), (6, total_bank)]:
        cell = ws.cell(row=last_row, column=col)
        cell.value = val
        cell.font  = Font(bold=True, name="Calibri")

    auto_col_width(ws)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"cash_book_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


# ── Item-wise Invoice Summary Report Excel ────────────────────

@router.get("/itemwise-excel")
async def export_itemwise_excel(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Item-wise accounting summary: each invoice line item with full invoice header."""
    from models import InvoiceItem as InvItem, Invoice as Inv
    tid = payload["tenant_id"]

    q = (
        select(InvItem, Inv)
        .join(Inv, InvItem.invoice_id == Inv.id)
        .where(Inv.tenant_id == tid, Inv.status != "cancelled")
    )
    if from_date: q = q.where(Inv.invoice_date >= from_date)
    if to_date:   q = q.where(Inv.invoice_date <= to_date)
    q = q.order_by(Inv.invoice_date.desc(), Inv.id, InvItem.id)

    result = await db.execute(q)
    rows = result.all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Item-wise Summary"

    ws.merge_cells("A1:N1")
    title = ws["A1"]
    title.value = "Item-wise Invoice Summary"
    title.font = Font(name="Calibri", bold=True, size=13, color="FFFFFF")
    title.fill = PatternFill("solid", fgColor="C8900A")
    title.alignment = Alignment(horizontal="center")
    ws.row_dimensions[1].height = 26

    headers = ["Invoice No", "Date", "Customer Name", "Mobile", "PAN", "Pay Mode",
               "Category", "Purity", "Description", "HSN", "Qty", "Unit",
               "Rate (₹)", "Making (₹)", "Amount (₹)", "Invoice Total (₹)"]
    ws.append(headers)
    style_header_row(ws, 2, len(headers))

    grand = 0.0
    for item, inv in rows:
        ws.append([
            inv.invoice_no, inv.invoice_date.isoformat(),
            inv.customer_name, inv.customer_mobile, inv.customer_pan or "",
            inv.pay_mode, item.category.value if hasattr(item.category, 'value') else str(item.category),
            item.purity or "", item.description, item.hsn_code,
            float(item.qty), item.unit.value if hasattr(item.unit, 'value') else str(item.unit),
            float(item.rate), float(item.making_charges), float(item.amount),
            float(inv.grand_total),
        ])
        grand += float(item.amount)

    last_row = ws.max_row + 1
    ws.cell(row=last_row, column=1).value = "GRAND TOTAL"
    ws.cell(row=last_row, column=15).value = grand
    for col in (1, 15):
        ws.cell(row=last_row, column=col).font = Font(bold=True, name="Calibri", color="C8900A")

    auto_col_width(ws)
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"itemwise_summary_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


# ── Sales Register Excel ──────────────────────────────────────

@router.get("/sales-excel")
async def export_sales_excel(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Download Sales Register as Excel with date filter."""
    tid = payload["tenant_id"]
    q = select(Invoice).where(Invoice.tenant_id == tid, Invoice.status == "active").order_by(Invoice.invoice_date.desc())
    if from_date: q = q.where(Invoice.invoice_date >= from_date)
    if to_date:   q = q.where(Invoice.invoice_date <= to_date)
    result = await db.execute(q)
    invoices = result.scalars().all()

    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Sales Register"
    ws.merge_cells("A1:K1")
    t = ws["A1"]; t.value = f"Sales Register{' — '+from_date.isoformat() if from_date else ''}{' to '+to_date.isoformat() if to_date else ''}"
    t.font = Font(name="Calibri", bold=True, size=13, color="FFFFFF")
    t.fill = PatternFill("solid", fgColor="C8900A"); t.alignment = Alignment(horizontal="center")
    ws.row_dimensions[1].height = 26
    headers = ["Invoice No","Date","Customer","Mobile","Pay Mode","Subtotal (₹)","CGST (₹)","SGST (₹)","IGST (₹)","TCS (₹)","Grand Total (₹)","Status"]
    ws.append(headers); style_header_row(ws, 2, len(headers))
    total = 0.0
    for inv in invoices:
        ws.append([inv.invoice_no, inv.invoice_date.isoformat(), inv.customer_name, inv.customer_mobile,
                   inv.pay_mode.value, float(inv.subtotal), float(inv.cgst), float(inv.sgst),
                   float(inv.igst), float(inv.tcs_amount), float(inv.grand_total), inv.payment_status.value])
        total += float(inv.grand_total)
    lr = ws.max_row + 1
    ws.cell(row=lr, column=1).value = "TOTAL"; ws.cell(row=lr, column=11).value = total
    for col in (1,11): ws.cell(row=lr, column=col).font = Font(bold=True, name="Calibri", color="C8900A")
    auto_col_width(ws)
    buf = BytesIO(); wb.save(buf); buf.seek(0)
    fname = f"sales_register_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f"attachment; filename={fname}"})


# ── TCS Register Excel ────────────────────────────────────────

@router.get("/tcs-excel")
async def export_tcs_excel(
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """Download TCS Register 26Q as Excel."""
    tid = payload["tenant_id"]
    fy_start, fy_end = current_fy()
    result = await db.execute(select(Invoice).where(Invoice.tenant_id == tid, Invoice.tcs_applicable == True,
        Invoice.status == "active", Invoice.invoice_date >= fy_start, Invoice.invoice_date <= fy_end))
    invoices = result.scalars().all()

    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "TCS Register 26Q"
    ws.merge_cells("A1:I1"); t = ws["A1"]; t.value = f"TCS Register — 26Q — FY {fy_start.year}-{str(fy_start.year+1)[2:]}"
    t.font = Font(name="Calibri", bold=True, size=13, color="FFFFFF")
    t.fill = PatternFill("solid", fgColor="C8900A"); t.alignment = Alignment(horizontal="center"); ws.row_dimensions[1].height = 26
    headers = ["Invoice No","Date","Customer","Mobile","PAN","Invoice Value (₹)","TCS Base (₹)","TCS @1% (₹)","Pay Mode"]
    ws.append(headers); style_header_row(ws, 2, len(headers))
    for inv in invoices:
        ws.append([inv.invoice_no, inv.invoice_date.isoformat(), inv.customer_name, inv.customer_mobile,
                   inv.customer_pan or "⚠ MISSING", float(inv.grand_total), float(inv.tcs_base), float(inv.tcs_amount), inv.pay_mode.value])
    auto_col_width(ws)
    buf = BytesIO(); wb.save(buf); buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": "attachment; filename=tcs_register_26q.xlsx"})


# ── GSTR-1 Excel ──────────────────────────────────────────────

@router.get("/gstr1-excel")
async def export_gstr1_excel(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    payload:   dict           = Depends(get_current_user_payload),
    db:        AsyncSession   = Depends(get_db),
):
    """Download GSTR-1 Register as Excel."""
    tid = payload["tenant_id"]
    q = select(Invoice).where(Invoice.tenant_id == tid, Invoice.status == "active").order_by(Invoice.invoice_date.desc())
    if from_date: q = q.where(Invoice.invoice_date >= from_date)
    if to_date:   q = q.where(Invoice.invoice_date <= to_date)
    result = await db.execute(q); invoices = result.scalars().all()

    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "GSTR-1"
    ws.merge_cells("A1:L1"); t = ws["A1"]; t.value = "GSTR-1 Register"
    t.font = Font(name="Calibri", bold=True, size=13, color="FFFFFF")
    t.fill = PatternFill("solid", fgColor="C8900A"); t.alignment = Alignment(horizontal="center"); ws.row_dimensions[1].height = 26
    headers = ["Invoice No","Date","Customer","GSTIN","State","HSN","GST Type","Taxable (₹)","CGST%","CGST (₹)","SGST%","SGST (₹)","Grand Total (₹)"]
    ws.append(headers); style_header_row(ws, 2, len(headers))
    for inv in invoices:
        ws.append([inv.invoice_no, inv.invoice_date.isoformat(), inv.customer_name, inv.customer_gstin or "Unregistered",
                   inv.customer_state or "", "7113", inv.gst_type.value, float(inv.subtotal),
                   float(inv.gst_rate/2), float(inv.cgst), float(inv.gst_rate/2), float(inv.sgst), float(inv.grand_total)])
    auto_col_width(ws)
    buf = BytesIO(); wb.save(buf); buf.seek(0)
    fname = f"gstr1_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f"attachment; filename={fname}"})


# ── SFT Register Excel ────────────────────────────────────────

@router.get("/sft-excel")
async def export_sft_excel(
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """Download SFT Register as Excel."""
    from models import Customer as Cust
    tid = payload["tenant_id"]
    result = await db.execute(select(Cust).where(Cust.tenant_id == tid, Cust.sft_flagged == True))
    customers = result.scalars().all()

    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "SFT Register"
    ws.merge_cells("A1:F1"); t = ws["A1"]; t.value = "SFT Register — Cash > ₹2,00,000 FY"
    t.font = Font(name="Calibri", bold=True, size=13, color="FFFFFF")
    t.fill = PatternFill("solid", fgColor="C8900A"); t.alignment = Alignment(horizontal="center"); ws.row_dimensions[1].height = 26
    headers = ["Customer Name","Mobile","PAN","Cash Receipts FY (₹)","SFT Threshold (₹)","PAN Missing"]
    ws.append(headers); style_header_row(ws, 2, len(headers))
    for c in customers:
        ws.append([c.name, c.mobile, c.pan or "", float(c.cash_receipts_fy), float(SFT_THRESHOLD), "YES" if not c.pan else "No"])
    auto_col_width(ws)
    buf = BytesIO(); wb.save(buf); buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": "attachment; filename=sft_register.xlsx"})


# ── Outstanding Register Excel ────────────────────────────────

@router.get("/outstanding-excel")
async def export_outstanding_excel(
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """Download Outstanding Register as Excel."""
    tid = payload["tenant_id"]
    result = await db.execute(select(Invoice).where(Invoice.tenant_id == tid, Invoice.payment_status != "paid",
        Invoice.status == "active").order_by(Invoice.invoice_date.desc()))
    invoices = result.scalars().all()

    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Outstanding"
    ws.merge_cells("A1:G1"); t = ws["A1"]; t.value = "Outstanding Invoices"
    t.font = Font(name="Calibri", bold=True, size=13, color="FFFFFF")
    t.fill = PatternFill("solid", fgColor="C8900A"); t.alignment = Alignment(horizontal="center"); ws.row_dimensions[1].height = 26
    headers = ["Invoice No","Date","Customer","Mobile","Grand Total (₹)","Paid (₹)","Outstanding (₹)"]
    ws.append(headers); style_header_row(ws, 2, len(headers))
    for inv in invoices:
        ws.append([inv.invoice_no, inv.invoice_date.isoformat(), inv.customer_name, inv.customer_mobile,
                   float(inv.grand_total), float(inv.amount_paid), float(inv.outstanding)])
    auto_col_width(ws)
    buf = BytesIO(); wb.save(buf); buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": "attachment; filename=outstanding_register.xlsx"})


# ── FIFO Stock Valuation Excel ────────────────────────────────

@router.get("/fifo-excel")
async def export_fifo_excel(
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """Download FIFO Stock Valuation as Excel."""
    from models import StockItem as SI, StockTransaction as ST
    from utils.business import fifo_valuation
    tid = payload["tenant_id"]
    stocks_result = await db.execute(select(SI).where(SI.tenant_id == tid, SI.category != "Polish Charges", SI.is_active == True))
    stocks = stocks_result.scalars().all()

    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "FIFO Valuation"
    ws.merge_cells("A1:G1"); t = ws["A1"]; t.value = f"FIFO Stock Valuation — {date.today().isoformat()}"
    t.font = Font(name="Calibri", bold=True, size=13, color="FFFFFF")
    t.fill = PatternFill("solid", fgColor="C8900A"); t.alignment = Alignment(horizontal="center"); ws.row_dimensions[1].height = 26
    headers = ["Category","Purity","Description","Unit","Qty on Hand","Avg Rate (₹)","Total Value (₹)"]
    ws.append(headers); style_header_row(ws, 2, len(headers))
    grand = 0.0
    for stock in stocks:
        txns = await db.execute(select(ST).where(ST.stock_item_id == stock.id, ST.txn_type.in_(["purchase","opening"]), ST.lot_remaining > 0).order_by(ST.txn_date))
        lots = [{"qty_remaining": t.lot_remaining, "purchase_rate": t.purchase_rate} for t in txns.scalars() if t.purchase_rate]
        val = fifo_valuation(lots)
        ws.append([stock.category.value, stock.purity or "—", stock.description, stock.unit.value,
                   float(stock.qty_on_hand), float(val["avg_rate"]), float(val["total_value"])])
        grand += float(val["total_value"])
    lr = ws.max_row + 1
    ws.cell(row=lr, column=1).value = "GRAND TOTAL"; ws.cell(row=lr, column=7).value = grand
    for col in (1,7): ws.cell(row=lr, column=col).font = Font(bold=True, name="Calibri", color="C8900A")
    auto_col_width(ws)
    buf = BytesIO(); wb.save(buf); buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": "attachment; filename=fifo_valuation.xlsx"})
