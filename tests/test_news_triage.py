"""Tests for src/investor/graphs/news_triage.py and src/investor/graphs/_nodes.py."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from investor.graphs.news_triage import (
    NewsCriticReview,
    NewsTriageBatch,
    NewsTriageItem,
    NewsTriageState,
    arbitrate_node,
    build_news_triage_graph,
    classify_node,
    copy_to_final,
    critic_node,
    route_after_critic,
)
from investor.services.llm import HAIKU, SONNET, LLMResponse
from investor.services.news import NewsRaw

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm_response(content: str, model: str = HAIKU) -> LLMResponse:
    return LLMResponse(
        content=content,
        model=model,
        prompt_hash="abc123def456",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.0001,
        latency_ms=200,
    )


def _make_raw(
    url_hash: str = "hash001",
    headline: str = "Test Headline",
    snippet: str = "Test snippet",
    published_at: datetime | None = None,
) -> NewsRaw:
    return NewsRaw(
        ticker="AAPL",
        headline=headline,
        snippet=snippet,
        url=f"https://example.com/{url_hash}",
        url_hash=url_hash,
        published_at=published_at or datetime(2024, 1, 10, 12, 0, tzinfo=UTC),
        source="alpaca",
    )


def _make_state(
    ticker: str = "AAPL",
    raws: list[NewsRaw] | None = None,
    classifications: list[NewsTriageItem] | None = None,
    flagged: list[str] | None = None,
    final: list[NewsTriageItem] | None = None,
) -> NewsTriageState:
    return NewsTriageState(
        ticker=ticker,
        raws=raws or [],
        classifications=classifications or [],
        flagged=flagged or [],
        final=final or [],
        telemetry={},
    )


def _make_config(
    llm: LLMClient | None = None,
    session: MagicMock | None = None,
    thread_id: str = "test-thread",
) -> dict:
    return {
        "configurable": {
            "thread_id": thread_id,
            "llm": llm or MagicMock(),
            "session": session or MagicMock(),
        }
    }


# Import LLMClient type hint
from investor.services.llm import LLMClient  # noqa: E402


def _make_llm_client(content: str, model: str = HAIKU) -> LLMClient:
    """Return a mock LLMClient whose .call() returns (LLMResponse, None) for schema validation."""
    mock_llm = MagicMock(spec=LLMClient)
    resp = _make_llm_response(content, model)
    mock_llm.call.return_value = (resp, None)
    return mock_llm


def _make_llm_client_with_parsed(
    content: str,
    parsed: object,
    model: str = HAIKU,
) -> LLMClient:
    """Return a mock LLMClient whose .call() returns (LLMResponse, parsed_object)."""
    mock_llm = MagicMock(spec=LLMClient)
    resp = _make_llm_response(content, model)
    mock_llm.call.return_value = (resp, parsed)
    return mock_llm


def _make_mock_session() -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    session.flush = MagicMock()
    return session


# ---------------------------------------------------------------------------
# classify_node
# ---------------------------------------------------------------------------


class TestClassifyNode:
    def test_classify_node_happy_path(self) -> None:
        """Mock LLMClient.call() returning valid parsed NewsTriageBatch."""
        raw = _make_raw("hash001")
        state = _make_state(raws=[raw])
        session = _make_mock_session()

        parsed_batch = NewsTriageBatch(items=[
            NewsTriageItem(
                url_hash="hash001",
                is_material=True,
                sentiment="bullish",
                summary="Stock rallied strongly",
            )
        ])
        valid_json = parsed_batch.model_dump_json()
        llm = _make_llm_client_with_parsed(valid_json, parsed_batch)

        config = _make_config(llm=llm, session=session)
        result = classify_node(state, config)

        assert len(result["classifications"]) == 1
        item = result["classifications"][0]
        assert item.url_hash == "hash001"
        assert item.is_material is True
        assert item.sentiment == "bullish"

    def test_classify_node_schema_error(self) -> None:
        """Mock returns invalid JSON → classifications == [] and status is schema_error."""
        raw = _make_raw("hash001")
        state = _make_state(raws=[raw])
        session = _make_mock_session()

        # LLM returns invalid JSON, parsed=None
        invalid_json = "not valid json at all"
        llm = _make_llm_client(invalid_json)  # parsed=None

        config = _make_config(llm=llm, session=session)
        result = classify_node(state, config)

        assert result["classifications"] == []
        # Telemetry should record schema_error
        assert result["telemetry"].get("news_classify_status") == "schema_error"


# ---------------------------------------------------------------------------
# critic_node
# ---------------------------------------------------------------------------


class TestCriticNode:
    def test_critic_node_flags_item(self) -> None:
        """Mock critic returns flagged=["hash1"]; verify state["flagged"] == ["hash1"]."""
        raw = _make_raw("hash1")
        cls_item = NewsTriageItem(
            url_hash="hash1",
            is_material=True,
            sentiment="bullish",
            summary="Test",
        )
        state = _make_state(raws=[raw], classifications=[cls_item])
        session = _make_mock_session()

        parsed_review = NewsCriticReview(flagged=["hash1"])
        llm = _make_llm_client_with_parsed(
            parsed_review.model_dump_json(), parsed_review
        )

        config = _make_config(llm=llm, session=session)
        result = critic_node(state, config)

        assert result["flagged"] == ["hash1"]

    def test_critic_node_empty_flags(self) -> None:
        """Mock critic returns flagged=[]; verify state["flagged"] == []."""
        raw = _make_raw("hash1")
        cls_item = NewsTriageItem(
            url_hash="hash1",
            is_material=False,
            sentiment="neutral",
            summary="Nothing notable",
        )
        state = _make_state(raws=[raw], classifications=[cls_item])
        session = _make_mock_session()

        parsed_review = NewsCriticReview(flagged=[])
        llm = _make_llm_client_with_parsed(
            parsed_review.model_dump_json(), parsed_review
        )

        config = _make_config(llm=llm, session=session)
        result = critic_node(state, config)

        assert result["flagged"] == []


# ---------------------------------------------------------------------------
# arbitrate_node
# ---------------------------------------------------------------------------


class TestArbitrateNode:
    def test_arbitrate_node_replaces_classification(self) -> None:
        """Mock Sonnet returns revised classification; verify state['final'] has revised."""
        raw = _make_raw("hash1")
        cls_item = NewsTriageItem(
            url_hash="hash1",
            is_material=False,
            sentiment="neutral",
            summary="Original classification",
        )
        state = _make_state(raws=[raw], classifications=[cls_item], flagged=["hash1"])
        session = _make_mock_session()

        revised_item = NewsTriageItem(
            url_hash="hash1",
            is_material=True,
            sentiment="bearish",
            summary="Revised by arbitrate",
        )
        revised_batch = NewsTriageBatch(items=[revised_item])
        llm = _make_llm_client_with_parsed(
            revised_batch.model_dump_json(), revised_batch, model=SONNET
        )

        config = _make_config(llm=llm, session=session)
        result = arbitrate_node(state, config)

        assert len(result["final"]) == 1
        assert result["final"][0].url_hash == "hash1"
        assert result["final"][0].is_material is True
        assert result["final"][0].sentiment == "bearish"
        assert result["final"][0].summary == "Revised by arbitrate"


# ---------------------------------------------------------------------------
# route_after_critic
# ---------------------------------------------------------------------------


class TestRouteAfterCritic:
    def test_route_after_critic_no_flags(self) -> None:
        """flagged=[] → returns 'no_arbitrate'."""
        state = _make_state(flagged=[])
        assert route_after_critic(state) == "no_arbitrate"

    def test_route_after_critic_with_flags(self) -> None:
        """flagged=["x"] → returns 'arbitrate'."""
        state = _make_state(flagged=["x"])
        assert route_after_critic(state) == "arbitrate"


# ---------------------------------------------------------------------------
# copy_to_final
# ---------------------------------------------------------------------------


class TestCopyToFinal:
    def test_copy_to_final(self) -> None:
        """state['final'] equals state['classifications'] after copy_to_final."""
        cls_items = [
            NewsTriageItem(url_hash="h1", is_material=True, sentiment="bullish", summary="A"),
            NewsTriageItem(url_hash="h2", is_material=False, sentiment="neutral", summary="B"),
        ]
        state = _make_state(classifications=cls_items)
        result = copy_to_final(state)

        assert result["final"] == cls_items
        # Verify it's a copy (not the same object reference isn't strictly required,
        # but they should be equal)
        assert result["final"] is not state["classifications"]


# ---------------------------------------------------------------------------
# Graph integration tests
# ---------------------------------------------------------------------------


def _make_stateful_llm(responses: list[tuple[str, object]]) -> LLMClient:
    """Return a mock LLMClient whose .call() returns successive (LLMResponse, parsed) tuples."""
    mock_llm = MagicMock(spec=LLMClient)
    side_effects = []
    for content, parsed in responses:
        resp = _make_llm_response(content)
        side_effects.append((resp, parsed))
    mock_llm.call.side_effect = side_effects
    return mock_llm


class TestGraphIntegration:
    def _build_graph(self) -> object:
        return build_news_triage_graph()

    def test_graph_integration_no_arbitrate(self) -> None:
        """Full graph with mocked LLM; critic returns no flags; final == classifications."""
        raw = _make_raw("hash001", headline="Apple Rises 5%")
        session = _make_mock_session()

        cls_item = NewsTriageItem(
            url_hash="hash001",
            is_material=True,
            sentiment="bullish",
            summary="Apple up strongly",
        )
        classify_batch = NewsTriageBatch(items=[cls_item])
        critic_review = NewsCriticReview(flagged=[])

        llm = _make_stateful_llm([
            (classify_batch.model_dump_json(), classify_batch),
            (critic_review.model_dump_json(), critic_review),
        ])

        graph = self._build_graph()

        initial_state = NewsTriageState(
            ticker="AAPL",
            raws=[raw],
            classifications=[],
            flagged=[],
            final=[],
            telemetry={},
        )
        result = graph.invoke(
            initial_state,
            config={
                "configurable": {
                    "thread_id": "test-no-arb",
                    "llm": llm,
                    "session": session,
                }
            },
        )

        assert len(result["final"]) == 1
        assert result["final"][0].url_hash == "hash001"
        assert result["final"][0].is_material is True
        assert result["flagged"] == []

    def test_graph_integration_with_arbitrate(self) -> None:
        """Critic flags one item; arbitrate runs; final has Sonnet's revised classification."""
        raw = _make_raw("hash001", headline="Company Misses Earnings")
        session = _make_mock_session()

        cls_item = NewsTriageItem(
            url_hash="hash001",
            is_material=False,
            sentiment="neutral",
            summary="Neutral classification",
        )
        classify_batch = NewsTriageBatch(items=[cls_item])
        critic_review = NewsCriticReview(flagged=["hash001"])

        revised_item = NewsTriageItem(
            url_hash="hash001",
            is_material=True,
            sentiment="bearish",
            summary="Revised: material bearish event",
        )
        arbitrate_batch = NewsTriageBatch(items=[revised_item])

        llm = _make_stateful_llm([
            (classify_batch.model_dump_json(), classify_batch),
            (critic_review.model_dump_json(), critic_review),
            (arbitrate_batch.model_dump_json(), arbitrate_batch),
        ])

        graph = self._build_graph()

        initial_state = NewsTriageState(
            ticker="AAPL",
            raws=[raw],
            classifications=[],
            flagged=[],
            final=[],
            telemetry={},
        )
        result = graph.invoke(
            initial_state,
            config={
                "configurable": {
                    "thread_id": "test-with-arb",
                    "llm": llm,
                    "session": session,
                }
            },
        )

        assert len(result["final"]) == 1
        final_item = result["final"][0]
        assert final_item.url_hash == "hash001"
        assert final_item.is_material is True
        assert final_item.sentiment == "bearish"
        assert "Revised" in final_item.summary


