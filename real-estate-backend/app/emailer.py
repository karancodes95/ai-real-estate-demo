"""Day-0 confirmation email via Zoho SMTP (mirrors warmup_send.py: stdlib
smtplib over SSL, app-password auth from .env).

Sync smtplib is run in a thread (asyncio.to_thread) so it never blocks the
event loop. Callers treat failures as soft: the chat reply must not depend on
the email going out. Every attempt is recorded in email_log by the caller.
"""

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .config import settings


def _build_confirmation(to_email: str, lead_name: str, summary: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{settings.agency_name} <{settings.zoho_email}>"
    msg["To"] = to_email
    msg["Subject"] = f"We received your enquiry — {settings.agency_name}"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    first_name = lead_name.split()[0] if lead_name.strip() else "there"
    msg.set_content(
        f"Hi {first_name},\n\n"
        f"Thanks for reaching out to {settings.agency_name}. "
        "Here's what we noted from your enquiry:\n\n"
        f"{summary}\n\n"
        "One of our property advisors will get back to you shortly. "
        f"If anything above looks off, just reply to this email.\n\n"
        f"Warm regards,\n{settings.agency_name}\n"
    )
    return msg


def send_confirmation_sync(to_email: str, lead_name: str, summary: str) -> None:
    """Blocking send. Raises on failure; caller decides how to record it.

    Skipped entirely (raises RuntimeError) when Zoho creds are absent so local
    dev without .env still works end-to-end minus the actual email.
    """
    if not settings.zoho_email or not settings.zoho_app_password:
        raise RuntimeError("zoho creds not configured; skipping send")
    msg = _build_confirmation(to_email, lead_name, summary)
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context) as smtp:
        smtp.login(settings.zoho_email, settings.zoho_app_password)
        smtp.send_message(msg)
