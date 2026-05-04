"""Email delivery: EmailSender Protocol, SMTPEmailer (real), FakeEmailer (testing)."""

from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Protocol


class EmailSender(Protocol):
    def send(self, *, to: str, subject: str, html: str, text: str) -> None: ...


class SMTPEmailer:
    """Send email via SMTP with STARTTLS (e.g. Gmail App Password)."""

    def __init__(self, host: str, port: int, user: str, password: str, from_addr: str) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._from = from_addr

    def send(self, *, to: str, subject: str, html: str, text: str) -> None:
        if not self._host or not self._user or not self._password:
            raise ValueError("SMTP credentials are not configured")

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self._from
        msg["To"] = to
        msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(self._host, self._port) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(self._user, self._password)
            smtp.sendmail(self._from, to, msg.as_string())


class FakeEmailer:
    """Records sent emails in memory — for use in tests."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def send(self, *, to: str, subject: str, html: str, text: str) -> None:
        self.sent.append({"to": to, "subject": subject, "html": html, "text": text})
