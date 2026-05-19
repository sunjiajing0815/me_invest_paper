# ADR-0020: Tavily Weekly Market Context

**Status:** Accepted  
**Date:** 2026-05-19  
**Phase:** 4.5

## Context

Phase 4 delivers the Friday weekly review email with 7 sections (account snapshot, suggestion audit, drift state, material news, preview suggestions, auto-trade activity, Moomoo status). A recurring gap: the email has no narrative about what happened in markets that week — readers must pull context themselves before acting on suggestions.

Phase 4.5 adds an 8th section: **Weekly Market Context**. Requirements:

- Macro/Fed narrative (2–4 sentences)
- Sector narrative for tickers in the watchlist (2–4 sentences)
- Per-ticker catch-up for tickers that didn't hit the daily ≥5% mover threshold
- Forward-looking events for next week (earnings, Fed schedule, max 5 bullets)
- Source citations (URL + title) for auditability
- Strictly informational — no output from this section may influence the suggestion engine or order-execution path

## Decision

Use **Tavily** as the search backend, with a **Protocol** wrapper to enable future substitution.

### Why Tavily

| Criterion | Tavily | Serper | Brave Search | Perplexity Sonar |
|---|---|---|---|---|
| Finance topic | `topic="finance"` native | General web | General web | General web |
| News topic | `topic="news"` native | News mode | — | — |
| `days` recency filter | Native | Via `tbs=` hack | Limited | — |
| LLM-optimised content | Yes (`search_depth="advanced"`) | Partial | Partial | Yes |
| Free tier | 1 000 searches/month | 100/month | 2 000/month | Very limited |
| Python SDK | `tavily-python` | `google-search-results` | Custom | `openai` compat |
| Cost at 200 searches/week | ~$0/month on free tier | ~$5/month | ~$0 on free tier | ~$40/month |
| Established API stability | SDK `>=0.6` (pre-1.0) | Stable | Stable | Stable |

Tavily's `topic="finance"` and `topic="news"` modes with `days` filtering are precisely the access pattern needed for weekly market summaries. LLM-optimised content extraction reduces the synthesis prompt length (fewer tokens → lower cost).

### Protocol wrapper (`TavilyClient`)

`TavilyClient` is a `typing.Protocol` so the concrete backend is swappable at the factory level without changing any call-site code. The swap path is documented below.

**Reason for Protocol rather than ABC:** The test double (`FakeTavilyClient`) satisfies the protocol structurally without inheriting from an ABC, keeping test setup clean and consistent with how `LLMClient` and `BrokerAdapter` are implemented.

### SDK version pinning

Pin `tavily-python>=0.6,<0.7`. Tavily was acquired by Nebius in February 2026; the SDK is pre-1.0. Tight pinning prevents silent breakage from upstream API surface changes post-acquisition.

### Monthly search cap

`TavilyConcreteClient` tracks `_used_this_month` per-instance. Once `_used_this_month >= _cap` (default 200, configurable via `TAVILY_MONTHLY_CAP`), all searches return `[]` with a WARNING log. The weekly review section is then omitted (graceful degradation — no broken HTML, no error email).

### Graceful degradation chain

1. `TAVILY_API_KEY` not set → `make_tavily_client()` returns `FakeTavilyClient()` → all searches return `[]`
2. Monthly cap reached → all searches return `[]`
3. SDK exception → that individual search returns `[]`, others continue
4. All Tavily results empty → `build_weekly_market_context()` returns `None`
5. `None` returned → `run_weekly_review` keeps `market_context=None` on `WeeklyReview`
6. Template guard `{% if review.market_context %}` → section absent from email

The weekly review email is never blocked by a Tavily outage.

### Hard constraint: informational only

Tavily results are pre-market narrative — they must never influence the suggestion engine or order-execution path. Enforcement:

- `build_weekly_market_context()` returns a `WeeklyMarketContext` frozen dataclass — it has no pathway to `generate_suggestions()`, `run_auto_trade_pass()`, or any broker adapter
- CI grep test (`test_no_unauthorized_submit_order.py`) continues to enforce that `submit_order` is only called from `services/auto_trade.py`
- This ADR is cited in `CLAUDE.md` under "Things to never do"

## Alternatives Considered

### Serper

Cheaper at scale (under the same free tier limit). No `finance` topic; requires `tbs=` date-range hacks that are undocumented and fragile. General web results noisier for financial content. **Rejected** — worse signal quality.

### Brave Search

Generous free tier (2 000/month). No `finance` or `news` topic; `freshness` filter coarser than Tavily's `days`. Would need more prompt engineering to compensate. **Deferred** — viable swap target if Tavily pricing changes ≥20%.

### Perplexity Sonar

LLM-native; returns synthesized summaries directly. ~10× cost of Tavily + Sonnet synthesis at same weekly volume. Harder to unit-test (no easy fake). **Rejected** — cost and testability concerns outweigh the reduced integration complexity.

### Direct Finnhub / Alpaca news API

Already used for per-ticker material news in `services/news.py`. Covers individual stock news well but has no macro/sector query capability. Would require many ticker-specific calls to approximate the macro narrative. **Rejected for macro** — `services/news.py` remains the right tool for per-ticker event detection.

## Swap Path

If Tavily becomes unsuitable (pricing, acquisition-driven API change, or downtime):

1. Create a new concrete client implementing `TavilyClient` Protocol (e.g. `SerperClient`, `BraveClient`)
2. Update `make_tavily_client()` factory in `services/tavily.py` to return the new client
3. No call-site changes needed — `build_weekly_market_context` receives the Protocol type
4. Update `TAVILY_API_KEY` env var to the new provider's key (or add a new env var + factory branch)

Re-evaluate provider choice every 6 months or when pricing changes ≥20%.

## Consequences

- **New env vars:** `TAVILY_API_KEY` (optional, empty = graceful skip), `TAVILY_MONTHLY_CAP` (default 200)
- **New files:** `services/tavily.py`, `services/weekly_context.py`, `prompts/weekly_context_v1.txt`
- **New config fields:** `Settings.tavily_api_key`, `Settings.tavily_monthly_cap`, `Settings.weekly_context_prompt_version`
- **Dependency added:** `tavily-python>=0.6,<0.7`
- **Weekly LLM cost increase:** ~$0.02–$0.05/week (1 Sonnet call, ~2 000 tokens input, ~500 output)
- **Test coverage:** `tests/test_tavily.py` (11 tests), `tests/test_weekly_context.py` (5 tests)
