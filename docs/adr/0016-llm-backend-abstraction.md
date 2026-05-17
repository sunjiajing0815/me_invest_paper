# ADR-0016 — LLM Backend Abstraction

**Status:** Accepted
**Date:** 2026-05-14

---

## Context

Phase 3a shipped `LLMClient` as a concrete class wrapping the `anthropic` Python SDK. Phase 3b introduces a second backend: the `claude-agent-sdk`, which wraps the Claude Code CLI subprocess and exposes an async iterator interface. Both backends make calls to Anthropic models and should be interchangeable from the perspective of graph nodes and service layer code.

The motivation for the second backend:
- Pro/Max subscribers receive a monthly Agent SDK credit applied against API-key billing; on those plans, routing movers-triage calls through the Agent SDK can reduce net cost
- The Agent SDK path is independent of the Anthropic API path, providing resilience against API-endpoint outages (though not against Anthropic infrastructure outages — both paths depend on Anthropic)

## Decision

Pull `LLMClient` up to a `typing.Protocol` with two concrete implementations selected by the `LLM_BACKEND` env var. A `make_llm_client(settings)` factory handles dispatch.

```
LLM_BACKEND=anthropic_api  →  AnthropicAPIClient   (default, Phase 3a behaviour)
LLM_BACKEND=agent_sdk      →  AgentSDKClient        (opt-in, Phase 3b)
```

Unknown values fall back to `anthropic_api` and emit a warning log.

## Protocol shape

Both implementations must satisfy:

```python
class LLMClient(Protocol):
    def call(self, *, model, system, user, max_tokens=4096,
             response_schema=None) -> tuple[LLMResponse, BaseModel | None]: ...
    @property
    def daily_spent_usd(self) -> float: ...
```

## Five invariants (all backends must satisfy)

1. **Same `LLMResponse` shape.** `cost_usd` records the token-level cost computed via `_calc_cost` (or falls back to `total_cost_usd` from the SDK when token counts are unavailable). The Pro/Max monthly credit is a billing-layer offset not reflected in `cost_usd`.
2. **Same daily-cost-cap semantics.** `daily_spent_usd` accumulates token spend; resets at midnight. `RuntimeError` raised when cap is hit.
3. **Same `_strip_fences + model_validate_json` path.** Both backends apply fence stripping before schema validation. `(resp, None)` returned on parse failure. This keeps `llm_call_log.error` content identical across backends.
4. **Same `persist_llm_call_log` call site.** Neither backend writes the log row itself — the `llm_node_call` helper in `graphs/_nodes.py` handles logging. Neither backend should be modified to add logging.
5. **Same `prompt_hash` computation.** SHA-256 of `system + user`, first 12 chars. Identical across backends for cross-backend retrospective analysis.

## `AgentSDKClient` design notes

- Wraps the async `claude_agent_sdk.query()` iterator via `asyncio.run()` to provide a sync `call()` interface compatible with the rest of the codebase.
- **`asyncio.run()` is APScheduler-safe** — each job runs in a dedicated thread with no live event loop. It is **NOT safe inside an `async def` FastAPI route** — calling `asyncio.run()` from within an already-running event loop raises `RuntimeError`. If a future route needs to call `llm.call()` on an `AgentSDKClient`, expose an async sibling method.
- `ANTHROPIC_API_KEY` is always required, even for `LLM_BACKEND=agent_sdk`. The Agent SDK authenticates to Anthropic via API key, not via consumer OAuth. **Using consumer Claude.ai OAuth tokens for automated/unattended use is prohibited by Anthropic's Terms of Service.**
- `claude-agent-sdk` is pinned to `>=0.1.81,<0.2`. The SDK is at 0.1.x; minor-version bumps have historically shifted API surface. Upgrade deliberately and re-run `test_llm.py` after any bump.
- The Agent SDK spawns the Claude Code CLI subprocess. This means `claude` CLI must be installed and authenticated on the host. The SDK is not a pure API client.
- `max_turns=1` and `allowed_tools=[]` keep the Agent SDK in single-shot mode (one prompt → one response, no tool use). This matches `AnthropicAPIClient` behaviour.

## When to choose each backend

| Condition | Recommended backend |
|---|---|
| Default / no Pro/Max subscription | `anthropic_api` |
| Pro/Max subscriber; monthly SDK credit reduces net cost | `agent_sdk` |
| API endpoint experiencing elevated latency | `agent_sdk` (independent path) |
| Calling `llm.call()` from an async FastAPI route | `anthropic_api` only |
| Integrating tool use into LLM nodes in the future | `agent_sdk` (native tool support) |

## Future backend additions

A third backend (e.g., `bedrock`, `vertex`) must satisfy the same five invariants above. Add it as a new class implementing `LLMClient`, update `make_llm_client()`, and add a parallel test class in `test_llm.py`.

## Consumer OAuth — solo personal-use guardrails (Phase 3c)

`LLM_BACKEND=agent_sdk` routes calls through the Claude Code CLI subprocess. The `claude` CLI
can be logged in via consumer Claude.ai OAuth (`claude auth login`) in addition to the API key.

**This deployment is single-user personal use.** Anthropic's ToS permits the account owner to
automate personal workflows. However, the following guardrails apply:

- **`ANTHROPIC_API_KEY` is always the authentication source for LLM calls.** Consumer OAuth
  login may be present on the host (for interactive CLI use), but the Agent SDK routes all
  `query()` calls through the API key, not through consumer OAuth. The key must remain valid and
  tested in CI.
- **Never extend consumer OAuth into multi-user deployment.** If Phase 5 adds multiple users,
  each user's LLM calls must go through individual API keys — never through a shared consumer
  OAuth session, which would violate Anthropic's ToS (shared account across users).
- **`ANTHROPIC_API_KEY` must be exercised in CI even when `LLM_BACKEND=agent_sdk`.** The key
  guards cost-cap tracking, `llm_call_log` persistence, and the `_calc_cost` fallback path.
  Its absence creates silent failures that are difficult to diagnose in production.

## Consequences

- All callers that previously imported `LLMClient` (the concrete class) continue to work — `AnthropicAPIClient` is a drop-in for the old `LLMClient`. The import `from .services.llm import LLMClient` now imports the Protocol; `isinstance(obj, LLMClient)` works via `@runtime_checkable`.
- `main.py` lifespan uses `make_llm_client(_settings)` instead of `LLMClient(...)` directly.
- `tests/test_news_triage.py` uses `MagicMock(spec=LLMClient)` — this works with a `Protocol` target.
- The `llm_call_log.cost_usd` column reflects token-level cost, not actual billed amount. When comparing cost across backends, the column is directly comparable (both use the same `_calc_cost` rates), but may differ slightly from the Anthropic billing dashboard due to Pro/Max credits.
