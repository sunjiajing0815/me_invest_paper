"""Top-up suggestions engine (plans/topup_suggestions_design.md).

Sentiment fraction table, eligibility matrix, band-ceiling sizing, cash budget.
"""
from __future__ import annotations

from datetime import date

import pytest

from investor.services.daily_report import AccountSnapshot
from investor.services.gap import GapRow
from investor.services.levels import NearbyLevels, SRLevelRow
from investor.services.llm_levels import ScoredLevel
from investor.services.suggest import (
    generate_topup_suggestions,
    topup_size_fraction,
)

# ── sentiment fraction table ──────────────────────────────────────────────────

@pytest.mark.parametrize("fg,expected", [
    (10, 1.0), (25, 1.0),          # extreme fear
    (26, 0.75), (38, 0.75), (45, 0.75),  # fear
    (46, 0.5), (55, 0.5),          # neutral
    (56, 0.25), (75, 0.25),        # greed
    (76, 0.25), (90, 0.25),        # extreme greed
])
def test_fraction_fear_greed_primary(fg: int, expected: float) -> None:
    assert topup_size_fraction(None, fg) == pytest.approx(expected)
    assert topup_size_fraction(99.0, fg) == pytest.approx(expected)  # F&G wins over VIX


@pytest.mark.parametrize("vix,expected", [
    (35.0, 1.0), (30.0, 1.0),      # panic
    (25.0, 0.75), (20.0, 0.75),    # elevated
    (15.0, 0.5),                   # calm
])
def test_fraction_vix_fallback(vix: float, expected: float) -> None:
    assert topup_size_fraction(vix, None) == pytest.approx(expected)


def test_fraction_defaults_neutral_when_no_data() -> None:
    assert topup_size_fraction(None, None) == pytest.approx(0.5)


# ── generator fixtures ────────────────────────────────────────────────────────

def _account(equity: float = 100_000.0, cash: float = 50_000.0) -> AccountSnapshot:
    return AccountSnapshot(broker="alpaca", mode="paper", cash_usd=cash, equity_usd=equity)


def _gap(ticker: str, current: float, target: float,
         band_status: str = "in_band", equity: float = 100_000.0) -> GapRow:
    return GapRow(
        ticker=ticker, current_pct=current, target_pct=target,
        gap_pct=target - current, gap_usd=(target - current) / 100 * equity,
        band_status=band_status,  # type: ignore[arg-type]
    )


def _levels(ticker: str, current_price: float, support: float) -> NearbyLevels:
    return NearbyLevels(
        ticker=ticker, current_price=current_price,
        supports=[SRLevelRow(ticker=ticker, type="support", price=support,
                             method="sma_50", as_of=date(2026, 7, 17))],
        resistances=[],
    )


def _run(gap: GapRow, levels: NearbyLevels, *, band_high: float,
         fraction: float = 0.5, cash: float = 50_000.0,
         regular: set[str] | None = None,
         scored: dict[str, list[ScoredLevel]] | None = None):  # type: ignore[no-untyped-def]
    return generate_topup_suggestions(
        gap_rows=[gap],
        nearby_levels={gap.ticker: levels},
        account=_account(cash=cash),
        band_high_by_ticker={gap.ticker: band_high},
        regular_buy_tickers=regular or set(),
        sentiment_fraction=fraction,
        sentiment_note="fear&greed=38",
        scored_levels=scored,
        cash_available=cash,
    )


# ── eligibility ───────────────────────────────────────────────────────────────

def test_under_target_in_band_gets_topup() -> None:
    # NEE-like: 4.2% of 5% target, band_high 8, price 71, equity 100k.
    # GAP to target = (5-4.2)% * 100k = $800 → base 11 @ 71.
    # Effective fraction = sentiment 0.5 × conf 0.5 (unscored fallback) = 0.25 → qty 2.
    # (Band headroom would be $3,800/53 shares — sizing must use the GAP, not the band.)
    out = _run(_gap("NEE", 4.2, 5.0), _levels("NEE", 73.0, 71.0), band_high=8.0)
    assert len(out) == 1
    s = out[0]
    assert s.kind == "topup" and s.side == "buy"
    assert s.limit_price == pytest.approx(71.0)
    assert s.base_qty == 11 and s.size_factor == pytest.approx(0.25)
    assert s.qty == 2
    assert s.qty * s.limit_price <= 800 + 71  # deploys ~the gap, never ~the band headroom
    assert "top-up sized ×0.25" in (s.context_note or "")
    assert "conf 0.50" in (s.context_note or "")


