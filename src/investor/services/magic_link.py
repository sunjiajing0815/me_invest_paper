"""HMAC-SHA256 magic-link signing and verification for suggestion accept/reject."""
from __future__ import annotations

import hashlib
import hmac
import time


def sign_action(sid: int, action: str, secret: str, ttl_seconds: int = 7 * 24 * 3600) -> str:
    """Return a time-bounded HMAC token for the given suggestion ID and action.

    Token format: "<expires_unix>.<hex_sig_32_chars>"
    """
    expires = int(time.time()) + ttl_seconds
    msg = f"{sid}:{action}:{expires}".encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:32]
    return f"{expires}.{sig}"


def verify_action(sid: int, action: str, token: str, secret: str) -> bool:
    """Return True if the token is valid, not expired, and was signed for this sid+action."""
    try:
        expires_s, sig = token.split(".", 1)
        expires = int(expires_s)
    except (ValueError, IndexError):
        return False
    if expires < int(time.time()):
        return False
    msg = f"{sid}:{action}:{expires}".encode()
    expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expected)
