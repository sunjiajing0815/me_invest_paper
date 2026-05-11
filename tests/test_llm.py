"""Tests for LLMClient cost guard, schema validation, and _calc_cost."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch
from src.investor.services.llm import LLMClient, LLMResponse, _calc_cost, HAIKU, SONNET


class TestCalcCost:
    def test_haiku_cost(self):
        # 1M input tokens @ $1.00/M + 1M output tokens @ $5.00/M = $6.00
        cost = _calc_cost(HAIKU, 1_000_000, 1_000_000)
        assert abs(cost - 6.0) < 0.001

    def test_sonnet_cost(self):
        # 1M input tokens @ $3.00/M + 1M output tokens @ $15.00/M = $18.00
        cost = _calc_cost(SONNET, 1_000_000, 1_000_000)
        assert abs(cost - 18.0) < 0.001

    def test_unknown_model_raises_key_error(self):
        with pytest.raises(KeyError):
            _calc_cost("claude-unknown", 100, 100)

    def test_zero_tokens(self):
        assert _calc_cost(HAIKU, 0, 0) == 0.0

    def test_output_only_tokens(self):
        # Only output tokens, no input
        cost = _calc_cost(HAIKU, 0, 1_000_000)
        assert abs(cost - 5.0) < 0.001

    def test_input_only_tokens(self):
        # Only input tokens, no output
        cost = _calc_cost(SONNET, 1_000_000, 0)
        assert abs(cost - 3.0) < 0.001


class TestLLMClientCostGuard:
    def _make_client(self, cap: float = 5.0) -> LLMClient:
        return LLMClient(api_key="test-key", daily_cost_cap_usd=cap)

    def _make_mock_message(self, text: str = '{"result": "ok"}',
                           input_tokens: int = 100, output_tokens: int = 100):
        """Return a mock anthropic Message with a single TextBlock."""
        from anthropic.types import TextBlock
        mock_msg = MagicMock()
        mock_block = MagicMock(spec=TextBlock)
        mock_block.text = text
        mock_msg.content = [mock_block]
        mock_msg.usage.input_tokens = input_tokens
        mock_msg.usage.output_tokens = output_tokens
        return mock_msg

    def test_raises_when_cap_reached(self):
        client = self._make_client(cap=0.0)
        with pytest.raises(RuntimeError, match="daily LLM cost cap"):
            client.call(model=HAIKU, system="s", user="u")

    def test_raises_when_cap_already_spent(self):
        client = self._make_client(cap=1.0)
        client._spent_today = 1.0  # Already at cap
        with pytest.raises(RuntimeError, match="daily LLM cost cap"):
            client.call(model=HAIKU, system="s", user="u")

    def test_accumulates_spent(self):
        """Two calls accumulate _spent_today correctly."""
        client = self._make_client(cap=1.0)
        mock_msg = self._make_mock_message(input_tokens=100, output_tokens=100)

        with patch.object(client._client.messages, "create", return_value=mock_msg):
            client.call(model=HAIKU, system="s", user="u")
            first_spent = client._spent_today
            assert first_spent > 0.0
            client.call(model=HAIKU, system="s", user="u")
            assert client._spent_today > first_spent

    def test_accumulates_spent_correct_value(self):
        """_spent_today reflects the actual token cost after a call."""
        client = self._make_client(cap=1.0)
        # 100 input @ $1/M + 100 output @ $5/M = 0.0001 + 0.0005 = 0.0006
        mock_msg = self._make_mock_message(input_tokens=100, output_tokens=100)

        with patch.object(client._client.messages, "create", return_value=mock_msg):
            client.call(model=HAIKU, system="s", user="u")
            expected = _calc_cost(HAIKU, 100, 100)
            assert abs(client._spent_today - expected) < 1e-9

    def test_schema_validation_failure_returns_none(self):
        """When LLM returns invalid JSON, parsed is None but LLMResponse is returned."""
        from pydantic import BaseModel

        class MySchema(BaseModel):
            value: int

        client = self._make_client(cap=1.0)
        mock_msg = self._make_mock_message(text="not valid json", input_tokens=50, output_tokens=50)

        with patch.object(client._client.messages, "create", return_value=mock_msg):
            resp, parsed = client.call(model=HAIKU, system="s", user="u", response_schema=MySchema)

        assert isinstance(resp, LLMResponse)
        assert parsed is None

    def test_schema_validation_success(self):
        """When LLM returns valid JSON matching the schema, parsed is populated."""
        from pydantic import BaseModel

        class MySchema(BaseModel):
            value: int

        client = self._make_client(cap=1.0)
        mock_msg = self._make_mock_message(text='{"value": 42}', input_tokens=50, output_tokens=50)

        with patch.object(client._client.messages, "create", return_value=mock_msg):
            resp, parsed = client.call(model=HAIKU, system="s", user="u", response_schema=MySchema)

        assert isinstance(resp, LLMResponse)
        assert parsed is not None
        assert parsed.value == 42

    def test_schema_none_returns_content_as_string(self):
        """When no response_schema provided, parsed is None and resp.content has the text."""
        client = self._make_client(cap=1.0)
        mock_msg = self._make_mock_message(text="just plain text")

        with patch.object(client._client.messages, "create", return_value=mock_msg):
            resp, parsed = client.call(model=HAIKU, system="s", user="u")

        assert resp.content == "just plain text"
        assert parsed is None

    def test_day_rollover_resets_spent(self):
        """_reset_if_new_day resets _spent_today when date changes."""
        from datetime import date
        client = self._make_client(cap=1.0)
        client._spent_today = 4.99
        client._today = date(2020, 1, 1)  # Force stale date
        client._reset_if_new_day()
        assert client._spent_today == 0.0

    def test_day_rollover_updates_today(self):
        """_reset_if_new_day updates _today to the current date."""
        from datetime import date
        client = self._make_client(cap=1.0)
        client._spent_today = 3.50
        client._today = date(2020, 1, 1)
        client._reset_if_new_day()
        assert client._today == date.today()

    def test_no_rollover_when_same_day(self):
        """_reset_if_new_day does NOT reset when still the same day."""
        from datetime import date
        client = self._make_client(cap=1.0)
        client._spent_today = 2.50
        client._today = date.today()  # Same as today
        client._reset_if_new_day()
        assert client._spent_today == 2.50  # Unchanged

    def test_llm_response_has_correct_fields(self):
        """LLMResponse has all expected fields populated from the API response."""
        client = self._make_client(cap=1.0)
        mock_msg = self._make_mock_message(text="hello", input_tokens=200, output_tokens=300)

        with patch.object(client._client.messages, "create", return_value=mock_msg):
            resp, _ = client.call(model=HAIKU, system="system-prompt", user="user-msg")

        assert resp.model == HAIKU
        assert resp.content == "hello"
        assert resp.input_tokens == 200
        assert resp.output_tokens == 300
        assert resp.cost_usd == _calc_cost(HAIKU, 200, 300)
        assert resp.latency_ms >= 0
        assert len(resp.prompt_hash) == 12
