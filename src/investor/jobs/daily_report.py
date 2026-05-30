"""Daily report job: sync positions, compose report, render templates, send email.

Per-broker (Phase 4.9a): ``run_daily_report_all_brokers`` loops active accounts and
sends one email each (subject prefixed ``[nickname]``); one broker's failure is logged
and skipped so the rest still run. ``run_daily_report`` remains the single-broker
(primary-account) entrypoint used by the scheduler/admin trigger until B8 rewires them.
"""

from __future__ import annotations

import logging

from ..brokers.base import BrokerAdapter
from ..config import Settings, load_targets
from ..db import session_scope
from ..services.accounts import AccountInfo, list_active_accounts, resolve_primary_account_ref
from ..services.bars import update_bars
from ..services.daily_report import compose_daily_report
from ..services.email import EmailSender
from ..services.render import render_template
from ..services.snapshot import take_snapshot

logger = logging.getLogger(__name__)


def run_daily_report_for_account(
    settings: Settings,
    adapter: BrokerAdapter,
    emailer: EmailSender,
    account: AccountInfo,
) -> None:
    """Sync, compose, render and email the daily report for ONE broker account."""
    logger.info(
        "run_daily_report started for %s (account_ref=%s)",
        account.nickname, account.account_ref,
    )

    targets = load_targets(settings.targets_path)

    try:
        update_bars(
            tickers=targets.watchlist,
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
            bars_dir=settings.bars_dir,
        )
    except Exception as exc:
        logger.warning("update_bars failed; continuing with stale bars: %s", exc)

    with session_scope() as session:
        take_snapshot(adapter, session, settings, account.account_ref)
        report = compose_daily_report(
            session,
            broker_account_id=account.account_ref,
            watchlist=targets.watchlist,
            bars_dir=settings.bars_dir,
        )

    html = render_template("daily_report.html.j2", report=report, account=account)
    text = render_template("daily_report.txt.j2", report=report, account=account)

    subject = f"[{account.nickname}] Portfolio — {report.date:%Y-%m-%d}"
    if report.account:
        subject += f" (equity ${report.account.equity_usd:,.0f})"

    emailer.send(to=settings.email_to, subject=subject, html=html, text=text)
    logger.info(
        "run_daily_report completed for %s: email sent to %s",
        account.nickname, settings.email_to,
    )


def run_daily_report_all_brokers(
    settings: Settings,
    emailer: EmailSender,
    adapters: dict[int, BrokerAdapter],
) -> None:
    """Run the daily report for every active broker account, isolating failures."""
    with session_scope() as session:
        accounts = list_active_accounts(session)
    for account in accounts:
        adapter = adapters.get(account.account_ref)
        if adapter is None:
            logger.warning(
                "run_daily_report_all_brokers: no adapter for account_ref=%s (%s); skipping",
                account.account_ref, account.nickname,
            )
            continue
        try:
            run_daily_report_for_account(settings, adapter, emailer, account)
        except Exception:
            logger.exception(
                "daily report failed for account_ref=%s (%s); continuing",
                account.account_ref, account.nickname,
            )
            continue


def run_daily_report(settings: Settings, adapter: BrokerAdapter, emailer: EmailSender) -> None:
    """Single-broker (primary account) daily report. Back-compat entrypoint.

    Resolves the primary active account and runs the report for it. No-ops with a
    warning if no active account exists yet (a fresh DB before broker onboarding).
    """
    with session_scope() as session:
        accounts = list_active_accounts(session)
        primary_ref = resolve_primary_account_ref(session)
    account = next((a for a in accounts if a.account_ref == primary_ref), None)
    if account is None:
        logger.warning("run_daily_report: no active broker account — skipping")
        return
    run_daily_report_for_account(settings, adapter, emailer, account)