def test_at_or_over_target_not_eligible() -> None:
    assert _run(_gap("QQQ", 25.0, 25.0), _levels("QQQ", 650, 640), band_high=29) == []
    assert _run(_gap("QQQ", 26.0, 25.0), _levels("QQQ", 650, 640), band_high=29) == []


def test_ticker_with_regular_draft_excluded() -> None:
    out = _run(_gap("NEE", 4.2, 5.0), _levels("NEE", 73.0, 71.0), band_high=8.0,
               regular={"NEE"})
    assert out == []


def test_one_share_exceeding_band_skipped() -> None:
    # current 28.9% of band_high 29 → headroom 0.1% = $100; price $650 → 0 shares fit.
    out = _run(_gap("QQQ", 28.9, 25.0), _levels("QQQ", 660.0, 650.0), band_high=29.0)
    assert out == []


def test_one_share_floor_when_fraction_rounds_to_zero() -> None:
    # Under target (24.9 < 25) with tiny band headroom: (25.4-24.9)%*100k = $500 @ $450
    # → base 1 share; fraction 0.25 → floor(0.25)=0 → floored to 1.
    out = _run(_gap("QQQ", 24.9, 25.0), _levels("QQQ", 460.0, 450.0), band_high=25.4,
               fraction=0.25)
    assert len(out) == 1 and out[0].qty == 1 and out[0].base_qty == 1


def test_distance_guard_applies() -> None:
    # support 20% below current → rejected, no fallback closer → no topup.
    out = _run(_gap("NEE", 4.2, 5.0), _levels("NEE", 90.0, 71.0), band_high=8.0)
    assert out == []


def test_cash_budget_reduces_qty() -> None:
    # base 11 × (0.5 sentiment × 0.5 conf) = 2 @ $71 = $142, but only $171 cash:
    # $71 above the $100 floor → qty reduced to floor(71/71) = 1.
    out = _run(_gap("NEE", 4.2, 5.0), _levels("NEE", 73.0, 71.0), band_high=8.0,
               cash=171.0)
    assert len(out) == 1 and out[0].qty == 1


def test_cash_below_floor_skips() -> None:
    out = _run(_gap("NEE", 4.2, 5.0), _levels("NEE", 73.0, 71.0), band_high=8.0,
               cash=150.0)  # 150-100 floor = $50 < 1 share
    assert out == []


def test_scored_anchor_preferred_and_confidence_captured() -> None:
    scored = {"NEE": [ScoredLevel(method="ema_21", price=70.5, type="support",
                                  confidence=0.8, rationale="tested twice")]}
    out = _run(_gap("NEE", 4.2, 5.0), _levels("NEE", 73.0, 71.0), band_high=8.0,
               scored=scored)
    assert len(out) == 1
    assert out[0].anchor_method == "ema_21"
    assert out[0].confidence_at_creation == pytest.approx(0.8)
    # per-ticker scaling: 0.5 sentiment × 0.8 conf = 0.40 effective
    assert out[0].size_factor == pytest.approx(0.4)
    gap_shares = int(800 / 70.5)  # 11
    assert out[0].qty == int(gap_shares * 0.4)  # 4


# ── email template rendering ──────────────────────────────────────────────────

def _render_weekly_html(topup_items: list) -> str:  # type: ignore[type-arg]
    from investor.services.render import render_template
    return render_template(
        "weekly_suggestions.html.j2",
        week_of=date(2026, 7, 20),
        account=_account(),
        account_nickname="Alpaca Paper",
        account_broker="alpaca",
        indicators=[],
        nearby={},
        untracked=[],
        skipped=[],
        scoring_failures=[],
        market_context=None,
        etf_tickers=set(),
        suggestion_items=[],
        topup_items=topup_items,
        base_url="http://localhost",
    )