# ---------------------------------------------------------------------------
# Regression tests
# ---------------------------------------------------------------------------


class TestFenceRegression:
    def test_fence_regression(self) -> None:
        """Inject LLM response wrapped in ```json ... ``` ; node succeeds (status='ok')."""
        raw = _make_raw("hash001")
        state = _make_state(raws=[raw])
        session = _make_mock_session()

        cls_item = NewsTriageItem(
            url_hash="hash001",
            is_material=True,
            sentiment="bullish",
            summary="Fenced response",
        )
        batch = NewsTriageBatch(items=[cls_item])
        # Simulate fenced JSON response
        fenced_content = f"```json\n{batch.model_dump_json()}\n```"

        # LLM.call() actually strips fences internally and parses — simulate successful parse
        mock_llm = MagicMock(spec=LLMClient)
        resp = _make_llm_response(fenced_content)
        # The call returns (resp, parsed_batch) because LLMClient strips fences internally
        mock_llm.call.return_value = (resp, batch)

        config = _make_config(llm=mock_llm, session=session)
        result = classify_node(state, config)

        # Status should be 'ok' (not schema_error) since parsed is not None
        assert result["telemetry"].get("news_classify_status") == "ok"
        assert len(result["classifications"]) == 1

    def test_llm_call_log_error_reflects_parse_failure(self) -> None:
        """Invalid JSON after fence-stripping → error in log contains Pydantic message."""
        raw = _make_raw("hash001")
        state = _make_state(raws=[raw])
        session = _make_mock_session()

        # LLM returns invalid JSON (after fence stripping, still invalid), parsed=None
        invalid_content = '{"wrong_field": "totally wrong schema"}'
        mock_llm = MagicMock(spec=LLMClient)
        resp = _make_llm_response(invalid_content)
        mock_llm.call.return_value = (resp, None)  # parsed=None → schema_error path

        captured_error: list[str | None] = []

        with patch(
            "investor.graphs._nodes.persist_llm_call_log"
        ) as mock_persist:
            def capture_persist(session, resp, *, purpose, status, error=None):
                captured_error.append(error)

            mock_persist.side_effect = capture_persist
            config = _make_config(llm=mock_llm, session=session)
            result = classify_node(state, config)

        # Status should be schema_error
        assert result["telemetry"].get("news_classify_status") == "schema_error"
        # The captured error should contain a Pydantic validation message
        assert len(captured_error) == 1
        assert captured_error[0] is not None
        # Should contain Pydantic validation error text (not raw content)
        err = captured_error[0].lower()
        assert "validation error" in err or "field" in err
