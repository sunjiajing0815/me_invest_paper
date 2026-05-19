# Phase 4.5 — Tavily-Driven Weekly Market Context: Step-by-Step Guide

**Goal:** Augment the Phase 3b news pipeline (Alpaca + Finnhub for per-ticker daily classification) with a **weekly market-context digest** powered by Tavily — an AI-optimized search API. The new content lands as a "Weekly Market Context" section in the existing Phase 4 Friday weekly review email, with three blocks: (a) macro / sector narrative, (b) per-ticker catch-up for watchlist tickers whose news didn't hit the daily ≥5% mover threshold, (c) forward-looking events (upcoming earnings, Fed meetings, regulatory deadlines next week).

**Out of scope for Phase 4.5:** Per-user Tavily keys (Phase 5 multi-tenant work). Tavily-driven suggestion generation — the synthesis is informational; suggestions still flow through Phase 3c's review graph only. Real-time / intra-day Tavily queries — this is weekly, not daily.

**Time budget:** 3–5 days (1 long evening or 2–3 short evenings). The work is small and focused — a service wrapper, one synthesis service with prompt, integration into the existing weekly review email template, plus tests.

**Definition of done:** all 9 smoke-test rows pass, *and* you've received at least **two consecutive Friday weekly review emails** with the new "Weekly Market Context" section populated by Tavily-sourced content and Sonnet synthesis. The content reads as sensible market context — not as recommendations and not as hallucinated claims. Tag: `v0.4.5.0`.

**Depends on:** Phase 4 (`v0.4.0-phase-4-code-complete`) tagged. The Friday weekly review email infrastructure (cron, template, email composer) must exist; Phase 4.5 adds a new section to it rather than building a new email from scratch. Does *not* depend on any of Phase 4's auto-trade promotion tags — Phase 4.5 can ship in parallel with the `v0.4.1` → `v0.4.4` auto-trade soak progression.

---

## Architecture context — what's new in Phase 4.5

```
                                          ┌─────────────────────────┐
                                          │ Tavily API              │
                                          │ (post-Feb-2026 Nebius)  │
                                          │ • topic="news" + days=7 │
                                          │ • topic="finance"       │
                                          │ • free tier 1k/mo       │
                                          └────────────┬────────────┘
                                                       │
                                          ┌────────────▼────────────┐
                                          │ services/tavily.py      │
                                          │ • TavilyClient Protocol │
                                          │ • TavilyConcreteClient  │
                                          │ • cost-cap accounting   │
                                          │ • JSON-schema validation│
                                          │ • result dataclasses    │
                                          └────────────┬────────────┘
                                                       │
                                          ┌────────────▼────────────┐
                                          │ services/               │
                                          │   weekly_context.py     │
                                          │ • Tavily fanout queries:│
                                          │   macro, ticker catch-  │
                                          │   up, forward events    │
                                          │ • Sonnet synthesis →    │
                                          │   WeeklyMarketContext   │
                                          │   frozen dataclass      │
                                          └────────────┬────────────┘
                                                       │
                                          ┌────────────▼────────────┐
                                          │ jobs/weekly_review.py   │
                                          │ (Phase 4 — extended)    │
                                          │ • Friday 17:00 ET cron  │
                                          │ • new "Weekly Market    │
                                          │   Context" section in   │
                                          │   the email             │
                                          └─────────────────────────┘
```

Four behaviours to internalize:

1. **Tavily is one of two news layers — they're complementary, not redundant.** Phase 3b's Alpaca+Finnhub pipeline does per-ticker daily headline classification. Phase 4.5's Tavily layer does weekly macro + sector + forward-looking. They don't share code; they don't share schema; they don't share cadence. Don't try to merge them — the failure modes are different and the merger creates more confusion than it removes.
2. **Tavily results are evidence, not recommendations.** Same hard guardrails as Phase 3 LLM work: no price targets, no buy/sell recommendations, no fundamental claims beyond what the Tavily content visibly supports. The synthesis prompt enforces; JSON-schema validation rejects violations.
3. **The integration is intentionally thin behind a Protocol.** `TavilyClient` is a Protocol; the concrete implementation is small. If Nebius post-acquisition changes pricing or breaks API stability, the swap to a competitor (Serper, Brave, Perplexity) costs less than a day. ADR-0020 documents the rationale and the swap path.
4. **The output lives inside the existing Friday weekly review email**, not in a new email. One Friday digest, more content. Reduces noise; uses the email-render infrastructure that's already there.

