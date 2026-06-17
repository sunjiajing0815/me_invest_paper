"""Tests for email service classes."""

from __future__ import annotations

import pytest

from investor.services.email import FakeEmailer, SMTPEmailer


class TestFakeEmailer:
    def test_records_sent_email(self) -> None:
        emailer = FakeEmailer()
        emailer.send(to="user@example.com", subject="Test", html="<b>hi</b>", text="hi")
        assert len(emailer.sent) == 1
        assert emailer.sent[0]["to"] == "user@example.com"
        assert emailer.sent[0]["subject"] == "Test"
        assert emailer.sent[0]["html"] == "<b>hi</b>"
        assert emailer.sent[0]["text"] == "hi"

    def test_accumulates_multiple_sends(self) -> None:
        emailer = FakeEmailer()
        emailer.send(to="a@example.com", subject="S1", html="h1", text="t1")
        emailer.send(to="b@example.com", subject="S2", html="h2", text="t2")
        assert len(emailer.sent) == 2


    def test_records_inline_images(self) -> None:
        emailer = FakeEmailer()
        emailer.send(to="a@x.com", subject="s", html="h", text="t",
                     inline_images={"alloc_pie": b"\x89PNG..."})
        assert emailer.sent[0]["inline_images"] == {"alloc_pie": b"\x89PNG..."}


class TestSMTPEmailer:
    def test_raises_when_credentials_empty(self) -> None:
        emailer = SMTPEmailer(host="", port=587, user="", password="", from_addr="")
        with pytest.raises(ValueError, match="SMTP credentials are not configured"):
            emailer.send(to="x@example.com", subject="s", html="h", text="t")

    def test_build_message_plain_is_alternative(self) -> None:
        emailer = SMTPEmailer(host="h", port=587, user="u", password="p", from_addr="f@x.com")
        msg = emailer._build_message(to="t@x.com", subject="s", html="<b>h</b>", text="t",
                                     inline_images=None)
        assert msg.get_content_type() == "multipart/alternative"
        assert msg["Subject"] == "s" and msg["To"] == "t@x.com"

    def test_build_message_with_image_is_related_with_cid(self) -> None:
        emailer = SMTPEmailer(host="h", port=587, user="u", password="p", from_addr="f@x.com")
        msg = emailer._build_message(
            to="t@x.com", subject="s", html="<img src=cid:alloc_pie>", text="t",
            inline_images={"alloc_pie": b"\x89PNG\r\n\x1a\n"},
        )
        assert msg.get_content_type() == "multipart/related"
        parts = list(msg.walk())
        # one alternative container + one image part with the matching Content-ID
        assert any(p.get_content_type() == "multipart/alternative" for p in parts)
        images = [p for p in parts if p.get_content_type() == "image/png"]
        assert len(images) == 1
        assert images[0]["Content-ID"] == "<alloc_pie>"
        assert images[0].get("Content-Disposition", "").startswith("inline")
