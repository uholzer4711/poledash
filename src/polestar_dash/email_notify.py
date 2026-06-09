"""Email notification sender via SMTP (Gmail App-Password compatible)."""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

from .config import get_settings

log = logging.getLogger(__name__)


def email_send(
    subject: str, body: str, *, html: bool = False, text_alt: str | None = None
) -> None:
    """Send an email via SMTP. No-op if SMTP is not configured.

    When `html` is set and `text_alt` is given, a multipart/alternative message
    is built with the plain-text part first — HTML-only bodies with no text
    alternative are a strong spam signal. We also set Date and Message-ID: a
    missing Date/Message-ID makes Gmail file mail as spam (or drop it).
    """
    s = get_settings()
    if not (s.smtp_host and s.smtp_to):
        return

    sender = s.smtp_from or s.smtp_username or "poledash@localhost"
    if html and text_alt:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(text_alt, "plain", "utf-8"))
        msg.attach(MIMEText(body, "html", "utf-8"))
    else:
        msg = MIMEText(body, "html" if html else "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = s.smtp_to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])

    try:
        # Port 465 = implicit TLS (SMTPS): the socket is TLS from the first byte,
        # so we must use SMTP_SSL and must NOT issue STARTTLS. Port 587 (and any
        # other non-25 port) = explicit TLS via STARTTLS on a plain connection.
        # Port 25 = plaintext. Treating 465 like 587 (the old code) made every
        # 465 send fail.
        if s.smtp_port == 465:
            conn = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=15)
        else:
            conn = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15)
        with conn as srv:
            srv.ehlo()
            if s.smtp_port not in (25, 465):
                srv.starttls()
                srv.ehlo()
            if s.smtp_username and s.smtp_password:
                srv.login(s.smtp_username, s.smtp_password)
            srv.sendmail(msg["From"], [s.smtp_to], msg.as_string())
        log.info("Sent email to %s: %s", s.smtp_to, subject)
    except Exception:  # noqa: BLE001
        log.exception("Email send failed: %s", subject)