def _topup_item(ticker: str = "NEE", highlighted: bool = False) -> dict:  # type: ignore[type-arg]
    return {
        "sid": 99,
        "suggestion": {
            "ticker": ticker, "side": "buy", "qty": 12.0, "limit_price": 71.0,
            "reason": "top-up: 4.2% vs 5.0% target", "base_qty": 16.0,
            "size_factor": 0.75, "context_note": "top-up sized ×0.75 (fear&greed=38)",
            "kind": "topup", "is_highlighted": highlighted, "llm_rationale": None,
        },
        "rationale": "Near-target entry at a well-tested support.",
        "accept_token": "tokA", "reject_token": "tokR",
    }


def test_topup_section_renders_with_buttons() -> None:
    html = _render_weekly_html([_topup_item()])
    assert "Top-Up Opportunities" in html
    assert "/suggestions/99/accept?token=tokA" in html
    assert "/suggestions/99/reject?token=tokR" in html
    assert "STRONG ENTRY" not in html          # not highlighted
    assert "&amp;middot;" not in html          # autoescape-entity guard


def test_topup_highlight_pill_only_when_flagged() -> None:
    html = _render_weekly_html([_topup_item(highlighted=True)])
    assert "STRONG ENTRY" in html
    assert "#e8f6ec" in html                   # HL_BG applied to the row


def test_no_topup_section_when_empty() -> None:
    html = _render_weekly_html([])
    assert "Top-Up Opportunities" not in html


def test_gap_sizing_never_deploys_band_headroom() -> None:
    """Regression (07-20 email): AMZN-like 1.76% gap must NOT size from band headroom.
    current 3.2% of 5% target, band_high 8, price 234, equity 100k, fraction 0.75:
    gap $1,800 → base 7 → qty 5 (~$1,170) — NOT base 20/qty 15 (~$3,511) from headroom."""
    out = _run(_gap("AMZN", 3.24, 5.0), _levels("AMZN", 247.0, 234.0), band_high=8.0,
               fraction=0.75)
    assert len(out) == 1
    s = out[0]
    assert s.base_qty == 7          # floor(1760/234)
    assert s.qty == 2               # floor(7 × 0.75 sentiment × 0.5 unscored-conf)
    assert s.qty * s.limit_price < 1_800  # deploys within the gap


def test_one_share_floor_capped_by_band() -> None:
    """Tiny gap (<1 share) → 1-share floor, allowed only because 1 share fits under
    band_high (the eligibility cap). qty stays 1 regardless of fraction."""
    # gap (5-4.97)% * 100k = $30 @ $450 → gap_shares 0 → base 1; band headroom
    # (5.5-4.97)% * 100k = $530 → cap 1 → qty 1.
    out = _run(_gap("QQQ", 4.97, 5.0), _levels("QQQ", 460.0, 450.0), band_high=5.5,
               fraction=1.0)
    assert len(out) == 1 and out[0].qty == 1 and out[0].base_qty == 1


def test_high_confidence_scales_up_vs_low() -> None:
    """Per-ticker scaling: same gap + sentiment, higher anchor confidence → larger qty."""
    def run_with_conf(conf: float):  # type: ignore[no-untyped-def]
        scored = {"NEE": [ScoredLevel(method="ema_21", price=70.5, type="support",
                                      confidence=conf, rationale="t")]}
        return _run(_gap("NEE", 3.0, 5.0), _levels("NEE", 73.0, 71.0), band_high=8.0,
                    fraction=1.0, scored=scored)
    hi = run_with_conf(0.9)[0]
    lo = run_with_conf(0.5)[0]
    assert hi.qty > lo.qty
    assert hi.size_factor == pytest.approx(0.9)
    assert lo.size_factor == pytest.approx(0.5)
