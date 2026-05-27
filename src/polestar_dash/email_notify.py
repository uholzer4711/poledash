"""Email notification sender via SMTP (Gmail App-Password compatible)."""

from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText

from .config import get_settings

log = logging.getLogger(__name__)


def email_send(subject: str, body: str, *, html: bool = False) -> None:
    """Send an email via SMTP. No-op if SMTP is not configured."""
    s = get_settings()
    if not (s.smtp_host and s.smtp_to):
        return

    mime_type = "html" if html else "plain"
    msg = MIMEText(body, mime_type, "utf-8")
    msg["Subject"] = subject
    msg["From"] = s.smtp_from or s.smtp_username or "poledash@localhost"
    msg["To"] = s.smtp_to

    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as srv:
            srv.ehlo()
            if s.smtp_port != 25:
                srv.starttls()
                srv.ehlo()
            if s.smtp_username and s.smtp_password:
                srv.login(s.smtp_username, s.smtp_password)
            srv.sendmail(msg["From"], [s.smtp_to], msg.as_string())
        log.info("Sent email to %s: %s", s.smtp_to, subject)
    except Exception:  # noqa: BLE001
        log.exception("Email send failed: %s", subject)
