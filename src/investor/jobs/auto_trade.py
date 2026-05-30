"""Auto-trade cron job wrapper (per broker account)."""
from __future__ import annotations

import logging

from ..brokers.base import BrokerAdapter
from ..config import Settings
from ..db import session_scope
from ..services.accounts import list_active_accounts, resolve_primary_account_ref
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
            with session_scope() as session:
                outcomes = run_auto_trade_pass(
                    session=session,
                    adapter=adapter,
                    emailer=emailer,
                    email_to=settings.email_to,
                    broker=acct.broker,
                    broker_account_id=acct.account_ref,
                )
            all_outcomes.extend(outcomes)
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
) -> None:
    """Single-broker (primary account) auto-trade pass. Back-compat entrypoint."""
    with session_scope() as session:
        primary_ref = resolve_primary_account_ref(session)
        outcomes = run_auto_trade_pass(
            session=session,
            adapter=adapter,
            emailer=emailer,
            email_to=settings.email_to,
            broker=settings.broker,
            broker_account_id=primary_ref,
        )
    _send_summary(emailer, settings.email_to, outcomes)