---

## 0. Pre-flight checklist (~15 minutes)

- [ ] **Phase 4 `v0.4.0-phase-4-code-complete` tagged.** The Friday weekly review email infrastructure (`jobs/weekly_review.py`, `templates/weekly_review.html.j2`, `templates/weekly_review.txt.j2`) must exist. Phase 4.5 extends them.
- [ ] **Tavily account created at https://tavily.com.** Free tier is sufficient for solo personal use (1,000 searches/month; typical Phase 4.5 monthly use is 40–60).
- [ ] **API key generated and saved.** Add `TAVILY_API_KEY=` to `.env` and `.env.example`.
- [ ] **`tavily-python` installed:** `uv add 'tavily-python>=0.6,<0.7'`. Pin tightly — Tavily was acquired by Nebius in Feb 2026 and pre-1.0 SDK churn risk is real.
- [ ] **Note Nebius acquisition** in your operational runbook: if Tavily ever breaks or pricing changes adversely, ADR-0020 documents the swap path. Don't be surprised by it; just plan for it.
- [ ] **Phase 3a LLM cost cap (`LLM_DAILY_COST_CAP_USD`) confirmed at ≥ $3.00.** Phase 4.5 adds one Sonnet synthesis call per week, ~$0.02–0.05 each. Negligible but worth confirming the cap covers it.

---

## 1. `services/tavily.py` — the thin SDK wrapper (~half evening)

```python
# src/investor/services/tavily.py
import logging
from dataclasses import dataclass
from datetime import date
from typing import Literal, Protocol
from pydantic import BaseModel

from tavily import TavilyClient as _TavilySDK

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsResult:
    """Single result from Tavily, projected to our dataclass.

    Frozen so it's safe to pass through service boundaries. No ORM linkage —
    Phase 4.5 doesn't persist these; the synthesized WeeklyMarketContext is
    what gets stored in the email-rendered HTML.
    """
    title: str
    url: str
    content: str                    # extracted snippet (Tavily returns ~500-1000 chars)
    published_date: date | None     # may be None for general web search
    source_domain: str
    score: float                    # Tavily's relevance score, 0..1


class TavilyClient(Protocol):
    """Behind a Protocol so the concrete impl can be swapped (Serper, Brave, etc.)
    if Tavily becomes unsuitable post-acquisition."""

    def search_news(
        self, query: str, *, days: int = 7, max_results: int = 5,
    ) -> list[NewsResult]: ...

    def search_finance(
        self, query: str, *, days: int = 7, max_results: int = 5,
    ) -> list[NewsResult]: ...


class TavilyConcreteClient:
    def __init__(self, api_key: str, monthly_searches_cap: int = 200):
        self._client = _TavilySDK(api_key=api_key)
        self._cap = monthly_searches_cap
        self._used_this_month: int = 0     # caller's responsibility to reset monthly

    def search_news(
        self, query: str, *, days: int = 7, max_results: int = 5,
    ) -> list[NewsResult]:
        return self._search(query, topic="news", days=days, max_results=max_results)

    def search_finance(
        self, query: str, *, days: int = 7, max_results: int = 5,
    ) -> list[NewsResult]:
        return self._search(query, topic="finance", days=days, max_results=max_results)

    def _search(
        self, query: str, *, topic: Literal["news", "finance"],
        days: int, max_results: int,
    ) -> list[NewsResult]:
        if self._used_this_month >= self._cap:
            log.warning("Tavily monthly cap reached (%d); returning empty result", self._cap)
            return []
        try:
            resp = self._client.search(
                query=query, topic=topic, days=days,
                max_results=max_results, search_depth="advanced",
            )
        except Exception as e:
            log.warning("Tavily search failed: %s", e, exc_info=True)
            return []
        self._used_this_month += 1
        return [
            NewsResult(
                title=r["title"], url=r["url"],
                content=r.get("content", "")[:1500],
                published_date=_parse_date(r.get("published_date")),
                source_domain=_domain(r["url"]),
                score=float(r.get("score", 0.0)),
            )
            for r in resp.get("results", [])
        ]


class FakeTavilyClient:
    """For tests. Records calls; returns canned results."""
    def __init__(self, canned: dict[str, list[NewsResult]] | None = None):
        self.calls: list[tuple[str, str, int]] = []
        self._canned = canned or {}

    def search_news(self, query, *, days=7, max_results=5):
        self.calls.append((query, "news", days))
        return self._canned.get(query, [])

    def search_finance(self, query, *, days=7, max_results=5):
        self.calls.append((query, "finance", days))
        return self._canned.get(query, [])
```

