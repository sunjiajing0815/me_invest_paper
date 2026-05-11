# ADR-0010 — HMAC Magic-Link Auth for Suggestion Accept/Reject

**Date:** 2026-05-11
**Status:** Accepted
**Deciders:** Jane

---

## Context

The weekly suggestions email contains Accept and Reject buttons for each pending `order_suggestion`. The user needs to act on these from their inbox without going through a login screen — the app has no session-based auth at this phase. The mechanism must be:

- **Stateless** — no token table in the database.
- **Time-bounded** — links should expire after one week to match the weekly cadence.
- **Tamper-proof** — a link for one suggestion must not be replayable against a different suggestion or action.
- **Single-use** — a second click on the same link must not silently re-accept or re-reject.

## Decision

**HMAC-SHA256 magic links signed over `"{sid}:{action}:{expires}"`.**

Token format: `"{expires}.{hex32}"` where `expires` is a Unix timestamp (integer seconds UTC) and `hex32` is the first 32 hex characters of the HMAC digest.

Token generation:

```python
import hashlib, hmac, time

def make_magic_token(sid: int, action: str, secret: str, ttl: int = 604800) -> str:
    expires = int(time.time()) + ttl
    payload = f"{sid}:{action}:{expires}"
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{expires}.{digest}"
```

Verification uses `hmac.compare_digest()` to prevent timing attacks. A token is rejected if:

1. The HMAC does not match (tampered or wrong secret).
2. The current time exceeds `expires` (TTL exceeded).
3. The suggestion's `status` is not `"pending"` (second-click returns HTTP 409 Conflict).

`MAGIC_LINK_SECRET` is a distinct env var from `ADMIN_TOKEN`. They have different rotation cadences: `ADMIN_TOKEN` is long-lived infrastructure; `MAGIC_LINK_SECRET` is rotated after a suspected inbox compromise.

TTL is 7 days (`604800` seconds), matching the weekly suggestion cycle.

## Alternatives considered

| Option | Verdict | Reason |
|---|---|---|
| Signed JWT | Rejected | Heavier dependency; HMAC-SHA256 over a simple string is sufficient |
| One-time tokens stored in DB | Rejected | Requires a token table and cleanup job; breaks the stateless requirement |
| Session cookie login | Deferred | Phase 5 (multi-user); a full auth layer is premature for single-user Phase 3 |
| Unsigned URL with suggestion ID | Rejected | No tamper-proofing; any recipient could accept/reject any suggestion |

## Consequences

- Rotating `MAGIC_LINK_SECRET` invalidates every magic link currently in the user's inbox. Rotate only after the current week's suggestions have been acted on or expired naturally.
- Because the action (`accept` or `reject`) is embedded in the signed payload, an accept-signed token cannot be replayed as a reject — attempting to do so produces an HMAC mismatch.
- No database token storage is needed. The `status == "pending"` check on the `order_suggestion` row is the sole idempotency guard.
- If the user forwards the email to another person, that person can act on the links. This is acceptable for a single-user deployment; Phase 5 multi-user will add per-user HMAC keys.
