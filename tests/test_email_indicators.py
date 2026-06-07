"""Render tests for the email indicator-display feature.

Covers two additions to the weekly suggestions + weekly review emails:
  1. A "Market Sentiment" box (VIX + CNN Fear & Greed) shown when context is present.
  2. The 200-day MA columns / ETF-trend section shown for ETF holdings only.

These are template-level tests: they render the real Jinja2 templates with minimal
fixtures and assert on the produced HTML/text, which catches both Jinja syntax errors
and the gating/sentiment logic.
"""

from __future__ import annotations

from datetime import date

from investor.jobs.weekly_review import EtfTrendRow, WeeklyReview
from investor.services.daily_report import AccountSnapshot
from investor.services.indicators import IndicatorRow
from investor.services.render import render_template
from investor.services.weekly_context import WeeklyMarketContext
from investor.services.weekly_review_metrics import OrderFunnel


def _ind(ticker: str, close: float, sma_50: float, sma_200: float,
         pct50: float, pct200: float, rsi: float = 55.0) -> IndicatorRow:
    return IndicatorRow(
        ticker=ticker,
        as_of=date(2026, 6, 5),
        close=close,
        sma_20=None,
        sma_50=sma_50,
        sma_200=sma_200,
        ema_21=None,
        rsi_14=rsi,
        macd=None,
        macd_signal=None,
        pct_from_sma_50=pct50,
        pct_from_sma_200=pct200,
    )


def _ctx(vix: float | None, fg: int | None, label: str | None) -> WeeklyMarketContext:
    return WeeklyMarketContext(
        week_of=date(2026, 6, 8),
        macro_summary="",
        sector_summary="",
        ticker_catchup={},
        forward_events=[],
        citations=[],
        vix=vix,
        fear_greed_score=fg,
        fear_greed_label=label,
    )


# Distinct numbers so each formatted value is unique in the output.
_SPY = _ind("SPY", 455.0, 448.0, 450.0, 1.56, 1.11)      # ETF
_AAPL = _ind("AAPL", 190.0, 185.0, 300.0, 2.70, -36.67)  # non-ETF
_INDICATORS = [_SPY, _AAPL]


def _render_sugg(template: str, *, market_context: WeeklyMarketContext | None) -> str:
    ctx: dict[str, object] = dict(
        week_of=date(2026, 6, 8),
        account=AccountSnapshot("alpaca", "paper", 1000.0, 5000.0),
        account_nickname="Alpaca Paper",
        account_broker="alpaca",
        indicators=_INDICATORS,
        nearby={},
        untracked=[],
        skipped=[],
        scoring_failures=[],
        market_context=market_context,
        etf_tickers={"SPY"},
    )
    if template.endswith(".html.j2"):
        ctx["suggestion_items"] = []
        ctx["base_url"] = "http://localhost"
    else:
        ctx["suggestions"] = []
    return render_template(template, **ctx)


def _review(*, etf_trend: list[EtfTrendRow] | None,
            market_context: WeeklyMarketContext | None) -> WeeklyReview:
    return WeeklyReview(
        week_of=date(2026, 6, 12),
        account=AccountSnapshot("alpaca", "paper", 1000.0, 5000.0),
        realized_pnl_usd=12.5,
        suggestion_audits=[],
        gap_rows=[],
        material_news={},
        auto_trade_mode="OFF",
        promotions_this_week=[],
        kill_switches_this_week=[],
        executions_this_week=0,
        preview_suggestions=[],
        moomoo_status="unavailable",
        market_context=market_context,
        order_funnel=OrderFunnel(date(2026, 6, 12), 0, 0, 0, 0, 0, 0, 0, 0, 0),
        order_flow=None,
        drift_rows=None,
        breakdown_rows=None,
        trend_rows=None,
        etf_trend=etf_trend,
    )


# --------------------------------------------------------------------------- #
# Weekly suggestions email
# --------------------------------------------------------------------------- #

