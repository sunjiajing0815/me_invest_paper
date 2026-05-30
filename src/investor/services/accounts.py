"""Broker-account resolution helpers (Phase 4.9a multi-broker).

`account_ref` is the stable partition key on `broker_account` (constant across an
account's close-and-insert state rows). Every per-account table references it via
`broker_account_id`. These helpers resolve the active accounts for the per-broker
job loops and for endpoints that default to the primary account.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BrokerAccount


@dataclass(frozen=True)
class AccountInfo:
    """Session-safe snapshot of a broker account's identity (for job loops)."""

    account_ref: int
    nickname: str
    broker: str


def _active_rows(session: Session) -> list[BrokerAccount]:
    return list(
        session.scalars(
            select(BrokerAccount)
            .where(
                BrokerAccount.effective_to.is_(None),
                BrokerAccount.is_active.is_(True),
            )
            .order_by(BrokerAccount.last_sync.desc())
        ).all()
    )


def list_active_accounts(session: Session) -> list[AccountInfo]:
    """Return one AccountInfo per distinct active account (latest open row wins).

    Ordered primary-first (most recently synced). Frozen dataclasses are safe to use
    after the session closes — the canonical pattern for feeding job loops.
    """
    out: list[AccountInfo] = []
    seen: set[int] = set()
    for r in _active_rows(session):
        if r.account_ref is None or r.account_ref in seen:
            continue
        seen.add(r.account_ref)
        out.append(
            AccountInfo(
                account_ref=r.account_ref,
                nickname=r.nickname or r.broker,
                broker=r.broker,
            )
        )
    return out


def resolve_active_account_refs(session: Session) -> list[int]:
    """Return the account_ref of every active broker account (primary first)."""
    return [a.account_ref for a in list_active_accounts(session)]


def resolve_primary_account_ref(session: Session) -> int | None:
    """Return the primary (most-recently-synced active) account's account_ref, or None."""
    refs = resolve_active_account_refs(session)
    return refs[0] if refs else None
