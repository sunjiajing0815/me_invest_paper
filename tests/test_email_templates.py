"""Render-smoke tests for the shared-component email templates (daily + movers).

Weekly review/suggestions are covered in test_email_indicators.py. These add the two
remaining HTML emails and guard against the autoescaped-entity bug (an HTML entity used
inside a Jinja {{ }} expression renders as literal "&amp;mdash;").
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from investor.services.render import render_template


def _ns(**kw: object) -> SimpleNamespace:
    return SimpleNamespace(**kw)


def _gap(t, gp, gu, bs, tp):  # type: ignore[no-untyped-def]
    return _ns(ticker=t, gap_pct=gp, gap_usd=gu, band_status=bs, target_pct=tp, current_pct=tp + gp)


def _pos(t, q, ac, mv, w):  # type: ignore[no-untyped-def]
    return _ns(ticker=t, qty=q, avg_cost=ac, market_value=mv, currency="USD", weight_pct=w)


def _orders(placed=0, filled=0, notional=0.0, fills=None):  # type: ignore[no-untyped-def]
    return _ns(placed_count=placed, filled_count=filled,
               filled_notional_usd=notional, fills=fills or [])


def _fill(t, side, qty, price, when):  # type: ignore[no-untyped-def]
    return _ns(ticker=t, side=side, filled_qty=qty, filled_price=price, filled_at=when)


def _render_daily(committed: list | None = None, orders=None) -> str:  # type: ignore[no-untyped-def]
    account = _ns(equity_usd=125000, cash_usd=43000, currency="USD",
                  broker="alpaca", mode="paper")
    report = _ns(
        date=date(2026, 6, 5),
        account=account,
        drift_alerts=[_gap("TQQQ", -3.2, -4100, "over", 10)],
        untracked_positions=[_pos("NVDA", 5, 900, 4800, 3.8)],
        gap_rows=[_gap("VOO", 2.1, 2600, "under", 25), _gap("MSFT", 0.2, 250, "in_band", 12)],
        positions=[_pos("VOO", 60, 420, 28000, 22.4), _pos("MSFT", 30, 410, 12900, 10.3)],
        committed_orders=committed or [],
        orders_this_week=orders if orders is not None else _orders(),
    )
    tokens = {r.sid: f"tok{r.sid}" for r in (committed or []) if r.cancellable}
    return render_template("daily_report.html.j2", report=report,
                           account_nickname="Alpaca paper", account_broker="alpaca",
                           base_url="https://x", unaccept_tokens=tokens)


def _render_movers() -> str:
    movers = [
        _ns(ticker="MSFT", pct_change=6.2, today_close=430, last_week_close=405, threshold_hit=5),
        _ns(ticker="TQQQ", pct_change=-7.1, today_close=62, last_week_close=67, threshold_hit=5),
    ]
    fbt = {
        "MSFT": [_ns(is_material=True, summary="Signed a cloud AI deal.", sentiment="bullish")],
        "TQQQ": [_ns(is_material=True, summary="Nasdaq sold off.", sentiment="bearish")],
    }
    return render_template("movers.html.j2", today="2026-06-05", movers=movers, final_by_ticker=fbt)


def test_daily_report_renders() -> None:
    html = _render_daily()
    assert "Portfolio Report" in html
    assert "Orders This Week" in html
    assert "Levels at a Glance" not in html  # replaced by the orders recap
    assert "Gap Summary" in html


def test_daily_orders_this_week_summary_and_fills() -> None:
    from datetime import datetime
    orders = _orders(
        placed=3, filled=2, notional=3300.0,
        fills=[_fill("VOO", "buy", 5, 400.0, datetime(2026, 6, 5, 14, 30)),
               _fill("QQQ", "sell", 2, 650.0, datetime(2026, 6, 4, 10, 0))],
    )
    html = _render_daily(orders=orders)
    assert "Orders This Week" in html
    assert "3" in html and "$3,300" in html  # placed count + filled notional
    assert "VOO" in html and "$400.00" in html
    assert "Jun 5 14:30" in html             # fill timestamp formatting


def test_daily_committed_orders_section_renders_unaccept_link() -> None:
    from investor.services.daily_report import CommittedOrderRow
    html = _render_daily(committed=[
        CommittedOrderRow(sid=7, ticker="VOO", side="buy", qty=5, limit_price=400.0,
                          status_label="Working", filled_price=None, cancellable=True),
        CommittedOrderRow(sid=8, ticker="QQQ", side="buy", qty=2, limit_price=650.0,
                          status_label="Filled", filled_price=651.0, cancellable=False),
    ])
    assert "Committed" in html                              # the section heading
    assert "/suggestions/7/unaccept?token=tok7" in html     # cancellable -> link
    assert "/suggestions/8/unaccept" not in html            # filled -> no link


def test_movers_renders_with_sentiment_pills() -> None:
    html = _render_movers()
    assert "Movers" in html
    assert "MSFT" in html and "TQQQ" in html
    assert "BULLISH" in html and "BEARISH" in html


def test_no_autoescaped_entity_leak() -> None:
    """An HTML entity inside a {{ }} expression would render as literal '&amp;mdash;'."""
    for html in (_render_daily(), _render_movers()):
        assert "&amp;mdash;" not in html
        assert "&amp;ndash;" not in html
