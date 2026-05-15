"""Daily movers job: detect >=5% weekly movers, triage news via LangGraph, email."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd

from ..brokers.base import BrokerAdapter
from ..config import Settings
from ..db import session_scope
from ..graphs.news_triage import (
    NewsTriageItem,
    NewsTriageState,
    build_news_triage_graph,
)
from ..models import MoverState, NewsEvent
from ..services.analytics import duckdb_conn
from ..services.email import EmailSender
from ..services.llm import HAIKU, SONNET, LLMClient
from ..services.news import NewsRaw, get_news_for_movers
from ..services.render import render_template

log = logging.getLogger(__name__)

THRESHOLD_STEP = 5.0


def _compute_movers(bars_dir: str) -> pd.DataFrame:
    """Return DataFrame(ticker, today_close, last_week_close, pct_change) for all tickers.

    Empty DataFrame if no bars exist.
    """
    with duckdb_conn(bars_dir) as con:
        try:
            df: pd.DataFrame = con.execute("""
                WITH today AS (
                    SELECT ticker, close AS today_close
                    FROM price_bar
                    WHERE date = (SELECT MAX(date) FROM price_bar)
                ),
                last_week AS (
                    SELECT ticker, close AS last_week_close
                    FROM price_bar
                    WHERE date = (
                        SELECT MAX(date) FROM price_bar
                        WHERE date <= (SELECT MAX(date) FROM price_bar) - INTERVAL 7 DAYS
                    )
                )
                SELECT today.ticker,
                       today_close,
                       last_week_close,
                       (today_close / last_week_close - 1) * 100 AS pct_change
                FROM today
                JOIN last_week USING (ticker)
            """).df()
            return df
        except Exception as exc:
            log.warning("movers DuckDB query failed: %s", exc)
            return pd.DataFrame(
                columns=["ticker", "today_close", "last_week_close", "pct_change"]
            )


def run_movers_email(
    settings: Settings,
    adapter: BrokerAdapter,
    emailer: EmailSender,
    llm: LLMClient,
) -> None:
    """Run daily movers detection, news triage, persistence, and email.

    Uses tiered threshold logic: fires at 5% first time, then 10%, then 15%, etc.
    Resets threshold when a ticker's move drops back below 5%.
    Never sends email if no ticker crossed a new threshold today.
    """
    log.info("run_movers_email started")

    # 1. Compute pct_change for all tickers vs last week's close
    movers_df = _compute_movers(settings.bars_dir)
    if movers_df.empty:
        log.info("no price data; skipping movers email")
        return

    # 2. Load MoverState for all known tickers — extract plain floats before session closes
    with session_scope() as s:
        states: dict[str, float] = {
            ms.ticker: ms.last_triggered_threshold for ms in s.query(MoverState).all()
        }

    # 3. Apply tiered threshold filter
    tickers_to_process: list[dict[str, Any]] = []  # list of {ticker, pct_change, threshold_hit}
    tickers_to_reset: list[str] = []

    for row in movers_df.itertuples(index=False):
        ticker: str = row.ticker
        pct: float = float(row.pct_change)
        last_triggered = states.get(ticker, 0.0)

        if abs(pct) < 5.0:
            if last_triggered > 0.0:
                tickers_to_reset.append(ticker)
        else:
            next_threshold = last_triggered + THRESHOLD_STEP
            if abs(pct) >= next_threshold:
                tickers_to_process.append({
                    "ticker": ticker,
                    "pct_change": pct,
                    "today_close": float(row.today_close),
                    "last_week_close": float(row.last_week_close),
                    "threshold_hit": next_threshold,
                })

    # Reset recovered tickers (abs(pct) dropped back below 5%)
    if tickers_to_reset:
        with session_scope() as s:
            for ticker in tickers_to_reset:
                s.merge(MoverState(
                    ticker=ticker,
                    last_triggered_threshold=0.0,
                    last_triggered_at=None,
                    last_pct_change=None,
                ))

    if not tickers_to_process:
        log.info("no new threshold crossings today; skipping movers email")
        return

    log.info(
        "movers crossing new threshold today: %s",
        [(t["ticker"], t["pct_change"]) for t in tickers_to_process],
    )

    # 4. Fetch news for tickers that crossed a new threshold
    since = datetime.now(UTC) - timedelta(hours=24)
    news_by_ticker: dict[str, list[NewsRaw]] = get_news_for_movers(
        [t["ticker"] for t in tickers_to_process],
        since,
        alpaca_api_key=settings.alpaca_api_key,
        alpaca_secret_key=settings.alpaca_secret_key,
        finnhub_api_key=settings.finnhub_api_key,
    )

    # 5. Build graph once, invoke per ticker
    graph: Any = build_news_triage_graph(settings.sqlite_path)
    final_by_ticker: dict[str, list[NewsTriageItem]] = {}
    arbitrated_hashes: set[str] = set()

    with session_scope() as session:
        for t_info in tickers_to_process:
            ticker = t_info["ticker"]
            raws = news_by_ticker.get(ticker, [])
            if not raws:
                final_by_ticker[ticker] = []
                continue
            try:
                result = graph.invoke(
                    NewsTriageState(
                        ticker=ticker,
                        raws=raws,
                        classifications=[],
                        flagged=[],
                        final=[],
                        telemetry={},
                    ),
                    config={
                        "configurable": {
                            "thread_id": f"news-{ticker}-{date.today()}",
                            "llm": llm,
                            "session": session,
                        }
                    },
                )
                final_by_ticker[ticker] = result["final"]
                arbitrated_hashes.update(result["flagged"])
            except Exception as exc:
                log.warning("news triage graph failed for %s: %s", ticker, exc)
                final_by_ticker[ticker] = []

        # 6. Collect all url_hashes already in DB to avoid unique constraint violations
        all_raws = [r for raws_list in news_by_ticker.values() for r in raws_list]
        existing_hashes: set[str] = set()
        if all_raws:
            existing_hashes = {
                row.url_hash
                for row in session.query(NewsEvent.url_hash).filter(
                    NewsEvent.url_hash.in_([r.url_hash for r in all_raws])
                ).all()
            }

        # Persist NewsEvent rows (inside the same session as graph call_log flushes)
        for t_info in tickers_to_process:
            ticker = t_info["ticker"]
            raws = news_by_ticker.get(ticker, [])
            final_map = {item.url_hash: item for item in final_by_ticker.get(ticker, [])}
            for r in raws:
                if r.url_hash in existing_hashes:
                    continue
                f = final_map.get(r.url_hash)
                session.add(NewsEvent(
                    ticker=ticker,
                    published_at=r.published_at,
                    source=r.source,
                    headline=r.headline,
                    url=r.url,
                    url_hash=r.url_hash,
                    llm_material=f.is_material if f else None,
                    llm_sentiment=f.sentiment if f else None,
                    llm_summary=f.summary if f else None,
                    llm_model=SONNET if r.url_hash in arbitrated_hashes else HAIKU,
                    llm_cost_usd=None,  # cost is tracked via llm_call_log
                    arbitrated=r.url_hash in arbitrated_hashes,
                ))

        # 7. Update MoverState for processed tickers
        now = datetime.now(UTC)
        for t_info in tickers_to_process:
            session.merge(MoverState(
                ticker=t_info["ticker"],
                last_triggered_threshold=t_info["threshold_hit"],
                last_triggered_at=now,
                last_pct_change=t_info["pct_change"],
            ))
        # session.commit() happens via session_scope() context manager

    # 8. Email
    today_str = date.today().strftime("%Y-%m-%d")
    html = render_template(
        "movers.html.j2",
        movers=tickers_to_process,
        final_by_ticker=final_by_ticker,
        today=today_str,
    )
    text = render_template(
        "movers.txt.j2",
        movers=tickers_to_process,
        final_by_ticker=final_by_ticker,
        today=today_str,
    )
    emailer.send(
        to=settings.email_to,
        subject=f"Movers — {today_str}",
        html=html,
        text=text,
    )
    log.info(
        "run_movers_email completed: %d tickers emailed to %s",
        len(tickers_to_process),
        settings.email_to,
    )
