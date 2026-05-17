"""Tests for src/investor/graphs/suggestion_review.py."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from investor.db import override_engine_for_testing
from investor.graphs.suggestion_review import (
    CriticDecision,
    CriticDecisions,
    DraftRationales,
    ReviewContext,
    SuggestionReviewState,
    _apply_changes,
    critic_node,
    gather_context_node,
    reason_node,
    revise_node,
    route_after_critic,
    skip_revise_node,
)
from investor.models import Base
from investor.services.daily_report import AccountSnapshot
from investor.services.gap import GapRow
from investor.services.llm import SONNET, LLMResponse
from investor.services.llm_levels import ScoredLevel
from investor.services.suggest import OrderSuggestionRow, _next_friday_eod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        model=SONNET,
        prompt_hash="abc123def456",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.0003,
        latency_ms=300,
    )


def _make_account(cash: float = 5_000.0, equity: float = 50_000.0) -> AccountSnapshot:
    return AccountSnapshot(broker="alpaca", mode="paper", cash_usd=cash, equity_usd=equity)


def _make_gap(ticker: str = "AAPL", gap_pct: float = 5.0) -> GapRow:
    return GapRow(
        ticker=ticker,
        current_pct=35.0,
        target_pct=40.0,
        gap_pct=gap_pct,
        gap_usd=500.0,
        band_status="under",
    )


def _make_level(
    method: str = "sma_50",
    price: float = 150.0,
    level_type: str = "support",
    confidence: float = 0.75,
) -> ScoredLevel:
    return ScoredLevel(
        method=method,
        price=price,
        type=level_type,
        confidence=confidence,
        rationale="Test rationale",
    )


def _make_draft(
    ticker: str = "AAPL",
    limit_price: float = 150.0,
    qty: float = 3.0,
    anchor_method: str | None = "sma_50",
) -> OrderSuggestionRow:
    return OrderSuggestionRow(
        ticker=ticker,
        side="buy",
        qty=qty,
        limit_price=limit_price,
        reason="test",
        expires_at=_next_friday_eod(),
        anchor_method=anchor_method,
    )


def _make_ctx(
    scored_levels: dict[str, list[ScoredLevel]] | None = None,
    gap_rows: list[GapRow] | None = None,
) -> ReviewContext:
    return ReviewContext(
        gap_rows=gap_rows or [_make_gap()],
        scored_levels=scored_levels or {"AAPL": [_make_level()]},
        recent_news={},
        indicators={},
        account=_make_account(),
        untracked_positions=[],
    )


def _make_state(
    drafts: list[OrderSuggestionRow] | None = None,
    ctx: ReviewContext | None = None,
    critic_decisions: dict[int, CriticDecision] | None = None,
    rationales: dict[int, str] | None = None,
    finals: list[OrderSuggestionRow] | None = None,
) -> SuggestionReviewState:
    return SuggestionReviewState(
        week_of=date(2026, 5, 19),
        context=ctx or _make_ctx(),
        drafts=drafts or [_make_draft()],
        rationales=rationales or {},
        critic_decisions=critic_decisions or {},
        finals=finals or [],
        suggestion_ids=[],
        telemetry={},
        targets_id=None,
    )


def _make_mock_session() -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    session.flush = MagicMock()
    return session


@contextmanager
def _mock_session_factory(session: MagicMock) -> Generator[MagicMock, None, None]:
    yield session


# ---------------------------------------------------------------------------
# _apply_changes tests
# ---------------------------------------------------------------------------


class TestApplyChanges:
    def test_revise_node_applies_known_price(self) -> None:
        """Smoke row 6: _apply_changes applies a limit_price that matches a scored level."""
        known_price = 150.0
        level = _make_level(method="sma_50", price=known_price)
        ctx = _make_ctx(scored_levels={"AAPL": [level]})
        draft = _make_draft(ticker="AAPL", limit_price=200.0)

        result = _apply_changes(draft, {"limit_price": known_price}, ctx)

        assert result is not None
        assert abs(result.limit_price - known_price) <= 0.01

    def test_revise_node_rejects_invented_price(self) -> None:
        """Smoke row 6+15: _apply_changes returns None for a price not in scored_levels."""
        level = _make_level(method="sma_50", price=150.0)
        ctx = _make_ctx(scored_levels={"AAPL": [level]})
        draft = _make_draft(ticker="AAPL", limit_price=150.0)

        result = _apply_changes(draft, {"limit_price": 999.99}, ctx)

        assert result is None

    def test_revise_node_rejects_invented_anchor_method(self) -> None:
        """_apply_changes returns None when anchor_method is not in scored_levels."""
        level = _make_level(method="sma_50", price=150.0)
        ctx = _make_ctx(scored_levels={"AAPL": [level]})
        draft = _make_draft(ticker="AAPL")

        result = _apply_changes(draft, {"anchor_method": "nonexistent_method"}, ctx)

        assert result is None

    def test_revise_node_applies_known_anchor_method(self) -> None:
        """_apply_changes updates limit_price to matched level price for known anchor_method."""
        sma50_level = _make_level(method="sma_50", price=155.0)
        ctx = _make_ctx(scored_levels={"AAPL": [sma50_level]})
        draft = _make_draft(ticker="AAPL", limit_price=200.0, anchor_method=None)

        result = _apply_changes(draft, {"anchor_method": "sma_50"}, ctx)

        assert result is not None
        assert result.anchor_method == "sma_50"
        assert result.limit_price == 155.0

    def test_apply_changes_qty_below_1_returns_none(self) -> None:
        """_apply_changes returns None when qty change results in < 1."""
        ctx = _make_ctx()
        draft = _make_draft()

        result = _apply_changes(draft, {"qty": 0}, ctx)

        assert result is None

    def test_apply_changes_valid_qty(self) -> None:
        """_apply_changes applies a valid qty change."""
        ctx = _make_ctx()
        draft = _make_draft(qty=3.0)

        result = _apply_changes(draft, {"qty": 5}, ctx)

        assert result is not None
        assert result.qty == 5.0


# ---------------------------------------------------------------------------
# revise_node tests
# ---------------------------------------------------------------------------


class TestReviseNode:
    def test_revise_node_applies_known_price_via_state(self) -> None:
        """revise_node: verdict=revise with valid limit_price → finals has updated price."""
        known_price = 150.0
        level = _make_level(method="sma_50", price=known_price)
        ctx = _make_ctx(scored_levels={"AAPL": [level]})
        draft = _make_draft(ticker="AAPL", limit_price=200.0)
        decisions = {
            0: CriticDecision(
                verdict="revise",
                reason="Price too high",
                suggested_changes={"limit_price": known_price},
            )
        }
        state = _make_state(drafts=[draft], ctx=ctx, critic_decisions=decisions)

        result = revise_node(state)

        assert len(result["finals"]) == 1
        assert abs(result["finals"][0].limit_price - known_price) <= 0.01

    def test_revise_node_keeps_original_on_invented_price(self) -> None:
        """Smoke row 15: invented price → keep original draft unchanged."""
        level = _make_level(method="sma_50", price=150.0)
        ctx = _make_ctx(scored_levels={"AAPL": [level]})
        draft = _make_draft(ticker="AAPL", limit_price=150.0)
        decisions = {
            0: CriticDecision(
                verdict="revise",
                reason="Use different price",
                suggested_changes={"limit_price": 999.99},
            )
        }
        state = _make_state(drafts=[draft], ctx=ctx, critic_decisions=decisions)

        result = revise_node(state)

        # Original kept because invented price is invalid
        assert len(result["finals"]) == 1
        assert result["finals"][0].limit_price != 999.99
        assert result["finals"][0].limit_price == 150.0

    def test_revise_node_rejects_draft(self) -> None:
        """verdict=reject → draft absent from finals."""
        draft = _make_draft(ticker="AAPL")
        decisions = {
            0: CriticDecision(
                verdict="reject",
                reason="Not a good buy",
                suggested_changes=None,
            )
        }
        state = _make_state(drafts=[draft], critic_decisions=decisions)

        result = revise_node(state)

        assert len(result["finals"]) == 0

    def test_revise_node_applies_known_anchor_method_via_state(self) -> None:
        """verdict=revise with anchor_method in scored_levels → finals updated."""
        sma50_level = _make_level(method="sma_50", price=155.0)
        ctx = _make_ctx(scored_levels={"AAPL": [sma50_level]})
        draft = _make_draft(ticker="AAPL", limit_price=200.0, anchor_method=None)
        decisions = {
            0: CriticDecision(
                verdict="revise",
                reason="Use sma_50 anchor",
                suggested_changes={"anchor_method": "sma_50"},
            )
        }
        state = _make_state(drafts=[draft], ctx=ctx, critic_decisions=decisions)

        result = revise_node(state)

        assert len(result["finals"]) == 1
        assert result["finals"][0].anchor_method == "sma_50"
        assert result["finals"][0].limit_price == 155.0


# ---------------------------------------------------------------------------
# route_after_critic tests
# ---------------------------------------------------------------------------


class TestRouteAfterCritic:
    def test_route_all_approved_returns_skip_revise(self) -> None:
        """Smoke row 7: all verdicts 'approve' → 'skip_revise'."""
        decisions = {
            0: CriticDecision(verdict="approve", reason="Looks good", suggested_changes=None),
            1: CriticDecision(verdict="approve", reason="Fine too", suggested_changes=None),
        }
        state = _make_state(
            drafts=[_make_draft(), _make_draft(ticker="MSFT")],
            critic_decisions=decisions,
        )
        assert route_after_critic(state) == "skip_revise"

    def test_route_one_revise_returns_revise(self) -> None:
        """Smoke row 8: one verdict 'revise' → 'revise'."""
        decisions = {
            0: CriticDecision(verdict="approve", reason="Fine", suggested_changes=None),
            1: CriticDecision(
                verdict="revise", reason="Needs change", suggested_changes={"qty": 5}
            ),
        }
        state = _make_state(
            drafts=[_make_draft(), _make_draft(ticker="MSFT")],
            critic_decisions=decisions,
        )
        assert route_after_critic(state) == "revise"

    def test_route_one_reject_returns_revise(self) -> None:
        """One verdict 'reject' also returns 'revise' (the revise node handles both)."""
        decisions = {
            0: CriticDecision(verdict="reject", reason="Bad idea", suggested_changes=None),
        }
        state = _make_state(critic_decisions=decisions)
        assert route_after_critic(state) == "revise"


# ---------------------------------------------------------------------------
# skip_revise_node tests
# ---------------------------------------------------------------------------


class TestSkipReviseNode:
    def test_skip_revise_node_copies_drafts_to_finals(self) -> None:
        """skip_revise_node returns state with finals == drafts."""
        draft_a = _make_draft(ticker="AAPL")
        draft_b = _make_draft(ticker="MSFT", limit_price=300.0)
        state = _make_state(drafts=[draft_a, draft_b])

        result = skip_revise_node(state)

        assert result["finals"] == [draft_a, draft_b]


# ---------------------------------------------------------------------------
# reason_node tests (mock LLM)
# ---------------------------------------------------------------------------


class TestReasonNode:
    def _run_reason_node(
        self,
        parsed_obj: DraftRationales,
        drafts: list[OrderSuggestionRow] | None = None,
    ) -> dict[str, Any]:
        content = parsed_obj.model_dump_json()
        resp = _make_llm_response(content)

        mock_llm = MagicMock()
        mock_llm.call.return_value = (resp, parsed_obj)

        mock_session = _make_mock_session()

        @contextmanager
        def mock_factory() -> Generator[MagicMock, None, None]:
            yield mock_session

        state = _make_state(drafts=drafts)

        with patch("investor.graphs.suggestion_review.load_prompt", return_value="system prompt"):
            result = reason_node(state, mock_llm, mock_factory)

        return result

    def test_reason_node_keys_cover_drafts(self) -> None:
        """Smoke row 3: rationales keys match {0, 1} for two drafts."""
        from investor.graphs.suggestion_review import DraftRationale
        parsed_real = DraftRationales(items=[
            DraftRationale(draft_index=0, rationale="Test rationale."),
            DraftRationale(draft_index=1, rationale="Another."),
        ])
        drafts = [_make_draft(), _make_draft(ticker="MSFT")]
        result = self._run_reason_node(parsed_real, drafts=drafts)

        assert set(result["rationales"].keys()) == {0, 1}

    def test_reason_node_truncates_long_rationale(self) -> None:
        """reason_node truncates rationale to ≤ 600 chars."""
        from investor.graphs.suggestion_review import DraftRationale
        long_text = "X" * 700
        parsed_real = DraftRationales(items=[
            DraftRationale(draft_index=0, rationale=long_text),
        ])
        result = self._run_reason_node(parsed_real)

        assert len(result["rationales"][0]) <= 600


# ---------------------------------------------------------------------------
# critic_node tests (mock LLM)
# ---------------------------------------------------------------------------


class TestCriticNode:
    def _run_critic_node(
        self,
        parsed_obj: CriticDecisions,
        drafts: list[OrderSuggestionRow] | None = None,
    ) -> dict[str, Any]:
        content = parsed_obj.model_dump_json()
        resp = _make_llm_response(content)

        mock_llm = MagicMock()
        mock_llm.call.return_value = (resp, parsed_obj)

        mock_session = _make_mock_session()

        @contextmanager
        def mock_factory() -> Generator[MagicMock, None, None]:
            yield mock_session

        state = _make_state(drafts=drafts)

        with patch("investor.graphs.suggestion_review.load_prompt", return_value="system prompt"):
            result = critic_node(state, mock_llm, mock_factory)

        return result

    def test_critic_node_keys_cover_drafts(self) -> None:
        """Smoke row 4: critic_decisions keys match draft indices."""
        from investor.graphs.suggestion_review import CriticDecisionOut
        parsed_real = CriticDecisions(items=[
            CriticDecisionOut(
                draft_index=0, verdict="approve", reason="Fine", suggested_changes=None
            ),
            CriticDecisionOut(
                draft_index=1,
                verdict="revise",
                reason="Needs work",
                suggested_changes={"qty": 5},
            ),
        ])
        drafts = [_make_draft(), _make_draft(ticker="MSFT")]
        result = self._run_critic_node(parsed_real, drafts=drafts)

        assert set(result["critic_decisions"].keys()) == {0, 1}


# ---------------------------------------------------------------------------
# Session-leak guard (smoke row 14)
# ---------------------------------------------------------------------------


class TestGatherContextNodeSessionLeak:
    def test_gather_context_node_no_detached_instance_error(self) -> None:
        """After gather_context_node, ReviewContext fields must not raise DetachedInstanceError."""
        engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, future=True)
        Base.metadata.create_all(engine)
        override_engine_for_testing(engine)
        factory = sessionmaker(bind=engine, autoflush=True, autocommit=False)

        @contextmanager
        def test_session_factory() -> Generator[Session, None, None]:
            sess = factory()
            try:
                yield sess
                sess.commit()
            except Exception:
                sess.rollback()
                raise
            finally:
                sess.close()

        state = SuggestionReviewState(
            week_of=date(2026, 5, 19),
            context=None,  # type: ignore[typeddict-item]
            drafts=[],
            rationales={},
            critic_decisions={},
            finals=[],
            suggestion_ids=[],
            telemetry={},
            targets_id=None,
        )

        with (
            patch("investor.graphs.suggestion_review.compute_indicators", return_value=[]),
            patch("investor.graphs.suggestion_review.load_recent_material_news", return_value={}),
            patch("investor.graphs.suggestion_review.load_latest_scored_levels", return_value={}),
        ):
            result = gather_context_node(state, test_session_factory, [], "data/bars")

        ctx = result["context"]
        # Must not raise DetachedInstanceError — all data extracted while session was open
        assert isinstance(ctx.gap_rows, list)
        assert isinstance(ctx.scored_levels, dict)
        assert isinstance(ctx.recent_news, dict)
        assert isinstance(ctx.indicators, dict)
        assert isinstance(ctx.account, AccountSnapshot)
        assert isinstance(ctx.untracked_positions, list)
