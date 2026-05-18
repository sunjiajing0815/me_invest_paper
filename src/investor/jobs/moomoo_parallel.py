"""Moomoo parallel-run job: compare Moomoo positions against Alpaca.

Runs daily after reconciliation (16:50 ET Mon-Fri). Logs divergences but
does NOT email -- divergences surface in the Friday weekly review email instead.
"""

from __future__ import annotations

import logging

from ..brokers.base import BrokerAdapter
from ..config import Settings

logger = logging.getLogger(__name__)


def run_moomoo_parallel(settings: Settings, alpaca_adapter: BrokerAdapter) -> None:
    """Compare Moomoo positions + account against Alpaca. Log divergences."""
    try:
        from ..brokers.moomoo import MoomooAdapter

        moomoo = MoomooAdapter(
            host=settings.opend_host,
            port=settings.opend_port,
            paper=False,
            security_firm=settings.opend_security_firm,
        )
    except Exception as exc:
        logger.warning(
            "run_moomoo_parallel: MoomooAdapter init failed (OpenD unavailable?): %s", exc
        )
        return

    try:
        moomoo_positions = {p.ticker: p for p in moomoo.get_positions()}
        alpaca_positions = {p.ticker: p for p in alpaca_adapter.get_positions()}

        moomoo_account = moomoo.get_account()
        alpaca_account = alpaca_adapter.get_account()

        # Compare qty and avg_cost per ticker
        all_tickers = set(moomoo_positions) | set(alpaca_positions)
        divergences = []
        for ticker in sorted(all_tickers):
            mp = moomoo_positions.get(ticker)
            ap = alpaca_positions.get(ticker)
            if mp is None:
                logger.warning(
                    "moomoo_parallel: %s present in Alpaca (qty=%.2f) but not in Moomoo",
                    ticker,
                    ap.qty if ap else 0,
                )
                divergences.append(ticker)
                continue
            if ap is None:
                logger.warning(
                    "moomoo_parallel: %s present in Moomoo (qty=%.2f) but not in Alpaca",
                    ticker,
                    mp.qty,
                )
                divergences.append(ticker)
                continue
            qty_diff = abs(mp.qty - ap.qty)
            cost_diff_pct = (
                abs(mp.avg_cost - ap.avg_cost) / ap.avg_cost * 100 if ap.avg_cost else 0.0
            )
            if qty_diff > 0.01:
                logger.warning(
                    "moomoo_parallel: %s qty divergence -- Moomoo=%.4f Alpaca=%.4f (diff=%.4f)",
                    ticker,
                    mp.qty,
                    ap.qty,
                    qty_diff,
                )
                divergences.append(ticker)
            elif cost_diff_pct > 1.0:
                logger.warning(
                    "moomoo_parallel: %s avg_cost divergence -- Moomoo=%.4f Alpaca=%.4f (%.2f%%)",
                    ticker,
                    mp.avg_cost,
                    ap.avg_cost,
                    cost_diff_pct,
                )
                divergences.append(ticker)

        # Account-level comparison
        equity_diff_pct = (
            abs(moomoo_account.equity_usd - alpaca_account.equity_usd)
            / alpaca_account.equity_usd
            * 100
            if alpaca_account.equity_usd
            else 0.0
        )
        if equity_diff_pct > 2.0:
            logger.warning(
                "moomoo_parallel: equity divergence -- Moomoo=$%.2f Alpaca=$%.2f (%.2f%%)",
                moomoo_account.equity_usd,
                alpaca_account.equity_usd,
                equity_diff_pct,
            )

        if not divergences:
            logger.info(
                "moomoo_parallel: all positions match (checked %d tickers)", len(all_tickers)
            )
        else:
            logger.warning(
                "moomoo_parallel: %d divergence(s) found in %d tickers",
                len(divergences),
                len(all_tickers),
            )

    finally:
        moomoo.close()
