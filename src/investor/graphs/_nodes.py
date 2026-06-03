"""Generic LLM node helper for LangGraph nodes in the investor graphs package."""
from __future__ import annotations

import logging
from collections.abc import Callable

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from ..services.llm import (
    LLMClient,
    LLMResponse,
    _strip_fences,
    persist_llm_call_log,
)

log = logging.getLogger(__name__)


def llm_node_call[T: BaseModel](
    *,
    purpose: str,
    model: str,
    system: str,
    user: str,
    schema: type[T],
    fallback_factory: Callable[[], T],
    llm: LLMClient,
    session: Session,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    cache_system: bool = True,
) -> tuple[T, dict[str, object]]:
    """Call the LLM and persist a diagnostic log row; return (result, telemetry).

    Uses the Phase 3a LLMClient so cost-cap enforcement and prompt hashing happen
    in one place. On schema-validation failure the Pydantic ValidationError message
    (not the raw fenced content) is stored in the log row — Phase 3a Bug 3 lesson.

    The session must NOT be committed inside this function; commits are the
    caller's responsibility (session_scope() commits after graph.invoke() returns).

    Args:
        purpose: Short label written to the LLMCallLog row (e.g. "news_triage").
        model: Anthropic model ID (use HAIKU or SONNET from services.llm).
        system: System prompt string.
        user: User prompt string.
        schema: Pydantic BaseModel subclass for structured-output validation.
        fallback_factory: Zero-arg callable that returns a safe default when the
            LLM call fails or schema validation fails.
        llm: Shared LLMClient instance (typically from app.state).
        session: Open SQLAlchemy Session for persisting the LLMCallLog row.
        max_tokens: Token budget for the completion (default 4096 — larger than
            Phase 3a's 1500 because news batches require more headroom).
        temperature: Sampling temperature (default 0.0 for reproducible structured
            output; prose nodes pass higher). Forwarded to the LLM; recorded in the log.
        cache_system: Mark the system prompt as an ephemeral cache breakpoint
            (anthropic_api only; harmless no-op below the cache minimum).

    Returns:
        A two-tuple of (parsed_result_or_fallback, telemetry_dict).
        telemetry_dict keys are ``f"{purpose}_model"``, ``f"{purpose}_cost_usd"``,
        and ``f"{purpose}_status"``.
    """
    resp: LLMResponse
    parsed: T | None
    resp, parsed = llm.call(
        model=model,
        system=system,
        user=user,
        max_tokens=max_tokens,
        response_schema=schema,
        temperature=temperature,
        cache_system=cache_system,
    )

    error_msg: str | None = None
    if parsed is None:
        # Re-parse to capture the actual Pydantic ValidationError message
        # (not the raw fenced content). Phase 3a Bug 3 lesson.
        try:
            schema.model_validate_json(_strip_fences(resp.content))
        except ValidationError as exc:
            error_msg = str(exc)[:500]
        except Exception as exc:  # noqa: BLE001
            error_msg = str(exc)[:500]

    status = "ok" if parsed is not None else "schema_error"
    persist_llm_call_log(session, resp, purpose=purpose, status=status, error=error_msg)

    telemetry: dict[str, object] = {
        f"{purpose}_model": resp.model,
        f"{purpose}_cost_usd": resp.cost_usd,
        f"{purpose}_status": status,
    }

    return (parsed if parsed is not None else fallback_factory(), telemetry)