Three properties to verify:

- Cost-cap respected. After `_used_this_month >= _cap`, search returns `[]` (silent degradation; weekly digest still ships, just thinner).
- Exception handling falls through to empty result + warning log with traceback. Never raises into the caller.
- `FakeTavilyClient` is the test substitute; all unit tests use it. Only the integration smoke test hits real Tavily.

Factory pattern in `services/tavily.py`:

```python
def make_tavily_client(settings) -> TavilyClient:
    if not settings.tavily_api_key:
        log.warning("TAVILY_API_KEY not set; Phase 4.5 weekly context will be skipped")
        return FakeTavilyClient()
    return TavilyConcreteClient(
        api_key=settings.tavily_api_key,
        monthly_searches_cap=settings.tavily_monthly_cap,
    )
```

---

## 2. `services/weekly_context.py` — fanout queries + Sonnet synthesis (~1 evening)

```python
# src/investor/services/weekly_context.py
from dataclasses import dataclass
from datetime import date
from pydantic import BaseModel

from investor.services.tavily import TavilyClient, NewsResult
from investor.services.llm import LLMClient, SONNET


@dataclass(frozen=True)
class WeeklyMarketContext:
    week_of: date
    macro_summary: str               # 2-4 sentences
    sector_summary: str              # 2-4 sentences
    ticker_catchup: dict[str, str]   # ticker -> 1-2 sentence catch-up
    forward_events: list[str]        # bullet points: upcoming earnings, Fed, etc.
    citations: list[NewsResult]      # raw Tavily results, surfaced in email as source links


class WeeklyContextSynthesis(BaseModel):
    macro_summary: str
    sector_summary: str
    ticker_catchup: dict[str, str]
    forward_events: list[str]


def build_weekly_market_context(
    *, tavily: TavilyClient, llm: LLMClient,
    watchlist: list[str], week_of: date,
) -> WeeklyMarketContext | None:
    """Fanout Tavily queries, then synthesize with Sonnet.

    Returns None on full failure (Tavily empty + LLM failure) so the caller
    can omit the section gracefully rather than render a broken one.
    """
    # 1. Macro queries
    macro = tavily.search_news("US Federal Reserve policy this week", days=7, max_results=4)
    macro += tavily.search_finance("US equity market this week", days=7, max_results=3)

    # 2. Sector queries (auto-derived from watchlist; could refine via ticker → sector mapping)
    sectors = _infer_sectors(watchlist)        # ["technology", "consumer discretionary", ...]
    sector_results: list[NewsResult] = []
    for s in sectors:
        sector_results += tavily.search_finance(f"{s} sector news this week", days=7, max_results=3)

    # 3. Per-ticker catch-up — broader news that may not have hit the 5% mover threshold
    ticker_results: dict[str, list[NewsResult]] = {}
    for t in watchlist:
        ticker_results[t] = tavily.search_finance(f"{t} stock news this week", days=7, max_results=3)

    # 4. Forward-looking events
    forward = tavily.search_news("US stock market earnings calendar next week", days=2, max_results=5)
    forward += tavily.search_news("US Federal Reserve next week schedule", days=2, max_results=3)

    if not (macro or sector_results or any(ticker_results.values()) or forward):
        return None                            # Tavily silent or capped — skip section

    # 5. Synthesize via Sonnet
    user_prompt = json.dumps({
        "week_of": str(week_of),
        "watchlist": watchlist,
        "macro_results": [r.__dict__ for r in macro[:6]],
        "sector_results": [r.__dict__ for r in sector_results[:8]],
        "ticker_results": {
            t: [r.__dict__ for r in rs[:3]] for t, rs in ticker_results.items()
        },
        "forward_results": [r.__dict__ for r in forward[:6]],
    }, default=str)

    system_prompt = load_prompt(f"weekly_context_v{settings.weekly_context_prompt_version}.txt")
    _, parsed = llm.call(
        model=SONNET, system=system_prompt, user=user_prompt,
        max_tokens=2048, response_schema=WeeklyContextSynthesis,
    )
    if parsed is None:
        log.warning("weekly context synthesis failed; rendering with empty sections + citations only")
        return WeeklyMarketContext(
            week_of=week_of, macro_summary="", sector_summary="",
            ticker_catchup={}, forward_events=[],
            citations=list(macro + sector_results + sum(ticker_results.values(), []) + forward),
        )

    all_citations = list(macro + sector_results + sum(ticker_results.values(), []) + forward)
    # Dedup by URL; sort by Tavily score desc
    seen: set[str] = set()
    unique_cites = []
    for c in sorted(all_citations, key=lambda r: -r.score):
        if c.url in seen: continue
        seen.add(c.url); unique_cites.append(c)

    return WeeklyMarketContext(
        week_of=week_of,
        macro_summary=parsed.macro_summary,
        sector_summary=parsed.sector_summary,
        ticker_catchup=parsed.ticker_catchup,
        forward_events=parsed.forward_events,
        citations=unique_cites[:15],            # cap at 15 to keep email readable
    )
```

