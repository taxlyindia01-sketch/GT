# utils/pdf.py — GST Invoice PDF via reportlab
"""
generate_invoice_pdf(invoice, items, company_name, company_gstin, company_address)
  → bytes  (PDF binary)

Layout (A4):
  ┌─────────────────────────────────────┐
  │  COMPANY NAME   |  TAX INVOICE      │
  │  GSTIN / Addr   |  No: INV-1-0001   │
  │                 |  Date: DD/MM/YYYY │
  ├─────────────────────────────────────┤
  │  Bill To: Customer details          │
  ├─────────────────────────────────────┤
  │  # | Description | HSN | Qty | Rate | Making | Amount │
  ├─────────────────────────────────────┤
  │  Subtotal / CGST / SGST / IGST / TCS / Grand Total    │
  ├─────────────────────────────────────┤
  │  Notes | Terms & Conditions         │
  └─────────────────────────────────────┘

Does NOT use external fonts — uses reportlab's built-in Helvetica so it
works on Render without any system font installation.
"""

import io
from decimal import Decimal
from typing import Optional

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.colors import HexColor, black, white, grey
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    )
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
    from reportlab.platypus import KeepTogether
    REPORTLAB_OK = True
except ImportError:
    REPORTLAB_OK = False


# ── Colours ───────────────────────────────────────────────────
GOLD    = HexColor("#c8900a") if REPORTLAB_OK else None
DARKBG  = HexColor("#020c14") if REPORTLAB_OK else None
MIDGREY = HexColor("#f0f4f8") if REPORTLAB_OK else None
TEXTDARK= HexColor("#1a202c") if REPORTLAB_OK else None
MUTED   = HexColor("#718096") if REPORTLAB_OK else None

W, H = A4 if REPORTLAB_OK else (595, 842)
MARGIN = 18 * mm if REPORTLAB_OK else 51


def _inr(v) -> str:
    """Format as Indian Rupee string."""
    try:
        n = float(v or 0)
        return f"₹{n:,.2f}"
    except Exception:
        return "₹0.00"


