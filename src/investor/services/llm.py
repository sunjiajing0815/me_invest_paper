"""Shared Anthropic LLM client: cost guard, JSON-schema validation, call-log persistence."""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TypeVar

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


class LLMClient:
    """Cost-guarded Anthropic client. One instance per app lifetime (stored on app.state)."""

    def __init__(self, api_key: str, daily_cost_cap_usd: float = 1.0) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._daily_cap = daily_cost_cap_usd
        self._spent_today: float = 0.0
        self._today: date = date.today()

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
    ) -> tuple[LLMResponse, T | None]:
        """Call the Anthropic API. Returns (LLMResponse, parsed_or_None).

        If response_schema is provided and parsing fails, the second element is None.
        Caller must treat None as a failed call and fall back to deterministic logic.
        """
        self._reset_if_new_day()
        if self._spent_today >= self._daily_cap:
            raise RuntimeError(
                f"daily LLM cost cap ${self._daily_cap:.2f} reached"
            )

        prompt_hash = hashlib.sha256((system + user).encode()).hexdigest()[:12]
        t0 = time.monotonic()
        msg: Message = self._client.messages.create(
            model=model,
            system=system,
            messages=[{"role": "user", "content": user}],
            max_tokens=max_tokens,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        first_block = msg.content[0]
        if not isinstance(first_block, TextBlock):
            raise RuntimeError(
                f"unexpected content block type: {type(first_block).__name__}"
            )
        content = first_block.text
        cost = _calc_cost(model, msg.usage.input_tokens, msg.usage.output_tokens)
        self._spent_today += cost

        resp = LLMResponse(
            content=content,
            model=model,
            prompt_hash=prompt_hash,
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
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


def _calc_cost(model: str, in_toks: int, out_toks: int) -> float:
    """Calculate USD cost based on model token rates. Raises KeyError on unknown model."""
    rates: dict[str, dict[str, float]] = {
        HAIKU:  {"in": 1.00 / 1_000_000, "out":  5.00 / 1_000_000},
        SONNET: {"in": 3.00 / 1_000_000, "out": 15.00 / 1_000_000},
    }
    r = rates[model]  # KeyError on unknown model — intentional
    return in_toks * r["in"] + out_toks * r["out"]


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
        )
    )
    session.flush()
