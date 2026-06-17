"""Email delivery: EmailSender Protocol, SMTPEmailer (real), FakeEmailer (testing)."""

from __future__ import annotations

import smtplib
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Protocol


class EmailSender(Protocol):
    def send(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        text: str,
        inline_images: dict[str, bytes] | None = None,
    ) -> None:
        """Send an email. ``inline_images`` maps a Content-ID to PNG bytes; the HTML
        references each as ``<img src="cid:KEY">`` (rendered inline by mail clients)."""
        ...


class SMTPEmailer:
    """Send email via SMTP with STARTTLS (e.g. Gmail App Password)."""

    def __init__(self, host: str, port: int, user: str, password: str, from_addr: str) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._from = from_addr

    def _build_message(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        text: str,
        inline_images: dict[str, bytes] | None,
    ) -> MIMEMultipart:
        """Build the MIME message. Plain (no images) → ``multipart/alternative``;
        with inline images → ``multipart/related`` wrapping the alternative part plus
        one inline ``image/png`` per Content-ID."""
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(text, "plain"))
        alt.attach(MIMEText(html, "html"))

        root: MIMEMultipart
        if inline_images:
            root = MIMEMultipart("related")
            root.attach(alt)
            for cid, data in inline_images.items():
                img = MIMEImage(data, _subtype="png")
                img.add_header("Content-ID", f"<{cid}>")
                img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
                root.attach(img)
        else:
            root = alt

        root["Subject"] = subject
        root["From"] = self._from
        root["To"] = to
        return root

    def send(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        text: str,
        inline_images: dict[str, bytes] | None = None,
    ) -> None:
        if not self._host or not self._user or not self._password:
            raise ValueError("SMTP credentials are not configured")

        msg = self._build_message(
            to=to, subject=subject, html=html, text=text, inline_images=inline_images
        )

        with smtplib.SMTP(self._host, self._port) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(self._user, self._password)
            smtp.sendmail(self._from, to, msg.as_string())


class FakeEmailer:
    """Records sent emails in memory — for use in tests."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    def send(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        text: str,
        inline_images: dict[str, bytes] | None = None,
    ) -> None:
        self.sent.append({
            "to": to, "subject": subject, "html": html, "text": text,
            "inline_images": inline_images or {},
        })
