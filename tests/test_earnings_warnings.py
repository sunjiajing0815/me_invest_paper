"""Earnings warnings for the weekly suggestions email (build_earnings_warnings)."""
from __future__ import annotations

from datetime import date

from investor.services.earnings import EarningsWarning, build_earnings_warnings

_WEEK_OF = date(2026, 7, 27)   # a Monday
_TODAY = date(2026, 7, 26)     # the Sunday the email is sent


def _build(earnings, suggested=None, names=None):  # type: ignore[no-untyped-def]
    return build_earnings_warnings(
        earnings,
        week_of=_WEEK_OF,
        suggested_tickers=suggested or set(),
        names=names or {},
        today=_TODAY,
    )


def test_empty_when_no_earnings() -> None:
    assert _build({}) == []


def test_this_week_flagged() -> None:
    out = _build({"NVDA": date(2026, 7, 29)})  # Wed of week_of week
    assert len(out) == 1
    w = out[0]
    assert w.ticker == "NVDA" and w.this_week is True
    assert w.days_away == 3


def test_next_week_not_this_week() -> None:
    out = _build({"MSFT": date(2026, 8, 5)})  # Wed of the following week
    assert out[0].this_week is False


def test_suggested_tickers_sort_first() -> None:
    out = _build(
        {"MSFT": date(2026, 7, 28), "NVDA": date(2026, 8, 4)},
        suggested={"NVDA"},
    )
    # NVDA reports LATER but has a suggestion → sorts first
    assert [w.ticker for w in out] == ["NVDA", "MSFT"]
    assert out[0].has_suggestion is True
    assert out[1].has_suggestion is False


def test_same_group_sorts_by_date() -> None:
    out = _build({"A": date(2026, 8, 6), "B": date(2026, 7, 28)})
    assert [w.ticker for w in out] == ["B", "A"]  # both unsuggested → soonest first


def test_name_populated_from_map() -> None:
    out = _build({"NVDA": date(2026, 7, 29)}, names={"NVDA": "NVIDIA Corp"})
    assert out[0].name == "NVIDIA Corp"


def test_name_falls_back_to_ticker() -> None:
    out = _build({"NVDA": date(2026, 7, 29)})
    assert out[0].name == "NVDA"


def test_frozen() -> None:
    import pytest
    w = EarningsWarning(ticker="X", name="X", earnings_date=_TODAY, days_away=0,
                        this_week=True, has_suggestion=False)
    with pytest.raises((AttributeError, TypeError)):
        w.ticker = "Y"  # type: ignore[misc]


# ── email rendering ───────────────────────────────────────────────────────────

def _render(warnings) -> str:  # type: ignore[no-untyped-def]
    from investor.services.daily_report import AccountSnapshot
    from investor.services.render import render_template
    return render_template(
        "weekly_suggestions.html.j2",
        week_of=_WEEK_OF, account=AccountSnapshot("alpaca", "paper", 1000.0, 5000.0),
        account_nickname="Alpaca Paper", account_broker="alpaca",
        indicators=[], nearby={}, untracked=[], skipped=[], scoring_failures=[],
        market_context=None, etf_tickers=set(), suggestion_items=[], topup_items=[],
        base_url="http://localhost", earnings_warnings=warnings,
    )


def test_email_shows_earnings_box() -> None:
    w = EarningsWarning(ticker="NVDA", name="NVIDIA", earnings_date=date(2026, 7, 29),
                        days_away=3, this_week=True, has_suggestion=True)
    html = _render([w])
    assert "Earnings this week" in html
    assert "NVDA" in html and "Wed Jul 29" in html
    assert "&#9733;" in html  # star for has_suggestion

def test_email_no_box_when_empty() -> None:
    assert "Earnings this week" not in _render([])
