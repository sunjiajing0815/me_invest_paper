"""Daily funds-flow detection job (P2.3, ADR-0035).

Per active broker account: detect an unexplained day-over-day cash move and, if found, persist a
``funds_event`` and email a notice naming the broker. Cross-broker transfers surface as two events
(same run) with a "consider whether these are one transfer" header note. Errors are isolated
per account.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from ..config import Settings
from ..db import session_scope
from ..models import FundsEvent
from ..services.accounts import AccountInfo, list_active_accounts
from ..services.email import EmailSender
from ..services.funds import FundsFlow, detect_funds_flow
from ..services.render import render_template

logger = logging.getLogger(__name__)


def run_funds_detection_all_brokers(settings: Settings, emailer: EmailSender) -> None:
    """Detect + persist + email external cash flows for every active broker account."""
    detected: list[tuple[AccountInfo, FundsFlow]] = []
    with session_scope() as session:
        accounts = list_active_accounts(session)
        for acct in accounts:
            try:
                flow = detect_funds_flow(
                    session, acct.account_ref,
                    threshold=settings.funds_detection_threshold_usd,
                )
            except Exception:
                logger.exception("funds_detection: failed for %s", acct.nickname)
                continue
            if flow is None:
                continue
            session.add(FundsEvent(
                broker_account_id=flow.broker_account_id, ts=datetime.now(UTC),
                delta_usd=flow.delta_usd, kind=flow.kind, prev_cash=flow.prev_cash,
                cur_cash=flow.cur_cash, trade_cash_flow=flow.trade_cash_flow, note=flow.note,
            ))
            detected.append((acct, flow))
            logger.info(
                "funds_detection: %s on %s — $%.0f (cash %.0f→%.0f, trades %.0f)",
                flow.kind, acct.nickname, flow.delta_usd, flow.prev_cash, flow.cur_cash,
                flow.trade_cash_flow,
            )

    if not detected:
        logger.info("funds_detection: no external flows over threshold")
        return

    multi = len(detected) > 1  # cross-broker transfer hint
    for acct, flow in detected:
        try:
            kw = dict(nickname=acct.nickname, broker=acct.broker, flow=flow, multi=multi)
            emailer.send(
                to=settings.email_to,
                subject=f"[{acct.nickname}] {flow.kind.title()} detected — "
                        f"${abs(flow.delta_usd):,.0f}",
                html=render_template("funds_event.html.j2", **kw),
                text=render_template("funds_event.txt.j2", **kw),
            )
        except Exception:
            logger.exception("funds_detection: email failed for %s", acct.nickname)
