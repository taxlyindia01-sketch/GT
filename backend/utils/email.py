# utils/email.py — Transactional email via aiosmtplib
"""
GoldTrader Pro — Email Notifications
=====================================
All outbound email is sent via SMTP (aiosmtplib, async).
Configuration comes from config.py / env vars:
    SMTP_HOST     = smtp.gmail.com
    SMTP_PORT     = 587
    SMTP_USER     = your-gmail@gmail.com
    SMTP_PASSWORD = app-password-16-chars
    FROM_EMAIL    = GoldTrader Pro <support@goldtraderpro.in>

If SMTP_USER or SMTP_PASSWORD is not set, every send() call is a silent
no-op — the app never crashes on missing email config.

Email types implemented:
  1. send_approval_email(email, name, company)   — admin approved Google user
  2. send_rejection_email(email, name)            — admin rejected Google user
  3. send_trial_expiry_reminder(email, name, days_left, company) — 3-day warning
  4. send_invoice_email(email, name, inv, company_name, pdf_bytes) — invoice PDF
  5. send_welcome_email(email, name, company, trial_days)   — on signup
"""

import logging
import mimetypes
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from typing import Optional

import aiosmtplib

from config import settings

log = logging.getLogger("goldtrader.email")

# ── Internal send helper ──────────────────────────────────────

async def _send(to_email: str, subject: str, html: str, text: str,
                attachment_bytes: Optional[bytes] = None,
                attachment_name: Optional[str] = None) -> bool:
    """
    Send one email.  Returns True on success, False on any failure.
    Never raises — caller should not crash if email fails.
    """
    if not settings.SMTP_USER or not settings.SMTP_PASSWORD:
        log.warning("Email skipped — SMTP_USER/SMTP_PASSWORD not configured. To: %s Subject: %s", to_email, subject)
        return False

    msg = MIMEMultipart("mixed")
    msg["From"]    = settings.FROM_EMAIL
    msg["To"]      = to_email
    msg["Subject"] = subject

    # HTML + plain-text alternative
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text, "plain", "utf-8"))
    alt.attach(MIMEText(html, "html",  "utf-8"))
    msg.attach(alt)

    # Optional PDF attachment
    if attachment_bytes and attachment_name:
        part = MIMEApplication(attachment_bytes, _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=attachment_name)
        msg.attach(part)

    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            start_tls=True,
            timeout=10,
        )
        log.info("Email sent → %s | %s", to_email, subject)
        return True
    except Exception as exc:
        log.error("Email failed → %s | %s | %s: %s", to_email, subject, type(exc).__name__, exc)
        return False


# ── Brand helpers ─────────────────────────────────────────────

_GOLD = "#c8900a"
_BG   = "#020c14"
_TEXT = "#e6f0f8"

