"""Auto-trade cron job wrapper (per broker account)."""
from __future__ import annotations

import logging

from ..brokers.base import BrokerAdapter
from ..config import Settings
from ..db import session_scope
from ..services.accounts import (
    AccountInfo,
    list_active_accounts,
    resolve_primary_account_ref,
)
from ..services.auto_trade import AutoTradeOutcome, run_auto_trade_pass
from ..services.email import EmailSender

logger = logging.getLogger(__name__)


def _send_summary(
    emailer: EmailSender, email_to: str, outcomes: list[AutoTradeOutcome]
) -> None:
    """Log + email a one-line-per-outcome auto-trade summary (no-op if no outcomes)."""
    placed = [o for o in outcomes if o.placed]
    rejected = [o for o in outcomes if not o.placed]
    logger.info("run_auto_trade_job: %d placed, %d rejected/skipped", len(placed), len(rejected))
    if not outcomes:
        return
    try:
        subject = f"Auto-trade summary: {len(placed)} placed, {len(rejected)} skipped"
        lines = ["Auto-trade pass completed.", ""]
        for o in outcomes:
            if o.placed:
                lines.append(
                    f"  PLACED  sug-{o.suggestion_id}"
                    f" broker_order_id={o.broker_order_id} dry_run={o.dry_run}"
                )
            else:
                lines.append(f"  SKIPPED sug-{o.suggestion_id} reason={o.rejected_reason}")
        body = "\n".join(lines)
        emailer.send(to=email_to, subject=subject, html=f"<pre>{body}</pre>", text=body)
    except Exception as exc:
        logger.error("auto_trade summary email failed: %s", exc)


def run_auto_trade_job_for_account(
    settings: Settings,
    adapter: BrokerAdapter,
    emailer: EmailSender,
    account: AccountInfo,
) -> list[AutoTradeOutcome]:
    """Run ONE account's auto-trade pass (its own session for isolation); return outcomes.

    The broker string written to ``order_execution.broker`` is sourced from the account
    row (``account.broker`` — the family, e.g. "alpaca"), NEVER ``settings.broker`` (the
    "_paper"/"_live" variant). This keeps the column identical to what
    ``persist_reconciliation`` writes for the same account, so a later fill upserts the
    placement row in place instead of inserting a duplicate filled row and leaving the
    original stuck at ``accepted_for_routing``. Does NOT email — the caller summarises.
    """
    with session_scope() as session:
        return run_auto_trade_pass(
            session=session,
            adapter=adapter,
            emailer=emailer,
            email_to=settings.email_to,
            broker=account.broker,
            broker_account_id=account.account_ref,
        )


def run_auto_trade_job_all_brokers(
    settings: Settings,
    emailer: EmailSender,
    adapters: dict[int, BrokerAdapter],
) -> None:
    """Run an auto-trade pass for every active broker account (each in its own session
    for isolation) and send one aggregated summary email."""
    with session_scope() as session:
        accounts = list_active_accounts(session)

    all_outcomes: list[AutoTradeOutcome] = []
    for acct in accounts:
        adapter = adapters.get(acct.account_ref)
        if adapter is None:
            logger.warning(
                "auto_trade: no adapter for account_ref=%s (%s); skipping",
                acct.account_ref, acct.nickname,
            )
            continue
        try:
            all_outcomes.extend(
                run_auto_trade_job_for_account(settings, adapter, emailer, acct)
            )
        except Exception:
            logger.exception(
                "auto-trade failed for account_ref=%s (%s); continuing",
                acct.account_ref, acct.nickname,
            )
            continue

    _send_summary(emailer, settings.email_to, all_outcomes)


def run_auto_trade_job(
    settings: Settings,
    adapter: BrokerAdapter,
    emailer: EmailSender,
    account: AccountInfo | None = None,
) -> None:
    """Single-account auto-trade pass. Defaults to the primary account (back-compat).

    Pass ``account`` to trade a SPECIFIC account (the per-account admin trigger does
    this); omit it for the primary-account path. Both the traded account_ref and the
    broker string come from ``account`` — never from ``settings`` — so the account the
    caller asked for is the one actually traded (and the broker string matches
    reconciliation's). The earlier code hardcoded the primary ref + ``settings.broker``,
    which (a) ran the primary's suggestions through whatever adapter was passed and
    (b) wrote a drifting broker string.
    """
    if account is None:
        with session_scope() as session:
            primary_ref = resolve_primary_account_ref(session)
            accounts = list_active_accounts(session)
        account = next((a for a in accounts if a.account_ref == primary_ref), None)
    if account is None:
        logger.warning("run_auto_trade_job: no active broker account — skipping")
        return
    outcomes = run_auto_trade_job_for_account(settings, adapter, emailer, account)
    _send_summary(emailer, settings.email_to, outcomes)
