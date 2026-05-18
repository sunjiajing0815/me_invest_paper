"""Auto-trade cron job wrapper."""
from __future__ import annotations

import logging

from ..brokers.base import BrokerAdapter
from ..config import Settings
from ..db import session_scope
from ..services.auto_trade import run_auto_trade_pass
from ..services.email import EmailSender

logger = logging.getLogger(__name__)


def run_auto_trade_job(
    settings: Settings,
    adapter: BrokerAdapter,
    emailer: EmailSender,
) -> None:
    """Run a single auto-trade pass and send a daily summary email."""
    with session_scope() as session:
        outcomes = run_auto_trade_pass(
            session=session,
            adapter=adapter,
            emailer=emailer,
            email_to=settings.email_to,
            broker=settings.broker,
        )

    placed = [o for o in outcomes if o.placed]
    rejected = [o for o in outcomes if not o.placed]

    logger.info(
        "run_auto_trade_job: %d placed, %d rejected/skipped",
        len(placed), len(rejected),
    )

    # Daily summary email
    if outcomes:
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
                    lines.append(
                        f"  SKIPPED sug-{o.suggestion_id} reason={o.rejected_reason}"
                    )
            body = "\n".join(lines)
            emailer.send(
                to=settings.email_to,
                subject=subject,
                html=f"<pre>{body}</pre>",
                text=body,
            )
        except Exception as exc:
            logger.error("auto_trade summary email failed: %s", exc)