The synthesis prompt `prompts/weekly_context_v1.txt`:

```
You are a market-context synthesizer for a long-term US-equities investor.
You will receive raw search results from Tavily across four categories:
macro, sectors, per-ticker, and forward-looking. Your job is to write a
concise weekly digest.

Output a JSON object with four fields:

1. macro_summary: 2-4 sentences on the macro environment this week (Fed,
   inflation, key economic prints, market-wide moves). Factual only;
   cite from macro_results.

2. sector_summary: 2-4 sentences on sector rotations or sector-level news
   relevant to the user's watchlist. Reference sectors visible in
   sector_results.

3. ticker_catchup: a dict mapping each ticker in the user's watchlist to
   a 1-2 sentence catch-up if there's notable news. Omit a ticker entirely
   if there's no material content (don't fabricate "no major news"
   placeholder text).

4. forward_events: bullet points (5 max) about upcoming earnings, Fed
   meetings, or regulatory deadlines visible in forward_results.

HARD RULES — output is rejected if any of these are violated:
- NO price targets. Do not predict where any ticker is going.
- NO buy/sell/hold recommendations. You synthesize context, not advice.
- NO claims that aren't supported by the input data. If you don't see it
  in the Tavily content, don't say it.
- NO speculation about future fundamentals beyond what the forward_results
  actually contain.

Output ONLY valid JSON, no preamble, no markdown fences.
```

---

## 3. Integrate into the Phase 4 weekly review email (~half evening)

In `jobs/weekly_review.py`, add a step before email composition:

```python
def run_weekly_review(settings, adapter, emailer, llm, tavily):
    # ... existing data gathering from Phase 4 ...

    # NEW: weekly market context (Phase 4.5)
    try:
        market_context = build_weekly_market_context(
            tavily=tavily, llm=llm,
            watchlist=settings.watchlist, week_of=week_start.date(),
        )
    except Exception as e:
        log.warning("weekly market context failed; omitting section: %s", e, exc_info=True)
        market_context = None

    review = WeeklyReview(
        ..., market_context=market_context,    # NEW field, optional
    )
    ...
```

Add `market_context: WeeklyMarketContext | None = None` to the `WeeklyReview` dataclass.

In `templates/weekly_review.html.j2`, add a new section between "next-Sunday preview" and "Moomoo parallel status":

