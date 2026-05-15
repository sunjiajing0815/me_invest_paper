"""Tests for AnthropicAPIClient, AgentSDKClient, make_llm_client, and helpers."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from investor.services.llm import (
    HAIKU,
    SONNET,
    AgentSDKClient,
    AnthropicAPIClient,
    LLMClient,
    LLMResponse,
    _calc_cost,
    _strip_fences,
    make_llm_client,
)


class TestStripFences:
    def test_plain_json_unchanged(self):
        s = '{"key": "value"}'
        assert _strip_fences(s) == s

    def test_strips_json_fences(self):
        s = '```json\n{"key": "value"}\n```'
        assert _strip_fences(s) == '{"key": "value"}'

    def test_strips_plain_fences(self):
        s = '```\n{"key": "value"}\n```'
        assert _strip_fences(s) == '{"key": "value"}'

    def test_strips_leading_trailing_whitespace(self):
        s = '  ```json\n{"key": "value"}\n```  '
        assert _strip_fences(s) == '{"key": "value"}'

    def test_no_closing_fence(self):
        # Truncated response — don't crash, return what we have
        s = '```json\n{"key": "value"}'
        result = _strip_fences(s)
        assert result == '{"key": "value"}'


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


class TestAnthropicAPIClientCostGuard:
    def _make_client(self, cap: float = 5.0) -> AnthropicAPIClient:
        return AnthropicAPIClient(api_key="test-key", daily_cost_cap_usd=cap)

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
        client = self._make_client(cap=1.0)
        client._spent_today = 4.99
        client._today = date(2020, 1, 1)  # Force stale date
        client._reset_if_new_day()
        assert client._spent_today == 0.0

    def test_day_rollover_updates_today(self):
        """_reset_if_new_day updates _today to the current date."""
        client = self._make_client(cap=1.0)
        client._spent_today = 3.50
        client._today = date(2020, 1, 1)
        client._reset_if_new_day()
        assert client._today == date.today()

    def test_no_rollover_when_same_day(self):
        """_reset_if_new_day does NOT reset when still the same day."""
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

    def test_daily_spent_usd_property(self):
        client = self._make_client(cap=1.0)
        assert client.daily_spent_usd == 0.0
        client._spent_today = 0.5
        assert client.daily_spent_usd == 0.5

    def test_satisfies_llm_client_protocol(self):
        client = self._make_client()
        assert isinstance(client, LLMClient)


def _make_mock_result_message(
    result_text: str = '{"value": 1}',
    input_tokens: int = 100,
    output_tokens: int = 100,
    total_cost_usd: float | None = None,
    is_error: bool = False,
):
    """Build a mock ResultMessage for AgentSDKClient tests."""
    from claude_agent_sdk import ResultMessage
    msg = MagicMock(spec=ResultMessage)
    msg.result = result_text
    msg.is_error = is_error
    msg.errors = None
    msg.usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    msg.total_cost_usd = total_cost_usd
    return msg


class TestAgentSDKClient:
    def _make_client(self, cap: float = 5.0) -> AgentSDKClient:
        return AgentSDKClient(api_key="test-key", daily_cost_cap_usd=cap)

    def test_happy_path(self):
        """Returns LLMResponse with correct shape from a successful Agent SDK call."""
        client = self._make_client()
        result_msg = _make_mock_result_message(
            result_text='{"value": 42}', input_tokens=100, output_tokens=50
        )

        async def fake_async_call(*args, **kwargs):
            return result_msg

        with patch.object(client, "_async_call", fake_async_call):
            resp, parsed = client.call(model=HAIKU, system="sys", user="usr")

        assert isinstance(resp, LLMResponse)
        assert resp.content == '{"value": 42}'
        assert resp.input_tokens == 100
        assert resp.output_tokens == 50
        assert resp.cost_usd == _calc_cost(HAIKU, 100, 50)
        assert resp.model == HAIKU
        assert len(resp.prompt_hash) == 12

    def test_schema_parse_success(self):
        """Parsed pydantic object returned when content matches schema."""
        from pydantic import BaseModel

        class MySchema(BaseModel):
            value: int

        client = self._make_client()
        result_msg = _make_mock_result_message(result_text='{"value": 99}')

        async def fake_async_call(*args, **kwargs):
            return result_msg

        with patch.object(client, "_async_call", fake_async_call):
            resp, parsed = client.call(model=HAIKU, system="s", user="u", response_schema=MySchema)

        assert parsed is not None
        assert parsed.value == 99

    def test_schema_error_returns_none(self):
        """Returns (resp, None) when content doesn't match schema."""
        from pydantic import BaseModel

        class MySchema(BaseModel):
            value: int

        client = self._make_client()
        result_msg = _make_mock_result_message(result_text="not valid json")

        async def fake_async_call(*args, **kwargs):
            return result_msg

        with patch.object(client, "_async_call", fake_async_call):
            resp, parsed = client.call(model=HAIKU, system="s", user="u", response_schema=MySchema)

        assert isinstance(resp, LLMResponse)
        assert parsed is None

    def test_fence_regression(self):
        """Fenced JSON is stripped; schema parse succeeds (not a schema_error)."""
        from pydantic import BaseModel

        class MySchema(BaseModel):
            value: int

        client = self._make_client()
        result_msg = _make_mock_result_message(result_text='```json\n{"value": 7}\n```')

        async def fake_async_call(*args, **kwargs):
            return result_msg

        with patch.object(client, "_async_call", fake_async_call):
            resp, parsed = client.call(model=HAIKU, system="s", user="u", response_schema=MySchema)

        assert parsed is not None
        assert parsed.value == 7

    def test_daily_cap_raises(self):
        """RuntimeError raised when daily cap is already reached."""
        client = self._make_client(cap=0.001)
        client._spent_today = 0.001  # Already at cap

        with pytest.raises(RuntimeError, match="daily LLM cost cap"):
            client.call(model=HAIKU, system="s", user="u")

    def test_day_rollover_resets_spent(self):
        """_roll_day_if_needed resets when date changes."""
        client = self._make_client(cap=1.0)
        client._spent_today = 0.9
        client._spent_date = date(2020, 1, 1)
        client._roll_day_if_needed()
        assert client._spent_today == 0.0
        assert client._spent_date == date.today()

    def test_no_rollover_same_day(self):
        """_roll_day_if_needed does not reset when still the same day."""
        client = self._make_client(cap=1.0)
        client._spent_today = 0.5
        client._spent_date = date.today()
        client._roll_day_if_needed()
        assert client._spent_today == 0.5

    def test_cost_calculated_from_tokens(self):
        """cost_usd uses _calc_cost when tokens are available."""
        client = self._make_client()
        result_msg = _make_mock_result_message(input_tokens=500, output_tokens=200)

        async def fake_async_call(*args, **kwargs):
            return result_msg

        with patch.object(client, "_async_call", fake_async_call):
            resp, _ = client.call(model=HAIKU, system="s", user="u")

        assert abs(resp.cost_usd - _calc_cost(HAIKU, 500, 200)) < 1e-9

    def test_cost_falls_back_to_total_cost_usd(self):
        """cost_usd falls back to total_cost_usd when token counts are 0."""
        client = self._make_client()
        result_msg = _make_mock_result_message(
            input_tokens=0, output_tokens=0, total_cost_usd=0.0042
        )

        async def fake_async_call(*args, **kwargs):
            return result_msg

        with patch.object(client, "_async_call", fake_async_call):
            resp, _ = client.call(model=HAIKU, system="s", user="u")

        assert abs(resp.cost_usd - 0.0042) < 1e-9

    def test_prompt_hash_matches_anthropic_client(self):
        """Same system+user produces identical prompt_hash across both backends."""
        import hashlib
        system, user = "test system", "test user"
        expected_hash = hashlib.sha256((system + user).encode()).hexdigest()[:12]

        api_client = AnthropicAPIClient(api_key="k", daily_cost_cap_usd=10.0)
        sdk_client = self._make_client()

        result_msg = _make_mock_result_message()

        async def fake_async_call(*args, **kwargs):
            return result_msg

        # AnthropicAPIClient hash
        from anthropic.types import TextBlock
        mock_msg = MagicMock()
        mock_block = MagicMock(spec=TextBlock)
        mock_block.text = "hello"
        mock_msg.content = [mock_block]
        mock_msg.usage.input_tokens = 10
        mock_msg.usage.output_tokens = 10
        with patch.object(api_client._client.messages, "create", return_value=mock_msg):
            api_resp, _ = api_client.call(model=HAIKU, system=system, user=user)

        with patch.object(sdk_client, "_async_call", fake_async_call):
            sdk_resp, _ = sdk_client.call(model=HAIKU, system=system, user=user)

        assert api_resp.prompt_hash == expected_hash
        assert sdk_resp.prompt_hash == expected_hash

    def test_no_result_message_raises(self):
        """RuntimeError if Agent SDK yields no ResultMessage."""
        client = self._make_client()

        async def fake_async_call(*args, **kwargs):
            raise RuntimeError("Agent SDK returned no ResultMessage")

        with (
            patch.object(client, "_async_call", fake_async_call),
            pytest.raises(RuntimeError, match="Agent SDK returned no ResultMessage"),
        ):
            client.call(model=HAIKU, system="s", user="u")

    def test_daily_spent_usd_property(self):
        client = self._make_client()
        assert client.daily_spent_usd == 0.0
        client._spent_today = 1.23
        assert client.daily_spent_usd == 1.23

    def test_satisfies_llm_client_protocol(self):
        client = self._make_client()
        assert isinstance(client, LLMClient)


class TestMakeLLMClient:
    def _make_settings(self, backend: str) -> object:
        class FakeSettings:
            anthropic_api_key = "test-key"
            llm_daily_cost_cap_usd = 3.0
            llm_backend = backend
        return FakeSettings()

    def test_anthropic_api_returns_anthropic_client(self):
        settings = self._make_settings("anthropic_api")
        client = make_llm_client(settings)
        assert isinstance(client, AnthropicAPIClient)

    def test_agent_sdk_returns_agent_sdk_client(self):
        settings = self._make_settings("agent_sdk")
        client = make_llm_client(settings)
        assert isinstance(client, AgentSDKClient)

    def test_unknown_backend_falls_back_to_anthropic(self):
        """Unknown backend value logs warning and returns AnthropicAPIClient."""
        settings = self._make_settings("typo_backend")
        client = make_llm_client(settings)
        assert isinstance(client, AnthropicAPIClient)

    def test_result_satisfies_protocol(self):
        """Both backends satisfy the LLMClient Protocol."""
        for backend in ("anthropic_api", "agent_sdk"):
            settings = self._make_settings(backend)
            client = make_llm_client(settings)
            assert isinstance(client, LLMClient), f"{backend} should satisfy LLMClient"