def _wrap_html(title: str, body_html: str) -> str:
    """Wrap content in a branded email shell."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>{title}</title>
</head>
<body style="margin:0;padding:0;background:#f0f4f8;font-family:'Segoe UI',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f4f8;padding:32px 0;">
  <tr><td align="center">
    <table width="580" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.10);">

      <!-- Header -->
      <tr>
        <td style="background:{_BG};padding:28px 32px;border-bottom:3px solid {_GOLD};">
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td>
                <div style="font-size:22px;font-weight:900;color:{_GOLD};letter-spacing:1px;">GoldTrader Pro</div>
                <div style="font-size:11px;color:#5e8099;margin-top:2px;letter-spacing:0.5px;">by Taxly India</div>
              </td>
              <td align="right">
                <div style="width:40px;height:40px;background:{_GOLD};border-radius:8px;display:inline-flex;align-items:center;justify-content:center;">
                  <span style="color:#000;font-weight:900;font-size:16px;">GT</span>
                </div>
              </td>
            </tr>
          </table>
        </td>
      </tr>

      <!-- Body -->
      <tr>
        <td style="padding:32px 36px;">
          {body_html}
        </td>
      </tr>

      <!-- Footer -->
      <tr>
        <td style="background:#f8fafc;padding:20px 36px;border-top:1px solid #e2e8f0;">
          <p style="margin:0;font-size:11px;color:#94a3b8;line-height:1.6;">
            This email was sent by <strong>GoldTrader Pro</strong> (Taxly India Pvt. Ltd.).<br/>
            Need help? <a href="mailto:support@goldtraderpro.in" style="color:{_GOLD};">support@goldtraderpro.in</a>
            &nbsp;·&nbsp; <span>+91 88829 35471</span>
          </p>
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""


def _btn(url: str, label: str) -> str:
    return (f'<div style="margin:24px 0;">'
            f'<a href="{url}" style="display:inline-block;padding:12px 28px;'
            f'background:{_GOLD};color:#000;font-weight:700;font-size:14px;'
            f'border-radius:8px;text-decoration:none;">{label}</a></div>')


def _h1(text: str) -> str:
    return f'<h1 style="margin:0 0 8px;font-size:22px;font-weight:800;color:#1a1a2e;">{text}</h1>'


def _p(text: str) -> str:
    return f'<p style="margin:0 0 14px;font-size:14px;color:#374151;line-height:1.7;">{text}</p>'


def _highlight(text: str) -> str:
    return (f'<div style="background:#fffbeb;border-left:4px solid {_GOLD};'
            f'padding:14px 18px;border-radius:0 8px 8px 0;margin:18px 0;">'
            f'<span style="font-size:14px;color:#374151;">{text}</span></div>')


# ── Email #1 — Approval ───────────────────────────────────────

async def send_approval_email(email: str, name: str, company: str) -> bool:
    """
    Sent when a Taxly admin approves a Google-signup user.
    Tells them their trial period is over and they have full access.
    """
    subject = "✅ Your GoldTrader Pro account has been approved!"
    body_html = (
        _h1("You're approved! 🎉") +
        _p(f"Hi {name},") +
        _p(f"Great news — the Taxly admin has approved your <strong>{company}</strong> account on "
           f"<strong>GoldTrader Pro</strong>. You now have full, unlimited access to the CRM.") +
        _highlight("Your account is now active. Log in with the Google account you used to sign up.") +
        _btn("https://goldtrader-backend.onrender.com/", "Open GoldTrader Pro →") +
        _p("If you have any questions, reply to this email or contact our support team.") +
        _p("Welcome aboard,<br/><strong>The Taxly India Team</strong>")
    )
    plain = (f"Hi {name},\n\n"
             f"Your GoldTrader Pro account for {company} has been approved.\n"
             f"Log in at: https://goldtrader-backend.onrender.com/\n\n"
             f"— Taxly India")
    return await _send(email, subject, _wrap_html(subject, body_html), plain)


# ── Email #2 — Rejection ──────────────────────────────────────

async def send_rejection_email(email: str, name: str) -> bool:
    """Sent when admin rejects a Google-signup user."""
    subject = "GoldTrader Pro — Account not approved"
    body_html = (
        _h1("Account not approved") +
        _p(f"Hi {name},") +
        _p("We regret to inform you that your GoldTrader Pro account request was not approved "
           "at this time.") +
        _p("If you believe this is a mistake or would like to discuss your business requirements, "
           "please contact our team directly.") +
        _p("Regards,<br/><strong>The Taxly India Team</strong>")
    )
    plain = (f"Hi {name},\nYour GoldTrader Pro account was not approved.\n"
             f"Contact support@goldtraderpro.in for more info.\n— Taxly India")
    return await _send(email, subject, _wrap_html(subject, body_html), plain)


# ── Email #3 — Trial Expiry Reminder ─────────────────────────

async def send_trial_expiry_reminder(email: str, name: str, company: str,
                                      days_left: int) -> bool:
    """
    Sent proactively 3 days before trial expires.
    Triggered by a background scheduler or on each login during last 3 days.
    """
    urgency = "⚠️" if days_left <= 1 else "⏰"
    day_str = "day" if days_left == 1 else "days"
    subject = f"{urgency} GoldTrader Pro trial expires in {days_left} {day_str}"
    body_html = (
        _h1(f"Your trial ends in {days_left} {day_str}") +
        _p(f"Hi {name},") +
        _p(f"Your free trial for <strong>{company}</strong> on GoldTrader Pro will expire in "
           f"<strong>{days_left} {day_str}</strong>. After that, you'll need admin approval "
           f"to continue using the CRM.") +
        _highlight(f"Trial expiry is automatic — no data will be lost. Your account will be "
                   f"reviewed by the Taxly team within 1–2 business days.") +
        _p("To ensure uninterrupted access, please reach out to us before your trial ends:") +
        _btn("mailto:support@goldtraderpro.in?subject=GoldTrader%20Pro%20Access%20Request",
             "Request Full Access →") +
        _p("Or call us: <strong>+91 88829 35471</strong>") +
        _p("Thank you for choosing GoldTrader Pro.<br/><strong>— Taxly India</strong>")
    )
    plain = (f"Hi {name},\n"
             f"Your GoldTrader Pro trial for {company} expires in {days_left} {day_str}.\n"
             f"Contact support@goldtraderpro.in to request full access.\n"
             f"+91 88829 35471\n— Taxly India")
    return await _send(email, subject, _wrap_html(subject, body_html), plain)


# ── Email #4 — Welcome on Signup ─────────────────────────────

async def send_welcome_email(email: str, name: str, company: str,
                              trial_days: int = 10) -> bool:
    """Sent immediately on successful demo/Google signup."""
    subject = f"🏅 Welcome to GoldTrader Pro — your {trial_days}-day trial has started"
    body_html = (
        _h1(f"Welcome, {name}! 🎉") +
        _p(f"Your <strong>{company}</strong> account is ready. You have <strong>{trial_days} days</strong> "
           f"to explore every feature of GoldTrader Pro — GST invoicing, TCS tracking, "
           f"SFT compliance, FIFO stock, and the full CRM.") +
        _highlight(f"Trial period: {trial_days} days · All features unlocked · No credit card needed") +
        _btn("https://goldtrader-backend.onrender.com/", "Open GoldTrader Pro →") +
        "<hr style='border:none;border-top:1px solid #e2e8f0;margin:24px 0;'/>" +
        "<p style='font-size:13px;color:#374151;margin:0 0 8px;font-weight:700;'>Quick start:</p>" +
        "<ul style='font-size:13px;color:#374151;margin:0 0 16px;padding-left:20px;line-height:2;'>"
        "<li>Add your customers (Customers tab)</li>"
        "<li>Create your first GST invoice (+ New Invoice)</li>"
        "<li>Check TCS &amp; SFT reports (Reports tab)</li>"
        "</ul>" +
        _p("Need help? Reply to this email or call <strong>+91 88829 35471</strong>.") +
        _p("— <strong>The Taxly India Team</strong>")
    )
    plain = (f"Welcome {name}!\nYour {company} account on GoldTrader Pro is ready.\n"
             f"Trial: {trial_days} days, all features unlocked.\n"
             f"Open: https://goldtrader-backend.onrender.com/\n— Taxly India")
    return await _send(email, subject, _wrap_html(subject, body_html), plain)


# ── Email #5 — Invoice PDF ────────────────────────────────────

async def send_invoice_email(to_email: str, customer_name: str,
                              invoice_no: str, company_name: str,
                              grand_total: float,
                              pdf_bytes: Optional[bytes] = None) -> bool:
    """
    Send invoice to customer, optionally with PDF attachment.
    Called from POST /api/invoices/{id}/send-email endpoint.
    """
    subject = f"Invoice {invoice_no} from {company_name}"
    total_str = f"₹{grand_total:,.2f}"
    body_html = (
        _h1(f"Invoice {invoice_no}") +
        _p(f"Dear {customer_name},") +
        _p(f"Please find attached your invoice from <strong>{company_name}</strong>.") +
        _highlight(f"Invoice: <strong>{invoice_no}</strong> &nbsp;·&nbsp; Amount: <strong>{total_str}</strong>") +
        _p("For any queries about this invoice, please contact the store directly.") +
        _p("Thank you for your business.")
    )
    plain = (f"Dear {customer_name},\n"
             f"Invoice {invoice_no} — Amount: {total_str}\n"
             f"From: {company_name}\nThank you for your business.")
    attachment_name = f"{invoice_no}.pdf" if pdf_bytes else None
    return await _send(to_email, subject, _wrap_html(subject, body_html),
                       plain, pdf_bytes, attachment_name)