```jinja
{% if review.market_context %}
<h2 style="color: #2a2a2a; margin-top: 30px;">Weekly Market Context</h2>

{% if review.market_context.macro_summary %}
<h3 style="margin-bottom: 4px;">Macro</h3>
<p>{{ review.market_context.macro_summary }}</p>
{% endif %}

{% if review.market_context.sector_summary %}
<h3 style="margin-bottom: 4px;">Sectors</h3>
<p>{{ review.market_context.sector_summary }}</p>
{% endif %}

{% if review.market_context.ticker_catchup %}
<h3 style="margin-bottom: 4px;">Ticker Catch-Up</h3>
<ul>
  {% for ticker, summary in review.market_context.ticker_catchup.items() %}
  <li><strong>{{ ticker }}:</strong> {{ summary }}</li>
  {% endfor %}
</ul>
{% endif %}

{% if review.market_context.forward_events %}
<h3 style="margin-bottom: 4px;">Next Week</h3>
<ul>
  {% for event in review.market_context.forward_events %}
  <li>{{ event }}</li>
  {% endfor %}
</ul>
{% endif %}

<h3 style="margin-bottom: 4px; font-size: 14px; color: #888;">Sources</h3>
<ol style="font-size: 12px;">
  {% for cite in review.market_context.citations[:10] %}
  <li><a href="{{ cite.url }}">{{ cite.title }}</a> ({{ cite.source_domain }})</li>
  {% endfor %}
</ol>
{% endif %}
```

Mirror the section in `templates/weekly_review.txt.j2` with plain-text formatting.

Wire `tavily` into the lifespan:

```python
# main.py
app.state.tavily = make_tavily_client(_settings)
```

And the cron call site (the existing Phase 4 `weekly_review` job) receives `tavily` alongside the other dependencies.

---

## 4. Smoke-test checklist (Phase 4.5 done when all green)

