"""APScheduler job wrapper for position sync.

Orchestration layer: opens a session, calls services, handles errors.
Services themselves are pure functions.
"""

from __future__ import annotations

import logging

from ..brokers import make_adapter
from ..brokers.base import BrokerAdapter
from ..config import Settings
from ..db import session_scope
from ..services.accounts import list_active_accounts, resolve_primary_account_ref
from ..services.snapshot import take_snapshot

logger = logging.getLogger(__name__)


def run_sync_for_account(
    adapter: BrokerAdapter, settings: Settings, broker_account_id: int
) -> int:
    """Sync ONE broker account through *its own* adapter and persist. Returns row count.

    The adapter MUST belong to ``broker_account_id`` (e.g.
    ``app.state.adapters[broker_account_id]``). Passing the primary adapter here is
    exactly the bug that wrote Alpaca's positions under the Moomoo account.
    """
    with session_scope() as session:
        n = take_snapshot(adapter, session, settings, broker_account_id)
    logger.info(
        "run_sync completed for account_ref=%s: %d position rows", broker_account_id, n
    )
    return n


def run_sync_all_brokers(
    settings: Settings, adapters: dict[int, BrokerAdapter]
) -> dict[int, int]:
    """Sync every active broker account through its own adapter, isolating failures.

    Returns ``{account_ref: rows_written}`` for the accounts that synced. One broker's
    failure (e.g. OpenD down) is logged and skipped so the rest still sync.
    """
    with session_scope() as session:
        accounts = list_active_accounts(session)
    results: dict[int, int] = {}
    for account in accounts:
        adapter = adapters.get(account.account_ref)
        if adapter is None:
            logger.warning(
                "run_sync_all_brokers: no adapter for account_ref=%s (%s); skipping",
                account.account_ref, account.nickname,
            )
            continue
        try:
            results[account.account_ref] = run_sync_for_account(
                adapter, settings, account.account_ref
            )
        except Exception:
            logger.exception(
                "sync failed for account_ref=%s (%s); continuing",
                account.account_ref, account.nickname,
            )
            continue
    return results


def run_sync_job(settings: Settings) -> None:
    """Single-broker (primary) sync via the configured primary adapter.

    Back-compat entrypoint (CLI / legacy callers). The multi-broker path is
    ``run_sync_all_brokers`` with per-account adapters from ``app.state.adapters``.
    """
    logger.info("run_sync_job started (broker=%s)", settings.broker)
    try:
        adapter = make_adapter(settings)
        with session_scope() as session:
            primary_ref = resolve_primary_account_ref(session)
        if primary_ref is None:
            logger.warning("run_sync_job: no active broker account — skipping")
            return
        run_sync_for_account(adapter, settings, primary_ref)
    except Exception:
        logger.exception("run_sync_job failed")
        raise
