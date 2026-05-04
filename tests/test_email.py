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


class TestSMTPEmailer:
    def test_raises_when_credentials_empty(self) -> None:
        emailer = SMTPEmailer(host="", port=587, user="", password="", from_addr="")
        with pytest.raises(ValueError, match="SMTP credentials are not configured"):
            emailer.send(to="x@example.com", subject="s", html="h", text="t")