| # | Step | Pass criteria |
|---|---|---|
| 1 | `uv run pytest tests/test_tavily.py` | All wrapper tests pass — cost-cap enforcement, exception-to-empty-result fallback, `FakeTavilyClient` records calls correctly |
| 2 | `uv run pytest tests/test_weekly_context.py` | Synthesis service tests pass with `FakeTavilyClient` + mocked LLMClient — happy path, Tavily silent → None, LLM schema-error → empty sections + citations only |
| 3 | `uv run pytest` overall | Total ≥ 250 unit tests + 1 integration |
| 4 | Live Tavily search with paper API key | `python -c "from investor.services.tavily import make_tavily_client; from investor.config import Settings; c = make_tavily_client(Settings()); print(c.search_news('US Federal Reserve this week', days=7))"` returns ≥ 1 result |
| 5 | Tavily cost cap respected | Set `tavily_monthly_cap=0` in test settings; first `search_news` call returns `[]` with WARNING log |
| 6 | Synthesis HARD RULES enforced | Inject a Tavily result containing the phrase "price target of $X"; assert the synthesized `macro_summary` does not echo the price target. (Sonnet should obey the prompt; if it doesn't, harden the prompt and version-bump.) |
| 7 | Friday weekly review email contains the new section | Manually trigger `POST /admin/run-weekly-review`; received email has a "Weekly Market Context" section with at least one populated subsection |
| 8 | Email graceful degradation when Tavily fails | Set `TAVILY_API_KEY=""`; trigger weekly review; email omits the "Weekly Market Context" section entirely (no half-rendered ghost) |
| 9 | **Two consecutive Friday emails observed** with non-empty, sensible content | Read the synthesis; does it sound like market context an analyst would write? Or hallucinated? |

Tag:

```bash
git add -A
git commit -m "phase 4.5: tavily-driven weekly market context"
git tag v0.4.5.0
git push --tags
```

---

## 5. Common Phase 4.5 pitfalls

1. **Tavily API surface drift post-acquisition.** Tavily was acquired by Nebius in Feb 2026. Pin `tavily-python>=0.6,<0.7` and check release notes before bumping. The `topic` and `days` parameters are documented and stable as of plan time, but a major-version bump may reshape them. ADR-0020 documents the swap path to alternatives.
2. **Free-tier burn during dev iteration.** 1,000 searches/month is generous but a tight prompt-iteration loop can burn through it surprisingly fast. Use `FakeTavilyClient` with canned fixtures for unit-test loops; only hit real Tavily for the integration smoke test (row 4) and the weekly cron in production.
3. **Synthesis hallucination on thin input.** If Tavily returns 1-2 weak results for a sector, Sonnet will be tempted to pad the `sector_summary` with generic claims. The prompt's "if you don't see it in the Tavily content, don't say it" rule reduces but doesn't eliminate this. During the first 2-3 weekly emails, read the synthesis carefully and fold any drift into prompt-v2.
4. **Tavily content extraction sometimes returns paywalled snippets.** A WSJ result might give you "Subscribe to read more" instead of substantive content. The synthesis prompt's "claim must be in the Tavily content" rule handles this correctly — the model will simply omit unsupported claims. But it can make sector_summary thinner than expected. Note in citations even when the snippet is paywalled; let the user click through if interested.
5. **Same ticker in `ticker_catchup` and in Phase 3b's `news_event`.** The two pipelines may surface the same headline. This is fine — Tavily synthesizes a 1-2 sentence catch-up *for the week*, vs Phase 3b's same-day daily entry. They serve different time horizons. The Friday email shows both; the user sees them as complementary, not redundant.
6. **Cost-cap shared with `LLMClient` is fine.** Phase 3a's `LLMClient` has a separate daily cost cap; Tavily's monthly cap lives on `TavilyConcreteClient`. Don't try to merge them — they have different vendors, different units (Anthropic dollars vs Tavily searches), different rollover cadences.
7. **The Friday email must still send on full Tavily failure.** If `build_weekly_market_context` raises, `jobs/weekly_review.py` should catch and omit the section, not crash the email. Test row 8 enforces this.
8. **Acquisition-watch.** Nebius could integrate Tavily into a paid platform offering or sunset the free tier on short notice. Add a monthly calendar reminder to check Tavily's pricing/status page and to verify the swap path in ADR-0020 still works. Boring operational hygiene, easy to skip, expensive to skip wrong.

---

## 6. ADRs to write in Phase 4.5

- **`docs/adr/0020-tavily-weekly-context.md`** — new. Why Tavily was chosen for weekly macro/sector context (LLM-optimized search, dedicated `news` and `finance` topics, generous free tier, established Python SDK). Why the integration is behind a Protocol (post-Nebius-acquisition durability risk; clean swap path). Alternatives considered: Serper ($0.30/1k cheaper but general web, no finance topic), Brave Search API (similar generality, less LLM-tuned), Perplexity Sonar API (richer answers but ~10× cost for our use case). The decision rule: re-evaluate alternatives every 6 months or any time Tavily pricing changes ≥ 20%.

One new ADR. About 30 minutes.

---

## 7. Documentation drift to fix

- **CLAUDE.md** — add to "Things to never do": "Never feed Tavily results directly into the suggestion engine or order-execution path — Tavily is informational/contextual only. The synthesis prompt's HARD RULES block all leakage into actionable decisions." Add to common gotchas: Tavily acquisition by Nebius (Feb 2026); the SDK is at 0.x and pre-1.0 churn risk is real — pin tightly. Add `services/tavily.py`, `services/weekly_context.py`, `prompts/weekly_context_v1.txt` to repo layout. Update env vars to include `TAVILY_API_KEY`, `TAVILY_MONTHLY_CAP`.
- **`product_plan.md`** — already updated to add Phase 4.5 in the prior edit. When the phase ships, mark it complete with the standard "code-complete / tag deferred until 2 observed weekly emails" pattern.
- **ADR index** — add entry for 0020.

---

## 8. What Phase 4.5 deliberately does not include

- **Tavily-driven suggestion generation.** The synthesis is informational; suggestions still come from Phase 3c's review graph only. Letting web-search hits influence suggestion logic is regulated-advice territory and outside the project's product principles.
- **Per-user Tavily API keys.** Phase 5 multi-tenant work; not here.
- **Intra-day Tavily queries.** Phase 4.5 is weekly. If you ever want real-time news (e.g., during a 5% mover), that's Phase 6 territory and worth a separate ADR — the rate-limit and cost implications are different.
- **Persisted Tavily results.** Results are fetched, synthesized, rendered into the Friday email, then discarded. The synthesis itself is preserved in the email; the raw results aren't in `news_event` or any new table. If you later want retrospective queries on what Tavily said in week N, add a `tavily_query_log` table — but that's a future enhancement, not Phase 4.5.

---

*When all 9 smoke-test rows are green and you've received two consecutive Friday emails with the new "Weekly Market Context" section reading as sensible market commentary (not hallucination, not recommendations), Phase 4.5 is done. Tag `v0.4.5.0`. Then continue with the Phase 4 auto-trade soak progression (`v0.4.1` → `v0.4.4`) on its independent calendar timeline.*
