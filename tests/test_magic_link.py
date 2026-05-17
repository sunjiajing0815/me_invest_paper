"""Tests for HMAC magic-link sign/verify."""
from __future__ import annotations

import time
from unittest.mock import patch

from src.investor.services.magic_link import sign_action, verify_action

SECRET = "test-secret-32chars-xxxxxxxxxxxxx"


class TestSignAction:
    def test_returns_string_with_dot_separator(self):
        token = sign_action(1, "accept", SECRET)
        assert "." in token

    def test_format_expires_dot_32hexchars(self):
        token = sign_action(1, "accept", SECRET)
        parts = token.split(".", 1)
        assert len(parts) == 2
        expires_s, sig = parts
        assert expires_s.isdigit()
        assert len(sig) == 32
        assert all(c in "0123456789abcdef" for c in sig)

    def test_expires_is_in_the_future(self):
        token = sign_action(1, "accept", SECRET)
        expires_s, _ = token.split(".", 1)
        assert int(expires_s) > int(time.time())

    def test_default_ttl_is_one_week(self):
        before = int(time.time())
        token = sign_action(1, "accept", SECRET)
        after = int(time.time())
        expires_s, _ = token.split(".", 1)
        expires = int(expires_s)
        one_week = 7 * 24 * 3600
        assert before + one_week <= expires <= after + one_week + 1

    def test_custom_ttl(self):
        before = int(time.time())
        token = sign_action(1, "accept", SECRET, ttl_seconds=60)
        after = int(time.time())
        expires_s, _ = token.split(".", 1)
        expires = int(expires_s)
        assert before + 60 <= expires <= after + 61

    def test_different_actions_produce_different_sigs(self):
        t1 = sign_action(1, "accept", SECRET)
        t2 = sign_action(1, "reject", SECRET)
        _, sig1 = t1.split(".", 1)
        _, sig2 = t2.split(".", 1)
        assert sig1 != sig2

    def test_different_sids_produce_different_sigs(self):
        t1 = sign_action(1, "accept", SECRET)
        t2 = sign_action(2, "accept", SECRET)
        _, sig1 = t1.split(".", 1)
        _, sig2 = t2.split(".", 1)
        assert sig1 != sig2


class TestVerifyAction:
    def test_valid_token_returns_true(self):
        token = sign_action(1, "accept", SECRET)
        assert verify_action(1, "accept", token, SECRET) is True

    def test_wrong_action_returns_false(self):
        token = sign_action(1, "accept", SECRET)
        assert verify_action(1, "reject", token, SECRET) is False

    def test_wrong_sid_returns_false(self):
        token = sign_action(1, "accept", SECRET)
        assert verify_action(2, "accept", token, SECRET) is False

    def test_tampered_sig_returns_false(self):
        token = sign_action(1, "accept", SECRET)
        expires, sig = token.split(".", 1)
        # Flip one character in the sig
        bad_sig = ("a" if sig[0] != "a" else "b") + sig[1:]
        tampered = f"{expires}.{bad_sig}"
        assert verify_action(1, "accept", tampered, SECRET) is False

    def test_expired_token_returns_false(self):
        # Create a token with TTL=1s, then advance time past expiry
        token = sign_action(1, "accept", SECRET, ttl_seconds=1)
        with patch("src.investor.services.magic_link.time.time", return_value=time.time() + 10):
            assert verify_action(1, "accept", token, SECRET) is False

    def test_malformed_no_dot_returns_false(self):
        assert verify_action(1, "accept", "nodotintoken", SECRET) is False

    def test_malformed_non_numeric_expires_returns_false(self):
        assert (
            verify_action(1, "accept", "notanumber.abcdef12345678901234567890123456", SECRET)
            is False
        )

    def test_wrong_secret_returns_false(self):
        token = sign_action(1, "accept", SECRET)
        assert verify_action(1, "accept", token, "wrong-secret") is False

    def test_verify_roundtrip_with_custom_ttl(self):
        """Token signed with custom TTL verifies correctly before expiry."""
        token = sign_action(42, "reject", SECRET, ttl_seconds=3600)
        assert verify_action(42, "reject", token, SECRET) is True

    def test_empty_token_returns_false(self):
        assert verify_action(1, "accept", "", SECRET) is False

    def test_only_dot_returns_false(self):
        """Token that is just a dot has empty expires and empty sig — should fail."""
        assert verify_action(1, "accept", ".", SECRET) is False
