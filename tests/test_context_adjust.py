"""Tests for context_adjust_node in suggestion_review.py."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

from investor.graphs.suggestion_review import (
    DraftSizeAdjustment,
    DraftSizeAdjustments,
    ReviewContext,
    SuggestionReviewState,
    context_adjust_node,
)
from investor.services.daily_report import AccountSnapshot
from investor.services.llm import SONNET, LLMResponse
from investor.services.llm_levels import ScoredLevel
from investor.services.suggest import OrderSuggestionRow, _next_friday_eod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(
    earnings_size_factor: float = 0.5,
    earnings_reanchor: bool = True,
    earnings_lookahead_days: int = 7,
    context_size_min: float = 0.25,
    context_size_max: float = 1.5,
    context_adjust_prompt_version: str = "v1",
) -> Any:
    s = MagicMock()
    s.earnings_size_factor = earnings_size_factor
    s.earnings_reanchor = earnings_reanchor
    s.earnings_lookahead_days = earnings_lookahead_days
    s.context_size_min = context_size_min
    s.context_size_max = context_size_max
    s.context_adjust_prompt_version = context_adjust_prompt_version
    return s


def _make_llm(adjustments: list[dict] | None = None) -> Any:
    """Mock LLM returning DraftSizeAdjustments JSON."""
    items = adjustments or []
    content = f'{{"items": {json.dumps(items)}}}'
    resp = LLMResponse(
        content=content,
        model=SONNET,
        prompt_hash="abc",
        input_tokens=10,
        output_tokens=20,
        cost_usd=0.0001,
        latency_ms=100,
    )
    mock = MagicMock()
    mock.call.return_value = (
        resp,
        DraftSizeAdjustments(items=[DraftSizeAdjustment(**it) for it in items]),
    )
    return mock


def _make_draft(
    ticker: str = "AAPL",
    side: str = "buy",
    qty: float = 10.0,
    limit_price: float = 150.0,
    anchor_method: str = "sma_50",
) -> OrderSuggestionRow:
    return OrderSuggestionRow(
        ticker=ticker,
        side=side,
        qty=qty,
        limit_price=limit_price,
        reason="test",
        expires_at=_next_friday_eod(),
        anchor_method=anchor_method,
    )


def _make_level(
    method: str, price: float, level_type: str = "support", confidence: float = 0.75
) -> ScoredLevel:
    return ScoredLevel(
        method=method, price=price, type=level_type, confidence=confidence, rationale="r"
    )


def _make_ctx(
    earnings_by_ticker: dict[str, date] | None = None,
    scored_levels: dict[str, list[ScoredLevel]] | None = None,
    market_context: Any = None,
) -> ReviewContext:
    return ReviewContext(
        gap_rows=[],
        scored_levels=scored_levels or {},
        recent_news={},
        indicators={},
        account=AccountSnapshot(broker="alpaca", mode="paper", cash_usd=5000.0, equity_usd=50000.0),
        untracked_positions=[],
        earnings_by_ticker=earnings_by_ticker or {},
        market_context=market_context,
    )


@contextmanager
def _mock_session_factory(session: Any):  # type: ignore[return]
    yield session


def _make_state(
    drafts: list[OrderSuggestionRow],
    ctx: ReviewContext,
    rationales: dict[int, str] | None = None,
) -> SuggestionReviewState:
    return SuggestionReviewState(
        week_of=date(2026, 5, 26),
        context=ctx,
        drafts=drafts,
        rationales=rationales or {},
        critic_decisions={},
        finals=[],
        suggestion_ids=[],
        telemetry={},
        targets_id=None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_earnings_gate_shrinks_qty() -> None:
    """Earnings gate halves qty when ticker has earnings in the window."""
    draft = _make_draft(ticker="AAPL", qty=10.0, limit_price=150.0)
    ctx = _make_ctx(
        earnings_by_ticker={"AAPL": date(2026, 6, 2)},
        scored_levels={"AAPL": [_make_level("sma_50", 150.0)]},
    )
    state = _make_state([draft], ctx)
    settings = _make_settings(earnings_size_factor=0.5, earnings_reanchor=False)
    llm = MagicMock()  # no market_context so llm never called
    mock_session = MagicMock()
    session_factory = lambda: _mock_session_factory(mock_session)  # noqa: E731

    result = context_adjust_node(state, llm, session_factory, settings)

    assert result["drafts"][0].qty == 5.0
    assert result["drafts"][0].base_qty == 10.0
    assert result["drafts"][0].size_factor == 0.5


def test_earnings_gate_reanchors_to_deeper_level() -> None:
    """When earnings_reanchor=True, limit_price moves to the deepest support below current."""
    draft = _make_draft(ticker="AAPL", qty=10.0, limit_price=150.0, anchor_method="sma_50")
    ctx = _make_ctx(
        earnings_by_ticker={"AAPL": date(2026, 6, 2)},
        scored_levels={
            "AAPL": [
                _make_level("sma_50", 150.0, "support"),
                _make_level("sma_200", 130.0, "support"),
            ]
        },
    )
    state = _make_state([draft], ctx)
    settings = _make_settings(earnings_reanchor=True)
    llm = MagicMock()
    mock_session = MagicMock()
    session_factory = lambda: _mock_session_factory(mock_session)  # noqa: E731

    result = context_adjust_node(state, llm, session_factory, settings)

    assert result["drafts"][0].limit_price == 130.0
    assert result["drafts"][0].anchor_method == "sma_200"


def test_earnings_gate_no_deeper_anchor_keeps_original() -> None:
    """When reanchor=True but no deeper support exists, original price is kept."""
    draft = _make_draft(ticker="AAPL", qty=10.0, limit_price=150.0, anchor_method="sma_50")
    ctx = _make_ctx(
        earnings_by_ticker={"AAPL": date(2026, 6, 2)},
        scored_levels={"AAPL": [_make_level("sma_50", 150.0, "support")]},
    )
    state = _make_state([draft], ctx)
    settings = _make_settings(earnings_reanchor=True)
    llm = MagicMock()
    mock_session = MagicMock()
    session_factory = lambda: _mock_session_factory(mock_session)  # noqa: E731

    result = context_adjust_node(state, llm, session_factory, settings)

    assert result["drafts"][0].limit_price == 150.0
    assert result["drafts"][0].anchor_method == "sma_50"


def test_narrative_multiplier_clamped_high() -> None:
    """Sonnet returning size_multiplier > context_size_max is clamped to max."""
    draft = _make_draft(ticker="AAPL", qty=10.0, limit_price=150.0)
    mock_market_ctx = MagicMock()
    mock_market_ctx.macro_summary = "bullish macro"
    mock_market_ctx.sector_summary = "tech rally"
    ctx = _make_ctx(
        market_context=mock_market_ctx,
    )
    state = _make_state([draft], ctx)
    settings = _make_settings(earnings_reanchor=False, context_size_max=1.5, context_size_min=0.25)

    adjustments = [
        {"draft_index": 0, "size_multiplier": 9.9, "prefer_anchor": None, "note": "bullish"}
    ]
    llm = _make_llm(adjustments)
    mock_session = MagicMock()
    session_factory = lambda: _mock_session_factory(mock_session)  # noqa: E731

    with patch("investor.graphs.suggestion_review.load_prompt", return_value="system prompt"):
        result = context_adjust_node(state, llm, session_factory, settings)

    assert result["drafts"][0].size_factor == 1.5
    assert result["drafts"][0].qty == 15.0  # floor(10 * 1.5)


def test_narrative_multiplier_clamped_low() -> None:
    """Sonnet returning size_multiplier < context_size_min is clamped to min."""
    draft = _make_draft(ticker="AAPL", qty=10.0, limit_price=150.0)
    mock_market_ctx = MagicMock()
    mock_market_ctx.macro_summary = "bearish macro"
    mock_market_ctx.sector_summary = "tech selloff"
    ctx = _make_ctx(
        market_context=mock_market_ctx,
    )
    state = _make_state([draft], ctx)
    settings = _make_settings(earnings_reanchor=False, context_size_max=1.5, context_size_min=0.25)

    adjustments = [
        {"draft_index": 0, "size_multiplier": 0.01, "prefer_anchor": None, "note": "bearish"}
    ]
    llm = _make_llm(adjustments)
    mock_session = MagicMock()
    session_factory = lambda: _mock_session_factory(mock_session)  # noqa: E731

    with patch("investor.graphs.suggestion_review.load_prompt", return_value="system prompt"):
        result = context_adjust_node(state, llm, session_factory, settings)

    assert result["drafts"][0].size_factor == 0.25
    assert result["drafts"][0].qty == 2.0  # floor(10 * 0.25)


def test_sub_1_share_draft_dropped() -> None:
    """Draft with qty=1 that gets halved (floor → 0) is dropped from the list."""
    draft = _make_draft(ticker="AAPL", qty=1.0, limit_price=150.0)
    ctx = _make_ctx(
        earnings_by_ticker={"AAPL": date(2026, 6, 2)},
    )
    state = _make_state([draft], ctx)
    settings = _make_settings(earnings_size_factor=0.5, earnings_reanchor=False)
    llm = MagicMock()
    mock_session = MagicMock()
    session_factory = lambda: _mock_session_factory(mock_session)  # noqa: E731

    result = context_adjust_node(state, llm, session_factory, settings)

    assert result["drafts"] == []


def test_rationales_rekeyed_after_drop() -> None:
    """After a draft is dropped, surviving drafts are re-keyed starting at 0."""
    draft_aapl = _make_draft(ticker="AAPL", qty=1.0, limit_price=150.0)
    draft_voo = _make_draft(ticker="VOO", qty=10.0, limit_price=400.0, anchor_method="sma_50")
    ctx = _make_ctx(
        earnings_by_ticker={"AAPL": date(2026, 6, 2)},
    )
    rationales = {0: "AAPL rationale", 1: "VOO rationale"}
    state = _make_state([draft_aapl, draft_voo], ctx, rationales=rationales)
    settings = _make_settings(earnings_size_factor=0.5, earnings_reanchor=False)
    llm = MagicMock()
    mock_session = MagicMock()
    session_factory = lambda: _mock_session_factory(mock_session)  # noqa: E731

    result = context_adjust_node(state, llm, session_factory, settings)

    assert len(result["drafts"]) == 1
    assert result["drafts"][0].ticker == "VOO"
    assert result["rationales"] == {0: "VOO rationale"}


def test_price_invariant() -> None:
    """prefer_anchor from Sonnet is ignored when the level is the wrong type for the side."""
    draft = _make_draft(ticker="AAPL", qty=10.0, limit_price=150.0, anchor_method="sma_50")
    mock_market_ctx = MagicMock()
    mock_market_ctx.macro_summary = "neutral"
    mock_market_ctx.sector_summary = "neutral"
    ctx = _make_ctx(
        market_context=mock_market_ctx,
        scored_levels={
            "AAPL": [
                _make_level("sma_50", 150.0, "support"),
                _make_level("resistance_swing", 170.0, "resistance"),
            ]
        },
    )
    state = _make_state([draft], ctx)
    settings = _make_settings(earnings_reanchor=False, context_size_max=1.5, context_size_min=0.25)

    # Sonnet returns prefer_anchor="resistance_swing" which is type "resistance" — wrong for a buy
    adjustments = [
        {
            "draft_index": 0,
            "size_multiplier": 1.0,
            "prefer_anchor": "resistance_swing",
            "note": "test",
        }
    ]
    llm = _make_llm(adjustments)
    mock_session = MagicMock()
    session_factory = lambda: _mock_session_factory(mock_session)  # noqa: E731

    with patch("investor.graphs.suggestion_review.load_prompt", return_value="system prompt"):
        result = context_adjust_node(state, llm, session_factory, settings)

    assert result["drafts"][0].limit_price == 150.0
    assert result["drafts"][0].anchor_method == "sma_50"


def test_reanchor_buy_price_floored() -> None:
    """Earnings reanchor floors the limit_price for a buy draft."""
    import math

    lv_price = 150.123456
    expected = math.floor(lv_price * 100) / 100  # 150.12

    draft = _make_draft(
        ticker="AAPL", side="buy", qty=10.0, limit_price=160.0, anchor_method="sma_50"
    )
    ctx = _make_ctx(
        earnings_by_ticker={"AAPL": date(2026, 6, 2)},
        scored_levels={
            "AAPL": [
                _make_level("sma_50", 160.0, "support"),
                _make_level("sma_200", lv_price, "support"),
            ]
        },
    )
    state = _make_state([draft], ctx)
    settings = _make_settings(earnings_reanchor=True)
    llm = MagicMock()
    mock_session = MagicMock()
    session_factory = lambda: _mock_session_factory(mock_session)  # noqa: E731

    result = context_adjust_node(state, llm, session_factory, settings)

    assert result["drafts"][0].limit_price == expected
    assert result["drafts"][0].anchor_method == "sma_200"


def test_reanchor_sell_price_ceiled() -> None:
    """Earnings reanchor ceils the limit_price for a sell draft."""
    import math

    lv_price = 150.123456
    expected = math.ceil(lv_price * 100) / 100  # 150.13

    draft = _make_draft(
        ticker="AAPL", side="sell", qty=10.0, limit_price=140.0, anchor_method="sma_50"
    )
    ctx = _make_ctx(
        earnings_by_ticker={"AAPL": date(2026, 6, 2)},
        scored_levels={
            "AAPL": [
                _make_level("sma_50", 140.0, "resistance"),
                _make_level("sma_200", lv_price, "resistance"),
            ]
        },
    )
    state = _make_state([draft], ctx)
    settings = _make_settings(earnings_reanchor=True)
    llm = MagicMock()
    mock_session = MagicMock()
    session_factory = lambda: _mock_session_factory(mock_session)  # noqa: E731

    result = context_adjust_node(state, llm, session_factory, settings)

    assert result["drafts"][0].limit_price == expected
    assert result["drafts"][0].anchor_method == "sma_200"


def test_base_qty_always_set_when_neutral() -> None:
    """base_qty is set even when size_factor == 1.0 (neutral context)."""
    draft = _make_draft(ticker="AAPL", qty=10.0, limit_price=150.0)
    mock_market_ctx = MagicMock()
    mock_market_ctx.macro_summary = "neutral"
    mock_market_ctx.sector_summary = "neutral"
    mock_market_ctx.vix = 18.0
    mock_market_ctx.fear_greed_score = 50
    mock_market_ctx.fear_greed_label = "Neutral"
    ctx = _make_ctx(market_context=mock_market_ctx)
    state = _make_state([draft], ctx)
    settings = _make_settings(earnings_reanchor=False, context_size_max=1.5, context_size_min=0.25)

    # Sonnet returns size_multiplier=1.0 — no earnings, neutral narrative → size_factor == 1.0
    adjustments = [
        {"draft_index": 0, "size_multiplier": 1.0, "prefer_anchor": None, "note": "neutral"}
    ]
    llm = _make_llm(adjustments)
    mock_session = MagicMock()
    session_factory = lambda: _mock_session_factory(mock_session)  # noqa: E731

    with patch("investor.graphs.suggestion_review.load_prompt", return_value="system prompt"):
        result = context_adjust_node(state, llm, session_factory, settings)

    draft_out = result["drafts"][0]
    # qty is unchanged (neutral), but base_qty must NOT be None — node ran
    assert draft_out.qty == 10.0
    assert draft_out.base_qty == 10.0, "base_qty must be set even for neutral (size_factor=1.0)"
    assert draft_out.size_factor == 1.0


def test_context_adjust_includes_asset_classes_in_payload() -> None:
    """The JSON payload sent to the LLM contains an 'asset_classes' key with per-ticker values."""
    import dataclasses as _dc

    draft_voo = _make_draft(ticker="VOO", qty=10.0, limit_price=400.0, anchor_method="sma_50")
    draft_tqqq = _make_draft(ticker="TQQQ", qty=5.0, limit_price=50.0, anchor_method="sma_50")

    mock_market_ctx = MagicMock()
    mock_market_ctx.macro_summary = "neutral"
    mock_market_ctx.sector_summary = "neutral"
    mock_market_ctx.vix = 18.0
    mock_market_ctx.fear_greed_score = 50
    mock_market_ctx.fear_greed_label = "Neutral"

    ctx = _make_ctx(market_context=mock_market_ctx)

    # Inject asset classes into the ReviewContext via dataclasses.replace
    ctx_with_classes = _dc.replace(
        ctx,
        target_asset_classes={"VOO": "index_etf", "TQQQ": "leveraged_etf"},
    )

    state = _make_state([draft_voo, draft_tqqq], ctx_with_classes)
    settings = _make_settings(
        earnings_reanchor=False, context_size_max=1.5, context_size_min=0.25
    )

    adjustments = [
        {"draft_index": 0, "size_multiplier": 1.0, "prefer_anchor": None, "note": ""},
        {"draft_index": 1, "size_multiplier": 1.0, "prefer_anchor": None, "note": ""},
    ]
    llm = _make_llm(adjustments)

    mock_session = MagicMock()
    session_factory = lambda: _mock_session_factory(mock_session)  # noqa: E731

    with patch("investor.graphs.suggestion_review.load_prompt", return_value="system prompt"):
        context_adjust_node(state, llm, session_factory, settings)

    # llm.call is called by llm_node_call with keyword args: model=, system=, user=, ...
    assert llm.call.called, "LLM was never called — market_context branch did not fire"
    _, call_kwargs = llm.call.call_args
    user_json = call_kwargs["user"]
    payload = json.loads(user_json)

    assert "asset_classes" in payload, "payload missing 'asset_classes' key"
    assert payload["asset_classes"]["VOO"] == "index_etf"
    assert payload["asset_classes"]["TQQQ"] == "leveraged_etf"
