"""Shared LLM client: cost guard, JSON-schema validation, call-log persistence.

Two backends: AnthropicAPIClient (default, direct Anthropic SDK) and
AgentSDKClient (claude-agent-sdk wrapper). Both satisfy the LLMClient Protocol.
Select via LLM_BACKEND env var; make_llm_client() factory handles dispatch.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol, TypeVar, runtime_checkable

import anthropic
from anthropic.types import Message, TextBlock
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from ..models import LLMCallLog

log = logging.getLogger(__name__)

HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-4-6"

T = TypeVar("T", bound=BaseModel)


class LLMResponse(BaseModel):
    content: str
    model: str
    prompt_hash: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    # Sampling temperature actually applied. None when the backend can't set it
    # (the agent_sdk backend — ClaudeAgentOptions exposes no temperature).
    temperature: float | None = None
    # Prompt-cache tiers (anthropic_api only; 0 elsewhere or when caching is inactive).
    # Recorded separately so cost accounting bills writes at 1.25x and reads at 0.10x,
    # and so the cache-hit ratio (cache_read / (cache_read + input_tokens)) is derivable.
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0


# --- Transient-error retry (shared by both backends) ---------------------------

_RETRY_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY_S = 0.5


def _is_transient(exc: BaseException) -> bool:
    """True for retryable transport/overload errors; False for 4xx and schema issues."""
    if isinstance(
        exc,
        (
            anthropic.RateLimitError,
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
            anthropic.InternalServerError,
        ),
    ):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return getattr(exc, "status_code", 0) >= 500  # incl. 529 overloaded
    return False


def _run_with_retry[R](fn: Callable[[], R], *, label: str) -> R:
    """Run fn(), retrying transient errors with exponential backoff + jitter.

    Bounded at _RETRY_MAX_ATTEMPTS. Non-transient errors (4xx, ValueError, etc.)
    propagate immediately. Schema-validation failures never reach here — they happen
    after a successful call returns.
    """
    for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt >= _RETRY_MAX_ATTEMPTS or not _is_transient(exc):
                raise
            delay = _RETRY_BASE_DELAY_S * (2 ** (attempt - 1)) + random.uniform(
                0, _RETRY_BASE_DELAY_S
            )
            log.warning(
                "%s: transient error (attempt %d/%d): %s — retrying in %.2fs",
                label, attempt, _RETRY_MAX_ATTEMPTS, exc, delay,
            )
            time.sleep(delay)
    raise RuntimeError(f"{label}: retry loop exhausted")  # unreachable


def _usage_int(usage: object, attr: str) -> int:
    """Read an int usage field, coercing None/missing/non-int (e.g. test mocks) to 0."""
    v = getattr(usage, attr, 0)
    return v if isinstance(v, int) else 0


@runtime_checkable
class LLMClient(Protocol):
    """Satisfied by AnthropicAPIClient and AgentSDKClient. See ADR-0016."""

    def call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 4096,
        response_schema: type[BaseModel] | None = None,
        temperature: float = 0.0,
        cache_system: bool = True,
    ) -> tuple[LLMResponse, BaseModel | None]: ...

    @property
    def daily_spent_usd(self) -> float: ...


class AnthropicAPIClient:
    """Direct Anthropic Python SDK backend. Formerly named LLMClient (Phase 3a).

    Cost-guarded; one instance per app lifetime (stored on app.state).
    """

    def __init__(self, api_key: str, daily_cost_cap_usd: float = 1.0) -> None:
        # max_retries is the SDK's own transient-retry layer; _run_with_retry wraps it
        # for app-level logging + uniform behaviour with the agent_sdk backend.
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=2)
        self._daily_cap = daily_cost_cap_usd
        self._spent_today: float = 0.0
        self._today: date = date.today()

    @property
    def daily_spent_usd(self) -> float:
        return self._spent_today

    def _reset_if_new_day(self) -> None:
        today = date.today()
        if today != self._today:
            self._spent_today = 0.0
            self._today = today

    def call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 2048,
        response_schema: type[T] | None = None,
        temperature: float = 0.0,
        cache_system: bool = True,
    ) -> tuple[LLMResponse, T | None]:
        """Call the Anthropic API. Returns (LLMResponse, parsed_or_None).

        If response_schema is provided and parsing fails, the second element is None.
        Caller must treat None as a failed call and fall back to deterministic logic.

        ``temperature`` is forwarded to the API (default 0.0 for reproducible structured
        output; prose call-sites pass higher). ``cache_system`` marks the system prompt as
        an ephemeral cache breakpoint — a no-op below Anthropic's cache minimum, so it's
        always safe to leave on. Transient transport/overload errors are retried with
        backoff; schema-validation failures are not (they happen after a successful call).
        """
        self._reset_if_new_day()
        if self._spent_today >= self._daily_cap:
            raise RuntimeError(
                f"daily LLM cost cap ${self._daily_cap:.2f} reached"
            )

        prompt_hash = hashlib.sha256((system + user).encode()).hexdigest()[:12]
        # cache_control on the system block makes it a cache breakpoint (anthropic_api
        # only). Harmless below the cache minimum: no cache is created, no 1.25x write.
        system_param: Any = (
            [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            if cache_system
            else system
        )
        t0 = time.monotonic()
        msg: Message = _run_with_retry(
            lambda: self._client.messages.create(
                model=model,
                system=system_param,
                messages=[{"role": "user", "content": user}],
                max_tokens=max_tokens,
                temperature=temperature,
            ),
            label=f"anthropic.messages.create[{model}]",
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        first_block = msg.content[0]
        if not isinstance(first_block, TextBlock):
            raise RuntimeError(
                f"unexpected content block type: {type(first_block).__name__}"
            )
        content = first_block.text
        cache_write = _usage_int(msg.usage, "cache_creation_input_tokens")
        cache_read = _usage_int(msg.usage, "cache_read_input_tokens")
        cost = _calc_cost(
            model, msg.usage.input_tokens, msg.usage.output_tokens, cache_write, cache_read
        )
        self._spent_today += cost

        resp = LLMResponse(
            content=content,
            model=model,
            prompt_hash=prompt_hash,
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            temperature=temperature,
            cache_write_tokens=cache_write,
            cache_read_tokens=cache_read,
        )

        parsed: T | None = None
        if response_schema is not None:
            try:
                parsed = response_schema.model_validate_json(_strip_fences(content))
            except (ValidationError, ValueError) as exc:
                log.warning(
                    "LLM output failed schema validation: %s; raw=%r",
                    exc,
                    content[:500],
                )
        return resp, parsed


class AgentSDKClient:
    """claude-agent-sdk backend. Same interface as AnthropicAPIClient. See ADR-0016.

    call() bridges the async SDK into a sync interface via asyncio.run().
    Safe in APScheduler job threads (no live event loop). NOT safe inside
    async FastAPI routes — asyncio.run() raises RuntimeError if an event
    loop is already running.
    """

    def __init__(self, api_key: str, daily_cost_cap_usd: float = 3.0, cli_path: str = "") -> None:
        self._api_key = api_key
        self._daily_cap = daily_cost_cap_usd
        self._cli_path = cli_path or None  # None → SDK uses bundled CLI
        self._spent_today: float = 0.0
        self._spent_date: date = date.today()

    @property
    def daily_spent_usd(self) -> float:
        return self._spent_today

    def _roll_day_if_needed(self) -> None:
        if date.today() != self._spent_date:
            self._spent_today = 0.0
            self._spent_date = date.today()

    def call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 4096,
        response_schema: type[T] | None = None,
        temperature: float = 0.0,
        cache_system: bool = True,
    ) -> tuple[LLMResponse, T | None]:
        self._roll_day_if_needed()
        if self._spent_today >= self._daily_cap:
            raise RuntimeError(
                f"daily LLM cost cap ${self._daily_cap:.2f} reached"
            )

        # ClaudeAgentOptions exposes neither temperature nor prompt caching, so both are
        # no-ops here: the response records temperature=None and zero cache tiers.
        # (anthropic_api is the default/production backend where they apply.)
        log.debug(
            "AgentSDKClient: temperature=%s / cache_system=%s are no-ops on agent_sdk",
            temperature, cache_system,
        )

        prompt_hash = hashlib.sha256((system + user).encode()).hexdigest()[:12]

        def _invoke() -> Any:
            # asyncio.run() teardown calls shutdown_asyncgens() which triggers
            # "aclose(): already running" in claude-agent-sdk 0.1.x, and plain
            # loop.close() without cancellation leaves pending athrow tasks.
            # Correct sequence: cancel pending tasks + gather (clears athrow tasks),
            # then close — skipping shutdown_asyncgens() entirely.
            _loop = asyncio.new_event_loop()
            try:
                return _loop.run_until_complete(
                    self._async_call(model, system, user, max_tokens, self._cli_path)
                )
            finally:
                pending = asyncio.all_tasks(_loop)
                if pending:
                    for _t in pending:
                        _t.cancel()
                    _loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                _loop.close()

        t0 = time.monotonic()
        result_msg = _run_with_retry(_invoke, label=f"agent_sdk.query[{model}]")
        latency_ms = int((time.monotonic() - t0) * 1000)

        content = result_msg.result or ""
        usage_dict = result_msg.usage or {}
        input_tokens = int(usage_dict.get("input_tokens", 0))
        output_tokens = int(usage_dict.get("output_tokens", 0))
        # Use _calc_cost for consistency with AnthropicAPIClient; fall back to
        # SDK-reported total_cost_usd when token counts are unavailable.
        if input_tokens or output_tokens:
            cost = _calc_cost(model, input_tokens, output_tokens)
        else:
            cost = result_msg.total_cost_usd or 0.0
        self._spent_today += cost

        resp = LLMResponse(
            content=content,
            model=model,
            prompt_hash=prompt_hash,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            temperature=None,  # not settable via ClaudeAgentOptions
            # cache tiers stay 0 — agent_sdk doesn't expose prompt caching
        )

        parsed: T | None = None
        if response_schema is not None:
            try:
                parsed = response_schema.model_validate_json(_strip_fences(content))
            except (ValidationError, ValueError) as exc:
                log.warning(
                    "AgentSDK output failed schema: %s; raw=%r",
                    exc,
                    content[:500],
                )
        return resp, parsed

    async def _async_call(
        self, model: str, system: str, user: str, max_tokens: int, cli_path: str | None = None
    ) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

        options = ClaudeAgentOptions(
            model=model,
            max_turns=1,
            system_prompt=system,
            allowed_tools=[],
            cli_path=cli_path,
        )
        async for msg in query(prompt=user, options=options):
            if isinstance(msg, ResultMessage):
                if msg.is_error:
                    raise RuntimeError(
                        f"Agent SDK error: {msg.errors or 'unknown'}"
                    )
                return msg
        raise RuntimeError("Agent SDK returned no ResultMessage")


def make_llm_client(settings: object) -> LLMClient:
    """Factory: returns AnthropicAPIClient or AgentSDKClient based on settings.llm_backend."""
    backend = getattr(settings, "llm_backend", "anthropic_api")
    api_key = getattr(settings, "anthropic_api_key", "")
    daily_cap = getattr(settings, "llm_daily_cost_cap_usd", 3.0)

    if backend == "agent_sdk":
        cli_path = getattr(settings, "llm_cli_path", "") or ""
        return AgentSDKClient(api_key=api_key, daily_cost_cap_usd=daily_cap, cli_path=cli_path)
    if backend != "anthropic_api":
        log.warning("unknown LLM_BACKEND=%r; defaulting to anthropic_api", backend)
    return AnthropicAPIClient(api_key=api_key, daily_cost_cap_usd=daily_cap)


def _strip_fences(text: str) -> str:
    """Strip markdown code fences and extract the outermost JSON object."""
    text = text.strip()
    if text.startswith("```"):
        newline = text.find("\n")
        if newline != -1:
            text = text[newline + 1:]
        if text.endswith("```"):
            text = text[:-3].rstrip()
    # Fallback: locate the outermost { ... } so stray prefix/suffix chars don't break parsing.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text.strip()


def _calc_cost(
    model: str,
    in_toks: int,
    out_toks: int,
    cache_write_toks: int = 0,
    cache_read_toks: int = 0,
) -> float:
    """Calculate USD cost based on model token rates. Raises KeyError on unknown model.

    Prompt-cache tiers are billed relative to the base input rate (Anthropic pricing):
    cache writes at 1.25x, cache reads at 0.10x. When caching is active the API reports
    ``input_tokens`` as only the uncached portion, so the three input components don't
    double-count.
    """
    rates: dict[str, dict[str, float]] = {
        HAIKU:  {"in": 1.00 / 1_000_000, "out":  5.00 / 1_000_000},
        SONNET: {"in": 3.00 / 1_000_000, "out": 15.00 / 1_000_000},
    }
    r = rates[model]  # KeyError on unknown model — intentional
    return (
        in_toks * r["in"]
        + out_toks * r["out"]
        + cache_write_toks * r["in"] * 1.25
        + cache_read_toks * r["in"] * 0.10
    )


def load_prompt(filename: str) -> str:
    """Load a prompt file from src/investor/prompts/<filename>."""
    prompts_dir = Path(__file__).parent.parent / "prompts"
    return (prompts_dir / filename).read_text(encoding="utf-8")


def persist_llm_call_log(
    session: Session,
    resp: LLMResponse,
    *,
    purpose: str,
    status: str,
    error: str | None = None,
) -> None:
    """Write one LLMCallLog row. Call this from service layer, not from LLMClient itself."""
    session.add(
        LLMCallLog(
            ts=datetime.now(UTC),
            purpose=purpose,
            model=resp.model,
            prompt_hash=resp.prompt_hash,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            cost_usd=resp.cost_usd,
            latency_ms=resp.latency_ms,
            status=status,
            error=error,
            temperature=resp.temperature,
            cache_write_tokens=resp.cache_write_tokens,
            cache_read_tokens=resp.cache_read_tokens,
        )
    )
    session.flush()
