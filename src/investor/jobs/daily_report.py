"""Daily report job: sync positions, compose report, render templates, send email."""

from __future__ import annotations

import logging

from ..brokers.base import BrokerAdapter
from ..config import Settings, load_targets
from ..db import session_scope
from ..services.bars import update_bars
from ..services.daily_report import compose_daily_report
from ..services.email import EmailSender
from ..services.render import render_template
from ..services.snapshot import take_snapshot

logger = logging.getLogger(__name__)


def run_daily_report(settings: Settings, adapter: BrokerAdapter, emailer: EmailSender) -> None:
    """Sync positions, compose daily report, render, and email. Re-raises on failure."""
    logger.info("run_daily_report started")

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
        take_snapshot(adapter, session, settings)
        report = compose_daily_report(
            session,
            watchlist=targets.watchlist,
            bars_dir=settings.bars_dir,
        )

    html = render_template("daily_report.html.j2", report=report)
    text = render_template("daily_report.txt.j2", report=report)

    subject = f"Portfolio — {report.date:%Y-%m-%d}"
    if report.account:
        subject += f" (equity ${report.account.equity_usd:,.0f})"

    emailer.send(to=settings.email_to, subject=subject, html=html, text=text)
    logger.info("run_daily_report completed: email sent to %s", settings.email_to)
