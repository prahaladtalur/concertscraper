"""Delivery channels: email via SMTP, optional SMS via Twilio.

Both degrade gracefully. With no credentials configured, email falls back to
writing rendered files into ./outbox/ and SMS becomes a logged no-op, so the
whole pipeline stays runnable before you've set anything up.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path

import httpx

from .config import Settings, get_settings
from .models import utcnow

log = logging.getLogger(__name__)

OUTBOX = Path("outbox")
TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

# Carriers split anything longer into multiple billed segments.
SMS_MAX_CHARS = 1000


def deliver_email(
    subject: str,
    text: str,
    html: str,
    settings: Settings | None = None,
    slug: str = "message",
) -> Path | None:
    """Send an email, or write it to ./outbox/ when dry-running.

    Returns the outbox path if it was written to disk, else None.
    """
    settings = settings or get_settings()

    if settings.email_dry_run or not settings.smtp_host:
        OUTBOX.mkdir(exist_ok=True)
        path = OUTBOX / f"{slug}-{utcnow():%Y%m%dT%H%M%SZ}.html"
        path.write_text(html, encoding="utf-8")
        path.with_suffix(".txt").write_text(text, encoding="utf-8")
        log.info("Dry run: wrote %s", path)
        return path

    if not settings.recipients:
        log.error("EMAIL_TO is empty; cannot send %r", subject)
        return None

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.email_from or settings.smtp_user
    msg["To"] = ", ".join(settings.recipients)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        # A dead mail server must not take the scheduler down with it.
        log.error("SMTP delivery failed for %r: %s", subject, exc)
        return None

    log.info("Emailed %r to %s", subject, settings.recipients)
    return None


def deliver_sms(body: str, settings: Settings | None = None) -> bool:
    """Send an SMS via Twilio's REST API. No-ops if unconfigured."""
    settings = settings or get_settings()
    if not settings.sms_configured:
        log.debug("SMS not configured; skipping text delivery")
        return False

    if len(body) > SMS_MAX_CHARS:
        body = body[: SMS_MAX_CHARS - 3] + "..."

    sent = 0
    for number in settings.sms_recipients:
        try:
            resp = httpx.post(
                TWILIO_URL.format(sid=settings.twilio_account_sid),
                auth=(settings.twilio_account_sid, settings.twilio_auth_token),
                data={
                    "From": settings.twilio_from_number,
                    "To": number,
                    "Body": body,
                },
                timeout=20.0,
            )
            resp.raise_for_status()
            sent += 1
        except httpx.HTTPError as exc:
            log.error("Twilio send to %s failed: %s", number, exc)

    log.info("Sent %d/%d SMS", sent, len(settings.sms_recipients))
    return sent > 0