def test_suggestions_html_sma200_shown_for_etf_only() -> None:
    html = _render_sugg("weekly_suggestions.html.j2", market_context=None)
    # SPY (ETF) shows its 200-day MA + %Δ200…
    assert "$450.00" in html          # SPY sma_200
    assert "+1.1%" in html            # SPY pct_from_sma_200
    # …AAPL (non-ETF) has both suppressed.
    assert "$300.00" not in html      # AAPL sma_200 would-be value
    assert "-36.7%" not in html       # AAPL pct_from_sma_200 would-be value
    # AAPL still renders its always-on SMA50 column (sanity).
    assert "$185.00" in html
    assert "shown for ETFs only" in html


def test_suggestions_txt_pct200_shown_for_etf_only() -> None:
    text = _render_sugg("weekly_suggestions.txt.j2", market_context=None)
    assert "+1.1%" in text            # SPY %Δ200
    assert "-36.7%" not in text       # AAPL %Δ200 suppressed
    assert "+2.7%" in text            # AAPL %Δ50 always shown (sanity)


def test_suggestions_html_market_sentiment_box() -> None:
    html = _render_sugg("weekly_suggestions.html.j2",
                        market_context=_ctx(18.3, 62, "Greed"))
    assert "Market Sentiment" in html
    assert "18.3" in html
    assert "Fear &amp; Greed" in html
    assert "Greed" in html          # F&G semantic descriptor
    assert "Normal" in html         # VIX 18.3 -> "Normal" band descriptor


def test_suggestions_sentiment_box_absent_without_context() -> None:
    html = _render_sugg("weekly_suggestions.html.j2", market_context=None)
    text = _render_sugg("weekly_suggestions.txt.j2", market_context=None)
    assert "Market Sentiment" not in html
    assert "Market sentiment:" not in text


def test_suggestions_txt_market_sentiment_line() -> None:
    text = _render_sugg("weekly_suggestions.txt.j2",
                        market_context=_ctx(18.3, 62, "Greed"))
    assert "Market sentiment:" in text
    assert "VIX 18.3" in text
    assert "Fear & Greed 62" in text
    assert "(Greed)" in text


# --------------------------------------------------------------------------- #
# Weekly review email
# --------------------------------------------------------------------------- #

def test_review_html_etf_trend_and_sentiment() -> None:
    review = _review(
        etf_trend=[EtfTrendRow("SPY", 455.0, 450.0, 1.1)],
        market_context=_ctx(18.3, 62, "Greed"),
    )
    html = render_template("weekly_review.html.j2", review=review)
    assert "ETF trend (vs 200-day MA)" in html
    assert "$450.00" in html          # sma_200
    assert "+1.1%" in html            # pct_from_sma_200
    assert "Market Sentiment" in html
    assert "18.3" in html
    assert "Greed" in html          # F&G semantic descriptor
    assert "Normal" in html         # VIX 18.3 -> "Normal" band descriptor


def test_review_txt_etf_trend_and_sentiment() -> None:
    review = _review(
        etf_trend=[EtfTrendRow("SPY", 455.0, 450.0, 1.1)],
        market_context=_ctx(18.3, 62, "Greed"),
    )
    text = render_template("weekly_review.txt.j2", review=review)
    assert "ETF TREND (vs 200-day MA)" in text
    assert "$450.00" in text
    assert "+1.1%" in text
    assert "Market sentiment:" in text
    assert "VIX 18.3" in text


def test_review_etf_trend_and_sentiment_absent_when_empty() -> None:
    review = _review(etf_trend=None, market_context=None)
    html = render_template("weekly_review.html.j2", review=review)
    text = render_template("weekly_review.txt.j2", review=review)
    assert "ETF trend (vs 200-day MA)" not in html
    assert "ETF TREND (vs 200-day MA)" not in text
    assert "Market Sentiment" not in html
    assert "Market sentiment:" not in text
