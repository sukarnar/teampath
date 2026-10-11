"""Outbound email. Without SMTP settings, messages are logged (development only)."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .config import get_settings

log = logging.getLogger("teampath.mail")
OUTBOX: list[dict] = []  # last messages, used by tests and the dev console


def send_email(to: str, subject: str, body: str, from_name: str | None = None) -> None:
    s = get_settings()
    OUTBOX.append({"to": to, "subject": subject, "body": body})
    del OUTBOX[:-50]
    if not s.smtp_host:
        if s.is_production:
            raise RuntimeError("SMTP is not configured")
        log.warning("DEV EMAIL to=%s subject=%s\n%s", to, subject, body)
        return
    msg = EmailMessage()
    msg["From"] = f"{from_name or s.app_name} <{s.smtp_from}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
        if s.smtp_starttls:
            smtp.starttls()
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(msg)