def generate_invoice_pdf(
    invoice,          # Invoice ORM object (or dict-like)
    items:  list,     # list of InvoiceItem ORM objects
    company_name:    str,
    company_gstin:   Optional[str] = None,
    company_address: Optional[str] = None,
    company_phone:   Optional[str] = None,
    company_state:   Optional[str] = None,
) -> bytes:
    """
    Generate a GST-compliant invoice PDF.

    Returns raw PDF bytes.  Raises RuntimeError if reportlab is not installed.
    """
    if not REPORTLAB_OK:
        raise RuntimeError(
            "reportlab is not installed. Add reportlab==4.2.5 to requirements.txt and redeploy."
        )

    buf = io.BytesIO()

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        title=f"Invoice {getattr(invoice, 'invoice_no', 'INV')}",
    )

    styles = getSampleStyleSheet()
    story  = []

    # ── Helper styles ─────────────────────────────────────────
    def sty(name, **kw):
        base = styles["Normal"] if name == "N" else styles.get(name, styles["Normal"])
        return ParagraphStyle(
            f"custom_{id(kw)}",
            parent=base,
            **kw
        )

    H1  = sty("N", fontSize=18, fontName="Helvetica-Bold", textColor=TEXTDARK, spaceAfter=2)
    H2  = sty("N", fontSize=11, fontName="Helvetica-Bold", textColor=TEXTDARK)
    H3  = sty("N", fontSize=9,  fontName="Helvetica-Bold", textColor=TEXTDARK)
    NOR = sty("N", fontSize=8,  fontName="Helvetica",      textColor=TEXTDARK)
    MUT = sty("N", fontSize=7,  fontName="Helvetica",      textColor=MUTED)
    RIG = sty("N", fontSize=8,  fontName="Helvetica",      textColor=TEXTDARK, alignment=TA_RIGHT)
    CEN = sty("N", fontSize=8,  fontName="Helvetica",      textColor=TEXTDARK, alignment=TA_CENTER)
    GOLD_H = sty("N", fontSize=9, fontName="Helvetica-Bold", textColor=GOLD)

    inv_no   = getattr(invoice, "invoice_no",    "—")
    inv_date = getattr(invoice, "invoice_date",  None)
    inv_date_str = inv_date.strftime("%d/%m/%Y") if inv_date else "—"

    # ── Header block ──────────────────────────────────────────
    header_data = [
        [
            # Left: Company info
            [
                Paragraph(company_name, H1),
                Paragraph(f"GSTIN: {company_gstin or 'Not Registered'}", NOR),
                Paragraph(company_address or "", NOR),
                Paragraph(company_phone or "", NOR),
            ],
            # Right: Invoice title
            [
                Paragraph("TAX INVOICE", sty("N", fontSize=16, fontName="Helvetica-Bold",
                                              textColor=GOLD, alignment=TA_RIGHT)),
                Paragraph(f"Invoice No: <b>{inv_no}</b>", sty("N", fontSize=9,
                           fontName="Helvetica", textColor=TEXTDARK, alignment=TA_RIGHT)),
                Paragraph(f"Date: <b>{inv_date_str}</b>", sty("N", fontSize=9,
                           fontName="Helvetica", textColor=TEXTDARK, alignment=TA_RIGHT)),
                Paragraph(f"Pay Mode: <b>{getattr(invoice, 'pay_mode', '—')}</b>", sty("N",
                           fontSize=9, fontName="Helvetica", textColor=TEXTDARK, alignment=TA_RIGHT)),
            ],
        ]
    ]
    header_tbl = Table(header_data, colWidths=[(W - 2*MARGIN)*0.55, (W - 2*MARGIN)*0.45])
    header_tbl.setStyle(TableStyle([
        ("VALIGN",      (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",(0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0,0), (-1,-1), 0),
    ]))
    story.append(header_tbl)
    story.append(HRFlowable(width="100%", thickness=2, color=GOLD, spaceAfter=8, spaceBefore=8))

    # ── Bill To ───────────────────────────────────────────────
    cust_name  = getattr(invoice, "customer_name",   "—")
    cust_mob   = getattr(invoice, "customer_mobile", "—")
    cust_pan   = getattr(invoice, "customer_pan",    None)
    cust_gstin = getattr(invoice, "customer_gstin",  None)
    cust_state = getattr(invoice, "customer_state",  None)

    bill_rows = [
        [Paragraph("<b>Bill To</b>", GOLD_H), ""],
        [Paragraph(cust_name, H3), Paragraph(f"Mobile: {cust_mob}", RIG)],
    ]
    if cust_gstin:
        bill_rows.append([Paragraph(f"GSTIN: {cust_gstin}", NOR), ""])
    if cust_state:
        bill_rows.append([Paragraph(f"State: {cust_state}", NOR), ""])
    if cust_pan:
        bill_rows.append([Paragraph(f"PAN: {cust_pan}", NOR), ""])

    bill_tbl = Table(bill_rows, colWidths=[(W-2*MARGIN)*0.6, (W-2*MARGIN)*0.4])
    bill_tbl.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), MIDGREY),
        ("SPAN",        (0,0), (-1,0)),
        ("LEFTPADDING", (0,0), (-1,-1), 6),
        ("RIGHTPADDING",(0,0), (-1,-1), 6),
        ("TOPPADDING",  (0,0), (-1,-1), 4),
        ("BOTTOMPADDING",(0,0),(-1,-1),4),
        ("BOX",         (0,0), (-1,-1), 0.5, MUTED),
        ("LINEBELOW",   (0,0), (-1, 0), 0.5, MUTED),
    ]))
    story.append(bill_tbl)
    story.append(Spacer(1, 6))

    # ── Line items table ──────────────────────────────────────
    th_style = sty("N", fontSize=8, fontName="Helvetica-Bold", textColor=white, alignment=TA_CENTER)

    col_headers = ["#", "Description", "HSN", "Qty", "Unit", "Rate (₹)", "Making (₹)", "Amount (₹)"]
    col_widths  = [8*mm, 62*mm, 18*mm, 14*mm, 12*mm, 22*mm, 22*mm, 24*mm]

    item_rows = [[Paragraph(h, th_style) for h in col_headers]]

    for i, item in enumerate(items, start=1):
        desc  = getattr(item, "description",   "—")
        hsn   = getattr(item, "hsn_code",      "7113")
        qty   = getattr(item, "qty",           0)
        unit  = getattr(item, "unit",          "")
        rate  = getattr(item, "rate",          0)
        mkg   = getattr(item, "making_charges", 0)
        amt   = getattr(item, "amount",        0)
        purity = getattr(item, "purity", None)
        desc_full = f"{desc}" + (f" ({purity})" if purity else "")

        item_rows.append([
            Paragraph(str(i),           CEN),
            Paragraph(desc_full,        NOR),
            Paragraph(str(hsn),         CEN),
            Paragraph(f"{float(qty):.3f}".rstrip("0").rstrip("."), CEN),
            Paragraph(str(unit)[:3] if unit else "—", CEN),
            Paragraph(f"{float(rate):,.2f}", RIG),
            Paragraph(f"{float(mkg):,.2f}",  RIG),
            Paragraph(f"{float(amt):,.2f}",  RIG),
        ])

    # Alternating row fill
    items_tbl = Table(item_rows, colWidths=col_widths, repeatRows=1)
    item_style = [
        ("BACKGROUND",   (0, 0), (-1, 0),  DARKBG),
        ("TEXTCOLOR",    (0, 0), (-1, 0),  white),
        ("FONTNAME",     (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",     (0, 1), (-1, -1), 8),
        ("GRID",         (0, 0), (-1, -1), 0.3, MUTED),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",   (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 4),
        ("LEFTPADDING",  (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    for j in range(1, len(item_rows)):
        if j % 2 == 0:
            item_style.append(("BACKGROUND", (0, j), (-1, j), MIDGREY))
    items_tbl.setStyle(TableStyle(item_style))
    story.append(items_tbl)
    story.append(Spacer(1, 6))

    # ── Totals block ──────────────────────────────────────────
    def _f(attr):
        return float(getattr(invoice, attr, 0) or 0)

    gst_type   = getattr(invoice, "gst_type", "CGST_SGST")
    subtotal   = _f("subtotal")
    cgst       = _f("cgst")
    sgst       = _f("sgst")
    igst       = _f("igst")
    tcs_amt    = _f("tcs_amount")
    grand_tot  = _f("grand_total")
    amt_paid   = _f("amount_paid")
    outstanding= _f("outstanding")
    gst_rate   = _f("gst_rate")

    totals = [
        ["", "Subtotal",                 _inr(subtotal)],
    ]
    if str(gst_type) in ("CGST_SGST", "cgst_sgst"):
        totals.append(["", f"CGST @ {gst_rate/2:.1f}%",   _inr(cgst)])
        totals.append(["", f"SGST @ {gst_rate/2:.1f}%",   _inr(sgst)])
    else:
        totals.append(["", f"IGST @ {gst_rate:.1f}%",     _inr(igst)])
    if tcs_amt > 0:
        totals.append(["", "TCS @ 1% (Sec 206C(1F))",     _inr(tcs_amt)])

    totals.append(["", "", ""])  # divider row
    totals.append(["", "Grand Total",               _inr(grand_tot)])
    totals.append(["", "Amount Paid",               _inr(amt_paid)])
    totals.append(["", "Outstanding",               _inr(outstanding)])

    usable = W - 2*MARGIN
    tot_tbl = Table(
        [[Paragraph(c if isinstance(c, str) else c, RIG) for c in row] for row in totals],
        colWidths=[usable*0.45, usable*0.35, usable*0.20],
    )
    tot_style = [
        ("FONTNAME",     (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE",     (0, 0), (-1, -1), 8),
        ("ALIGN",        (2, 0), (2, -1), "RIGHT"),
        ("ALIGN",        (1, 0), (1, -1), "RIGHT"),
        ("TOPPADDING",   (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        # Grand total row bold
        ("FONTNAME",     (1, len(totals)-3), (2, len(totals)-3), "Helvetica-Bold"),
        ("FONTSIZE",     (1, len(totals)-3), (2, len(totals)-3), 10),
        ("TEXTCOLOR",    (1, len(totals)-3), (2, len(totals)-3), GOLD),
        ("LINEABOVE",    (1, len(totals)-3), (2, len(totals)-3), 1, black),
        # Outstanding row colour
        ("TEXTCOLOR",    (2, len(totals)-1), (2, len(totals)-1), HexColor("#dc2626")),
    ]
    tot_tbl.setStyle(TableStyle(tot_style))
    story.append(tot_tbl)

    # ── Notes / Terms ─────────────────────────────────────────
    notes = getattr(invoice, "notes", None)
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=0.5, color=MUTED, spaceAfter=6))

    if notes:
        story.append(Paragraph(f"<b>Notes:</b> {notes}", NOR))
        story.append(Spacer(1, 4))

    story.append(Paragraph(
        "This is a computer-generated document. E&amp;OE. "
        "Disputes subject to local jurisdiction.",
        MUT,
    ))

    story.append(Spacer(1, 14))
    # Signature line
    sig_data = [["", "Authorised Signatory"]]
    sig_tbl  = Table(sig_data, colWidths=[usable*0.6, usable*0.4])
    sig_tbl.setStyle(TableStyle([
        ("LINEABOVE", (1,0),(1,0), 0.7, black),
        ("ALIGN",     (1,0),(1,0), "CENTER"),
        ("FONTSIZE",  (0,0),(-1,-1), 7),
        ("TEXTCOLOR", (0,0),(-1,-1), MUTED),
        ("TOPPADDING",(0,0),(-1,-1), 4),
    ]))
    story.append(sig_tbl)

    doc.build(story)
    return buf.getvalue()
