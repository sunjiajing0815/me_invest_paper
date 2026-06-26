#!/usr/bin/env python
"""Read-only data-integrity audit (soak-window P0.2).

Bounds the unknown after the ADR-0026 SQLite-WAL silent-data-loss fix: runs internal-
consistency checks across the OLTP tables and prints a "recent writes" listing for the
operator to eyeball against memory / broker UI / email archive.

Read-only — never writes. Run in-container against the live named-volume DB:

    docker compose exec app uv run python scripts/audit_integrity.py
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sqlalchemy import func, select  # noqa: E402

from investor.config import Settings  # noqa: E402
from investor.db import init_db, session_scope  # noqa: E402
from investor.models import (  # noqa: E402
    AutoTradePromotionLog,
    AutoTradeState,
    BrokerAccount,
    Meta,
    OrderExecution,
    OrderSuggestion,
    PositionsSnapshot,
    TargetAllocation,
)
from investor.services.accounts import (  # noqa: E402
    list_active_accounts,
    resolve_primary_account_ref,
)
from investor.services.targets import targets_path_for_account, yaml_hash

# Lifecycle: pending → accepted/rejected/expired/cancelled; reconciliation flips a matched
# suggestion to "filled" (services/reconciliation.py).
_VALID_SUGGESTION_STATUS = {
    "pending", "accepted", "rejected", "expired", "cancelled", "filled",
}

_fail = 0
_warn = 0


def _ok(msg: str) -> None:
    print(f"  [PASS] {msg}")


def _bad(msg: str) -> None:
    global _fail
    _fail += 1
    print(f"  [FAIL] {msg}")


def _warning(msg: str) -> None:
    global _warn
    _warn += 1
    print(f"  [WARN] {msg}")


def check_target_allocation_open_rows(session) -> None:  # type: ignore[no-untyped-def]
    print("\n# target_allocation — one open row per (account, ticker)")
    dupes = session.execute(
        select(
            TargetAllocation.broker_account_id,
            TargetAllocation.ticker,
            func.count().label("n"),
        )
        .where(TargetAllocation.effective_to.is_(None))
        .group_by(TargetAllocation.broker_account_id, TargetAllocation.ticker)
        .having(func.count() > 1)
    ).all()
    if dupes:
        for d in dupes:
            _bad(f"account {d.broker_account_id} {d.ticker}: {d.n} open rows (expected 1)")
    else:
        _ok("no (account, ticker) has >1 open target row")


def check_target_yaml_hash(session, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    print("\n# target_allocation — DB hash matches each account's YAML (WAL-loss canary)")
    primary = resolve_primary_account_ref(session)
    for acct in list_active_accounts(session):
        tp = targets_path_for_account(
            settings, acct.account_ref, is_primary=(acct.account_ref == primary)
        )
        if tp is None or not Path(tp).exists():
            _warning(f"account {acct.account_ref} ({acct.nickname}): no targets YAML — skipped")
            continue
        stored = session.get(Meta, f"targets_yaml_hash:{acct.account_ref}")
        actual = yaml_hash(tp)
        if stored is None:
            _warning(f"account {acct.account_ref}: no stored hash (never reloaded?)")
        elif stored.value == actual:
            _ok(f"account {acct.account_ref} ({acct.nickname}): hash matches {Path(tp).name}")
        else:
            _bad(
                f"account {acct.account_ref} ({acct.nickname}): DB hash != YAML "
                f"({Path(tp).name}) — DB targets are STALE vs the file; reload-targets needed"
            )


def check_broker_account_open_rows(session) -> None:  # type: ignore[no-untyped-def]
    print("\n# broker_account — one open state row per account_ref")
    dupes = session.execute(
        select(BrokerAccount.account_ref, func.count().label("n"))
        .where(BrokerAccount.effective_to.is_(None))
        .group_by(BrokerAccount.account_ref)
        .having(func.count() > 1)
    ).all()
    if dupes:
        for d in dupes:
            _bad(f"account_ref {d.account_ref}: {d.n} open rows (expected 1)")
    else:
        _ok("every account_ref has exactly one open state row")


def check_suggestion_integrity(session) -> None:  # type: ignore[no-untyped-def]
    print("\n# order_suggestion — status domain + accepted⇒acted_at")
    bad_status = session.scalars(
        select(OrderSuggestion.status).where(
            OrderSuggestion.status.not_in(_VALID_SUGGESTION_STATUS)
        ).distinct()
    ).all()
    if bad_status:
        _bad(f"unknown status values present: {sorted(bad_status)}")
    else:
        _ok(f"all statuses in {sorted(_VALID_SUGGESTION_STATUS)}")
    accepted_no_acted = session.scalar(
        select(func.count()).select_from(OrderSuggestion).where(
            OrderSuggestion.status == "accepted", OrderSuggestion.acted_at.is_(None)
        )
    )
    if accepted_no_acted:
        _bad(f"{accepted_no_acted} accepted suggestion(s) with NULL acted_at")
    else:
        _ok("every accepted suggestion has acted_at set")


def check_execution_uniqueness(session) -> None:  # type: ignore[no-untyped-def]
    print("\n# order_execution — (broker_order_id, broker) unique")
    dupes = session.execute(
        select(OrderExecution.broker_order_id, OrderExecution.broker, func.count().label("n"))
        .where(OrderExecution.broker_order_id.is_not(None))
        .group_by(OrderExecution.broker_order_id, OrderExecution.broker)
        .having(func.count() > 1)
    ).all()
    if dupes:
        for d in dupes:
            _bad(f"{d.broker}:{d.broker_order_id} appears {d.n} times")
    else:
        _ok("no duplicate (broker_order_id, broker) pairs")


def check_snapshot_batch_coherence(session) -> None:  # type: ignore[no-untyped-def]
    print("\n# positions_snapshot — one ts per sync batch (ADR-0033)")
    # The §9 per-row-as_of bug spreads ONE sync across many ts microseconds/seconds apart.
    # Real syncs are hours apart, so two distinct ts within a few seconds = a split batch.
    for acct in list_active_accounts(session):
        ts_list = sorted(
            session.scalars(
                select(PositionsSnapshot.ts)
                .where(PositionsSnapshot.broker_account_id == acct.account_ref)
                .distinct()
            ).all()
        )
        if not ts_list:
            _warning(f"account {acct.account_ref} ({acct.nickname}): no snapshots")
            continue
        close_pairs = sum(
            1 for a, b in zip(ts_list, ts_list[1:], strict=False)
            if (b - a).total_seconds() < 5
        )
        if close_pairs:
            _warning(
                f"account {acct.account_ref} ({acct.nickname}): {close_pairs} pair(s) of ts "
                f"<5s apart — suspected per-row as_of (split-batch) history (§9/ADR-0033)"
            )
        else:
            _ok(
                f"account {acct.account_ref} ({acct.nickname}): {len(ts_list)} sync batches, "
                "all >5s apart — coherent"
            )


def check_auto_trade_state(session) -> None:  # type: ignore[no-untyped-def]
    print("\n# auto_trade_state — current mode per account (informational)")
    for st in session.scalars(select(AutoTradeState)).all():
        print(f"    account {st.broker_account_id}: mode={st.mode}")
    print("  (cross-check the latest promotion log below against these)")


def recent_writes(session) -> None:  # type: ignore[no-untyped-def]
    print("\n" + "=" * 70)
    print("RECENT WRITES — eyeball these against your memory / broker UI / email")
    print("=" * 70)

    print("\n# broker_account roster (account_ref, nickname, broker)")
    for a in list_active_accounts(session):
        print(f"    {a.account_ref}  {a.nickname}  {a.broker}")

    print("\n# last 10 target reloads (distinct effective_from per account)")
    rows = session.execute(
        select(
            TargetAllocation.broker_account_id,
            TargetAllocation.effective_from,
            func.count().label("n"),
        )
        .group_by(TargetAllocation.broker_account_id, TargetAllocation.effective_from)
        .order_by(TargetAllocation.effective_from.desc())
        .limit(10)
    ).all()
    for r in rows:
        print(f"    acct {r.broker_account_id}  {r.effective_from}  ({r.n} tickers)")

    print("\n# last 10 auto-trade promotions")
    for p in session.scalars(
        select(AutoTradePromotionLog).order_by(AutoTradePromotionLog.ts.desc()).limit(10)
    ).all():
        print(f"    {p.ts}  {p.broker_scope}  {p.from_mode}→{p.to_mode}  ({p.actor})")

    print("\n# last 10 suggestion accept/reject actions")
    for s in session.scalars(
        select(OrderSuggestion)
        .where(OrderSuggestion.acted_at.is_not(None))
        .order_by(OrderSuggestion.acted_at.desc())
        .limit(10)
    ).all():
        print(
            f"    {s.acted_at}  acct {s.broker_account_id}  {s.ticker} {s.side}  → {s.status}"
        )

    print("\n# suggestion status distribution (all-time)")
    for status, n in Counter(
        session.scalars(select(OrderSuggestion.status)).all()
    ).most_common():
        print(f"    {status}: {n}")


def main() -> None:
    settings = Settings()
    init_db(settings.sqlite_path)
    print(f"Data-integrity audit — DB: {settings.sqlite_path}")
    with session_scope() as session:
        check_target_allocation_open_rows(session)
        check_target_yaml_hash(session, settings)
        check_broker_account_open_rows(session)
        check_suggestion_integrity(session)
        check_execution_uniqueness(session)
        check_snapshot_batch_coherence(session)
        check_auto_trade_state(session)
        recent_writes(session)
    print("\n" + "=" * 70)
    print(f"SUMMARY: {_fail} FAIL, {_warn} WARN. "
          + ("All consistency checks passed." if _fail == 0 else "Review FAILs above."))
    print("=" * 70)


if __name__ == "__main__":
    main()
