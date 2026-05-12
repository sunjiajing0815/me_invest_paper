# Phase 3b — News Triage via LangGraph: Step-by-Step Guide

**Goal:** Introduce LangGraph to the codebase via the news triage workflow. Daily 16:30 ET cron pulls news for any watchlist ticker that moved ≥ 5 % vs. last week's close, runs Haiku 4.5 classification, runs a Haiku critic over the classifier's output, and conditionally escalates flagged items to Sonnet 4.6 for arbitration. The final classifications drive a movers email — but only on days when something actually moved.

**Out of scope for 3b:** suggestion review pipeline (Phase 3c), Moomoo adapter (Phase 4), any use of news content inside the suggestion engine (the news triage results are *informational* in 3b; they only feed back into suggestion logic in 3c via the suggestion-review graph).

**Time budget:** 5–7 evenings (~1 week).

**Definition of done:** all 10 smoke-test rows pass, *and* you've received one movers email after a real ≥ 5 % weekly mover where the email shows at least one critic-corrected classification (verifiable by inspecting `news_event` + graph checkpoints).

**Depends on:** Phase 3a (`v0.3a.0`) tagged. The `services/llm.py` wrapper from 3a is the basis for every LangGraph node here.

---

## Architecture context — what's new in Phase 3b

Phase 3b introduces LangGraph as the orchestration framework for multi-step LLM reasoning. Two new dependencies (`langgraph`, `langgraph-checkpoint-sqlite`), two new services (`graphs/news_triage.py`, `services/news.py`), one new table (`news_event`), one new cron job (`jobs/movers.py`).

```
                      ┌─────────────────────────┐
                      │ Anthropic API           │
                      │ • Haiku 4.5 (bulk +     │
                      │   cheap critic)         │
                      │ • Sonnet 4.6 (only      │
                      │   flagged items)        │
                      └────────────┬────────────┘
                                   │
                      ┌────────────▼────────────┐
                      │ services/llm.py (3a)    │
                      └────────────┬────────────┘
                                   │ called by each node
                                   ▼
        ┌────────────────────────────────────────────────────┐
        │ graphs/news_triage.py                              │
        │   START ─▶ classify (Haiku) ─▶ critic (Haiku) ─▶   │
        │       (cond) ─▶ arbitrate (Sonnet) ─▶ END          │
        │       (or skip arbitrate ─▶ END)                   │
        │                                                    │
        │ State checkpointed to SQLite via SqliteSaver       │
        └────────────────────────────────────────────────────┘
                                   │
                                   ▼
                      ┌─────────────────────────┐
                      │ news_event table        │
                      │ + email if movers exist │
                      └─────────────────────────┘
```

Three behaviours to internalize:

1. **LangGraph is used here because a critic step materially catches failures** a single-shot prompt misses — over-classified analyst noise, sentiment/summary mismatches, hallucinated claims beyond the headline. The decision rule (when to use a graph vs. a single call) is captured in ADR-0012 written here.
2. **Haiku does the bulk classify + cheap critic; Sonnet only sees flagged items.** Typical movers day: ~5–15 headlines per moved ticker, 2–4 moved tickers. Cost per movers email: ~$0.10. Sonnet enters the graph only when the critic flags ≥ 1 item.
3. **State is persisted via `SqliteSaver` in the same `data/investor.db` file as OLTP.** Every graph run leaves a queryable reasoning trace. `langgraph dev --thread-id news-AAPL-2026-05-20` replays it. The pitfalls section flags the cost: never `session.commit()` inside a graph node — checkpointer and OLTP engine share the SQLite write lock.

---

## 0. Pre-flight checklist (~20 minutes)

- [ ] Phase 3a (`v0.3a.0`) **tagged**. Per the 3a completion report, the tag is pending until the first real Sunday email shows `(conf X.XX)` in the reason column and `llm_call_log` rows have `status='ok'`. **Do not start 3b code before that tag is pushed** — if the Sunday email reveals a 3a problem, the fix belongs in 3a, not piled on top of 3b changes.
- [ ] **Finnhub API key.** Sign up at `https://finnhub.io` (free tier, 60 req/min). Add `FINNHUB_API_KEY=` to `.env` and `.env.example`.
- [ ] **LangGraph dependencies installed:** `uv add 'langgraph>=0.2,<0.3' 'langchain-anthropic>=0.3,<0.4' 'langgraph-checkpoint-sqlite>=2.0,<3.0'`. Pin minor versions — the LangChain ecosystem has historically been version-churn-prone.
- [ ] **Bump `llm_daily_cost_cap_usd` to $3.00–$5.00** in `.env`. Phase 3a defaulted this to $1.00 which is fine for one weekly Sonnet pass per ticker. Phase 3b adds daily news triage which can 2–3× daily LLM call volume; the $1 cap will silently trip and degrade behaviour. Recalibrate after the first week of 3b in production.
- [ ] **Confirm Phase 3a infrastructure is in place** (you'll reuse, not rebuild): `services/llm.py` exports `LLMClient`, `HAIKU`, `SONNET`, `_strip_fences()`, `persist_llm_call_log()`. All graph nodes here use these primitives — don't recreate any of them.
- [ ] **Verify Phase 3a is still healthy:** `curl -H "X-Admin-Token: …" -X POST localhost:8000/admin/run-weekly-suggestions` returns 200; new `llm_call_log` entries appear with `status='ok'`.

---

## 1. LangGraph setup (~1 evening)

### 1a. `graphs/__init__.py` — shared checkpointer

```python
# src/investor/graphs/__init__.py
from langgraph.checkpoint.sqlite import SqliteSaver

# Shared checkpointer so every graph run is resumable / inspectable.
# Lives in data/investor.db alongside OLTP tables.
CHECKPOINTER = SqliteSaver.from_conn_string("data/investor.db")
```

### 1b. When to use a graph vs. a single call (anchor for ADR-0012)

| Decision rule | Example | Choice |
|---|---|---|
| Input is structured, output is structured classification, no judgment between steps | Level scoring (Phase 3a) | Single call |
| Output benefits from a critic that checks for hallucination, schema-violating claims, or self-consistency | News classification (this phase) | Graph with critic |
| Reasoning combines multiple sources and the final output must respect product principles | Suggestion review (Phase 3c) | Graph with reasoner + critic + revise |
| You need conditional routing, retry-with-different-prompt, or human-in-the-loop | n/a in Phase 3 | Graph |

Rule of thumb: **if you'd write a Python `if` to decide whether to run another LLM call, that's a graph edge.** If it's one in / one out, it's a single call.

### 1c. Shared node helper

```python
# src/investor/graphs/_nodes.py
from typing import TypeVar
from pydantic import BaseModel, ValidationError
from investor.services.llm import LLMClient, _strip_fences, persist_llm_call_log

T = TypeVar("T", bound=BaseModel)

def llm_node_call(
    *, purpose: str, model: str, system: str, user: str,
    schema: type[T], fallback_factory,
    llm: LLMClient, max_tokens: int = 4096,
) -> tuple[T | object, dict]:
    """Generic node helper. Returns (parsed_or_fallback, telemetry).

    Note: max_tokens default is 4096, not 2048. Phase 3a Bug 1 showed that
    1500 was already too small for a per-ticker level array; per-ticker
    news batches (5–15 headlines) are larger payloads. Bump higher for
    arbitrate nodes if you see truncation.
    """
    resp, parsed = llm.call(
        model=model, system=system, user=user,
        max_tokens=max_tokens, response_schema=schema,
    )
    # Phase 3a Bug 3 lesson: capture the actual Pydantic parse error
    # in llm_call_log.error, not the raw (fenced) content.
    error_msg: str | None = None
    if parsed is None:
        try:
            schema.model_validate_json(_strip_fences(resp.content))
        except ValidationError as e:
            error_msg = str(e)[:1000]
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"[:1000]

    persist_llm_call_log(
        resp, purpose=purpose,
        status="ok" if parsed else "schema_error",
        error=error_msg,
    )
    telemetry = {
        f"{purpose}_model": resp.model,
        f"{purpose}_cost_usd": resp.cost_usd,
        f"{purpose}_status": "ok" if parsed else "schema_error",
    }
    return (parsed if parsed else fallback_factory()), telemetry
```

Three patterns to internalize, all derived from Phase 3a production lessons:

- **`_strip_fences` is mandatory, not optional.** Sonnet and Haiku both wrap JSON output in ` ```json ... ``` ` fences regardless of prompt instructions. The `LLMClient.call()` already calls `_strip_fences` internally before schema validation, so `parsed` is correct — but the diagnostic re-parse used to populate `llm_call_log.error` must also strip fences, otherwise the error column logs the raw content rather than the actual Pydantic failure (this was Bug 3 in 3a).
- **Default `max_tokens=4096`, not 2048.** Phase 3a's initial `max_tokens=1500` truncated every Sonnet response (Bug 1). News classify and arbitrate nodes have larger payloads than level scoring — bump to 8192 if you see truncation on tickers with many headlines.
- **The helper carries `purpose` through.** Phase 3b purposes will be `news_classify`, `news_critic`, `news_arbitrate`. The `llm_call_log` purpose column already supports this from 3a — no schema change needed.

### 1d. Graph state inspection (dev workflow)

```bash
# show graph runs from today
sqlite3 data/investor.db "
  SELECT thread_id, checkpoint_id, type
  FROM checkpoints
  WHERE created_at > date('now') ORDER BY created_at DESC LIMIT 20"

# replay a specific run with the LangGraph CLI
uv run langgraph dev --thread-id <id>
```

Treat graph checkpoints like git history — never edit, always inspect to understand what happened. `news_event` shows *final* outputs; checkpoints show *how the graph got there*.

---

## 2. `news_event` table + Alembic (~30 min)

```python
class NewsEvent(Base):
    __tablename__ = "news_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    ticker: Mapped[str]
    published_at: Mapped[datetime]
    source: Mapped[str]                                # "alpaca" | "finnhub"
    headline: Mapped[str]
    url: Mapped[str]
    url_hash: Mapped[str]                              # sha256(normalised url)[:16]
    llm_material: Mapped[bool | None] = mapped_column(default=None)
    llm_sentiment: Mapped[str | None] = mapped_column(default=None)
    llm_summary: Mapped[str | None] = mapped_column(default=None)
    llm_model: Mapped[str | None] = mapped_column(default=None)
    llm_cost_usd: Mapped[float | None] = mapped_column(default=None)
    arbitrated: Mapped[bool] = mapped_column(default=False)
    __table_args__ = (UniqueConstraint("url_hash", name="uq_news_url_hash"),)
```

`url_hash` unique constraint prevents storing the same article twice. `arbitrated=True` marks items the critic flagged and Sonnet re-evaluated — useful for retrospectives ("did arbitration change the outcome?").

`uv run alembic revision --autogenerate -m "phase3b news_event"` then `upgrade head`.

---

## 3. `services/news.py` — Alpaca + Finnhub fetcher (~2–3 hours)

```python
@dataclass(frozen=True)
class NewsRaw:
    ticker: str
    headline: str
    snippet: str                    # first paragraph or summary
    url: str
    url_hash: str
    published_at: datetime
    source: Literal["alpaca", "finnhub"]


def _normalise_url(url: str) -> str:
    """Strip tracking query params and lowercase host. ADR-0011."""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path, "", "", ""))


def _hash_url(url: str) -> str:
    return hashlib.sha256(_normalise_url(url).encode()).hexdigest()[:16]


def fetch_alpaca_news(ticker: str, since: datetime) -> list[NewsRaw]:
    # ... use alpaca-py NewsClient
    return [NewsRaw(ticker=ticker, headline=a.headline, snippet=a.summary[:400],
                    url=a.url, url_hash=_hash_url(a.url),
                    published_at=a.created_at, source="alpaca")
            for a in articles]


def fetch_finnhub_news(ticker: str, since: datetime) -> list[NewsRaw]:
    # ... finnhub-python /company-news endpoint
    return [NewsRaw(...) for a in articles]


def get_news_for_movers(tickers: list[str], since: datetime) -> dict[str, list[NewsRaw]]:
    """Alpaca primary; Finnhub fallback when Alpaca returns empty or errors."""
    out: dict[str, list[NewsRaw]] = {}
    for t in tickers:
        try:
            items = fetch_alpaca_news(t, since)
        except Exception as e:
            log.warning("alpaca news failed for %s: %s", t, e)
            items = []
        if not items:
            try:
                items = fetch_finnhub_news(t, since)
            except Exception as e:
                log.warning("finnhub news failed for %s: %s", t, e)
        out[t] = items
    return out
```

URL normalization (strip query strings, lowercase host) prevents Alpaca + Finnhub from inserting near-duplicates of the same Benzinga article with different `?utm_source=` query params.

---

## 4. `graphs/news_triage.py` — the graph (~2 evenings)

### 4a. State shape

```python
class NewsTriageState(TypedDict):
    ticker: str
    raws: list[NewsRaw]                          # input
    classifications: list[NewsTriageItem]        # after classify node
    flagged: list[str]                           # url_hashes the critic wants re-evaluated
    final: list[NewsTriageItem]                  # after arbitration (or copy of classifications)
    telemetry: dict


class NewsTriageItem(BaseModel):
    url_hash: str
    is_material: bool
    sentiment: Literal["bullish", "bearish", "neutral"] | None
    summary: str


class NewsTriageBatch(BaseModel):
    items: list[NewsTriageItem]


class NewsCriticReview(BaseModel):
    flagged: list[str]
```

### 4b. Classify node (Haiku, batch)

```python
MAX_HEADLINES_PER_BATCH = 20    # analog of Phase 3a's 30% level pre-filter

def classify_node(state: NewsTriageState, llm: LLMClient) -> NewsTriageState:
    system = load_prompt("news_classify_v1.txt")
    # Cap to MAX_HEADLINES_PER_BATCH most-recent first; remaining raws are
    # persisted as news_event with null LLM fields. Keeps output tokens
    # bounded on busy news days.
    capped_raws = sorted(state["raws"], key=lambda r: r.published_at, reverse=True)[:MAX_HEADLINES_PER_BATCH]
    user = json.dumps({
        "ticker": state["ticker"],
        "headlines": [
            {"url_hash": r.url_hash, "headline": r.headline, "snippet": r.snippet[:400]}
            for r in capped_raws
        ],
    })
    items, tel = llm_node_call(
        purpose="news_classify", model=HAIKU, system=system, user=user,
        schema=NewsTriageBatch, fallback_factory=lambda: NewsTriageBatch(items=[]),
        llm=llm, max_tokens=4096,           # bump to 8192 if truncation seen
    )
    # Cap raws to 20 headlines per ticker before sending — keeps output
    # bounded even when a ticker has a busy news day (analog of the 30%
    # level pre-filter from Phase 3a).
    return {**state, "classifications": items.items,
            "telemetry": {**state["telemetry"], **tel}}
```

### 4c. Critic node (Haiku)

```python
def critic_node(state: NewsTriageState, llm: LLMClient) -> NewsTriageState:
    """Reads classifier output alongside originals. Flags problems."""
    system = load_prompt("news_critic_v1.txt")
    user = json.dumps({
        "ticker": state["ticker"],
        "pairs": [
            {
                "url_hash": cls.url_hash,
                "headline": next(r.headline for r in state["raws"] if r.url_hash == cls.url_hash),
                "snippet":  next(r.snippet for r in state["raws"] if r.url_hash == cls.url_hash)[:400],
                "classifier_output": cls.model_dump(),
            }
            for cls in state["classifications"]
        ],
    })
    review, tel = llm_node_call(
        purpose="news_critic", model=HAIKU, system=system, user=user,
        schema=NewsCriticReview, fallback_factory=lambda: NewsCriticReview(flagged=[]),
        llm=llm, max_tokens=2048,           # output is a small list of url_hashes
    )
    return {**state, "flagged": review.flagged,
            "telemetry": {**state["telemetry"], **tel}}
```

### 4d. Arbitrate node (Sonnet, only flagged)

```python
def arbitrate_node(state: NewsTriageState, llm: LLMClient) -> NewsTriageState:
    """Sonnet re-evaluates only flagged items with full attention."""
    raw_by_hash = {r.url_hash: r for r in state["raws"]}
    flagged_pairs = [
        {
            "url_hash": h,
            "headline": raw_by_hash[h].headline,
            "snippet": raw_by_hash[h].snippet[:400],
            "previous_classification": next(
                c.model_dump() for c in state["classifications"] if c.url_hash == h
            ),
        }
        for h in state["flagged"]
    ]
    system = load_prompt("news_arbitrate_v1.txt")
    user = json.dumps({"ticker": state["ticker"], "items": flagged_pairs})
    revised, tel = llm_node_call(
        purpose="news_arbitrate", model=SONNET, system=system, user=user,
        schema=NewsTriageBatch, fallback_factory=lambda: NewsTriageBatch(items=[]),
        llm=llm, max_tokens=4096,
    )
    revised_by_hash = {r.url_hash: r for r in revised.items}
    final = [revised_by_hash.get(c.url_hash, c) for c in state["classifications"]]
    return {**state, "final": final,
            "telemetry": {**state["telemetry"], **tel}}
```

### 4e. Conditional edge

```python
def route_after_critic(state: NewsTriageState) -> Literal["arbitrate", "no_arbitrate"]:
    return "arbitrate" if state["flagged"] else "no_arbitrate"


def copy_to_final(state: NewsTriageState) -> NewsTriageState:
    return {**state, "final": list(state["classifications"])}
```

### 4f. Graph assembly

```python
def build_news_triage_graph(llm: LLMClient):
    g = StateGraph(NewsTriageState)
    g.add_node("classify",      lambda s: classify_node(s, llm))
    g.add_node("critic",        lambda s: critic_node(s, llm))
    g.add_node("arbitrate",     lambda s: arbitrate_node(s, llm))
    g.add_node("no_arbitrate",  copy_to_final)
    g.add_edge(START, "classify")
    g.add_edge("classify", "critic")
    g.add_conditional_edges("critic", route_after_critic,
                            {"arbitrate": "arbitrate", "no_arbitrate": "no_arbitrate"})
    g.add_edge("arbitrate", END)
    g.add_edge("no_arbitrate", END)
    return g.compile(checkpointer=CHECKPOINTER)
```

---

## 5. Prompts (~1 evening to iterate)

### 5a. `prompts/news_classify_v1.txt`

```
You are a financial news classifier for a long-term US-equity investor.

For each headline + snippet, return:
- is_material: true if the news could materially affect company fundamentals
  (earnings, guidance, M&A, regulation, key personnel, legal action, product
  launches, supply chain). False for analyst rating noise, generic market
  commentary, listicles.
- sentiment: bullish / bearish / neutral. Null if is_material is false.
- summary: ONE sentence, factual, no opinions. Max 25 words.

Hard rules:
- No price targets in summary or sentiment.
- No buy/sell recommendations.
- No claims that go beyond the headline + snippet.
- Reference only entities mentioned in the input.

Output: ONLY valid JSON, no preamble, no markdown fences, no ```json wrapper.
{"items": [{"url_hash":"<hash>","is_material":<bool>,"sentiment":<str|null>,"summary":"<str>"}, ...]}
```

Note the explicit "no markdown fences" instruction. Phase 3a Bug 2 showed Sonnet wraps JSON in ` ```json ... ``` ` regardless — Haiku does the same. The `_strip_fences` defence in `llm.py` is what actually saves you; the prompt instruction reduces the rate but doesn't eliminate it.

### 5b. `prompts/news_critic_v1.txt`

```
You are an editor reviewing a financial-news classifier's output for a
long-term US-equity investor.

For each (headline, snippet, classifier_output) triple, decide if the
classification has a problem. Common problems:
- is_material=true on something that's clearly analyst noise, listicle,
  generic market commentary, or "price target raised" articles.
- sentiment is bullish/bearish but the headline+snippet don't support it.
- summary contains a price target, a "buy/sell" recommendation, or a
  claim about company fundamentals that isn't in the input.
- summary references entities not in the input.

Return ONLY the url_hashes of items that need re-evaluation. Output JSON only, no preamble, no markdown fences:
{"flagged": ["<url_hash>", ...]}

If everything looks fine, return {"flagged": []}.
```

### 5c. `prompts/news_arbitrate_v1.txt`

Like `news_classify_v1.txt` but framed for Sonnet — deeper read, asked to make the careful final call on items the critic flagged. Same JSON output shape so `NewsTriageBatch` schema validates either way.

---

## 6. `jobs/movers.py` — invoke graph + persist + email (~1 evening)

```python
def run_movers_email(settings, adapter, emailer, llm):
    # 1. compute weekly movers via DuckDB
    with duckdb_conn() as con:
        movers = con.execute("""
            WITH today AS (
                SELECT ticker, close AS today_close
                FROM price_bar
                WHERE date = (SELECT MAX(date) FROM price_bar)
            ),
            last_week AS (
                SELECT ticker, close AS last_week_close
                FROM price_bar
                WHERE date = (
                    SELECT MAX(date) FROM price_bar
                    WHERE date <= (SELECT MAX(date) FROM price_bar) - INTERVAL 7 DAYS
                )
            )
            SELECT today.ticker, today_close, last_week_close,
                   (today_close / last_week_close - 1) * 100 AS pct_change
            FROM today JOIN last_week USING (ticker)
            WHERE ABS(pct_change) >= 5
        """).df()

    if movers.empty:
        log.info("no movers today; skipping email")
        return

    # 2. fetch news + triage via graph
    since = datetime.now(UTC) - timedelta(hours=24)
    news_by_ticker = get_news_for_movers(movers["ticker"].tolist(), since)
    graph = build_news_triage_graph(llm)

    final_by_ticker: dict[str, list[NewsTriageItem]] = {}
    arbitrated_hashes: set[str] = set()
    for t, raws in news_by_ticker.items():
        if not raws:
            final_by_ticker[t] = []
            continue
        result = graph.invoke(
            {"ticker": t, "raws": raws, "classifications": [], "flagged": [],
             "final": [], "telemetry": {}},
            config={"configurable": {"thread_id": f"news-{t}-{date.today()}"}},
        )
        final_by_ticker[t] = result["final"]
        arbitrated_hashes.update(result["flagged"])

    # 3. persist (uses result["final"])
    with session_scope() as s:
        for t, raws in news_by_ticker.items():
            final_map = {it.url_hash: it for it in final_by_ticker[t]}
            for r in raws:
                f = final_map.get(r.url_hash)
                s.merge(NewsEvent(
                    ticker=t, published_at=r.published_at, source=r.source,
                    headline=r.headline, url=r.url, url_hash=r.url_hash,
                    llm_material=f.is_material if f else None,
                    llm_sentiment=f.sentiment if f else None,
                    llm_summary=f.summary if f else None,
                    llm_model=SONNET if r.url_hash in arbitrated_hashes else HAIKU,
                    arbitrated=r.url_hash in arbitrated_hashes,
                ))
        s.commit()

    # 4. email — only material headlines surface
    payload = build_movers_payload(movers, final_by_ticker)
    html = render_template("movers.html.j2", payload=payload)
    text = render_template("movers.txt.j2", payload=payload)
    emailer.send(to=settings.email_to,
                 subject=f"Movers — {date.today():%Y-%m-%d}",
                 html=html, text=text)
```

Cron:

```python
sched.add_job(
    run_movers_email,
    trigger=CronTrigger(day_of_week="mon-fri", hour=16, minute=30,
                       timezone="America/New_York"),
    id="movers", misfire_grace_time=60 * 60,
)
```

16:30 ET runs after the 16:15 daily report so bars are fresh.

Email template (`templates/movers.html.j2`): one card per moved ticker — pct_change, today's close, last week's close, top 3 material headlines with LLM summary. Tickers with no material news still show a card so "AAPL moved 6% but no news explaining it" is itself surfaced as information.

---

## 7. Smoke-test checklist (Phase 3b done when all green)

| # | Step | Pass criteria |
|---|---|---|
| 1 | `uv run alembic upgrade head` | `news_event` table exists with `UniqueConstraint(url_hash)` |
| 2 | `uv run pytest -m "not integration"` | All tests pass; total now ≥ 72; per-node and graph-integration tests included |
| 3 | URL normalisation test | `_normalise_url("https://x.com/a?utm=1")` == `_normalise_url("HTTPS://X.com/a")` |
| 4 | Manual: stub a moved ticker → `curl -H "X-Admin-Token: …" -X POST /admin/run-movers` (add this endpoint) | 200; `news_event` rows inserted; `llm_call_log` has `news_classify` + `news_critic` rows |
| 5 | Inspect graph checkpoint | `sqlite3 data/investor.db "SELECT * FROM checkpoints WHERE thread_id LIKE 'news-%' ORDER BY created_at DESC LIMIT 5"` returns rows |
| 6 | Trigger a critic-flagged scenario | Inject a synthetic headline that's clearly analyst noise but with `is_material=true` from the classifier — critic flags it, Sonnet arbitrates, final classification differs from initial |
| 7 | `sqlite3 ... "SELECT arbitrated, COUNT(*) FROM news_event GROUP BY arbitrated"` after a real run | Some rows with `arbitrated=true` (when critic flagged) |
| 8 | LLM cost guard: set `daily_cost_cap_usd=0.001` and invoke graph | Engine logs "cap reached"; persistence falls back to raw headlines with null LLM fields |
| 9 | No-movers day | No email sent; `llm_call_log` has no `news_classify` row from that day |
| 10 | One real movers email observed | At least one card; at least one material headline summarised; at least one `arbitrated=true` row in `news_event` across the soak period |
| 11 | Markdown-fence handling (regression for Phase 3a Bug 2) | Unit test injects a fake LLM response wrapped in ` ```json {…} ``` `; `LLMClient.call` strips fences and returns a successfully parsed pydantic object; `llm_call_log` row shows `status='ok'` not `schema_error` |
| 12 | `max_tokens` truncation diagnostic | When a node returns `parsed=None` and `resp.output_tokens == max_tokens`, the operator can tell from `llm_call_log.error` that truncation happened (Pydantic error about unexpected end of JSON), not just from raw fenced content |

Tag and push:

```bash
git add -A
git commit -m "phase 3b: news triage via LangGraph"
git tag v0.3b.0
git push --tags
```

---

## 8. Common Phase 3b pitfalls

1. **LangChain ecosystem version churn.** Pin `langgraph`, `langchain-core`, `langchain-anthropic`, `langgraph-checkpoint-sqlite` to specific minor versions in `pyproject.toml`. Upgrade deliberately. The graph-integration test in row 6 is the canary — run after any LangGraph upgrade before merging.
2. **Graph state mutability.** LangGraph nodes return *new* state dicts; never mutate `state` in place. The `{**state, "new_key": value}` pattern is the idiom. In-place mutation works in unit tests but breaks checkpointing.
3. **Checkpointer file lock.** `SqliteSaver` opens its own connection to `data/investor.db` separate from the SQLAlchemy engine. Never `session.commit()` inside a graph node — they share the SQLite write lock. Pattern in §6: persist outside the graph, after `graph.invoke()` returns.
4. **News dedup across sources.** Alpaca and Finnhub sometimes carry the same Benzinga article with different `?utm_source=` query params. URL normalization (`_normalise_url`) is what catches this. Test row 3 protects.
5. **Movers on holidays.** ≥ 5 % vs. last-week-close on the Monday after a 3-day weekend can be noisy because "last week" lands inside the holiday window. Acceptable for now; document.
6. **Sentiment leak into action.** The product principle is suggest-only, and LLM sentiment ratings can subtly influence the suggestion engine if you wire them in. In Phase 3b they are *informational only* for the user — Phase 3c will be the formal place to allow news context to influence suggestion review, and only via the critic.
7. **Thread-id collisions.** `thread_id` uniquely identifies a graph run for checkpointing. Use `f"news-{ticker}-{date}"` so re-running the same day doesn't collide and overwrite the previous trace.
8. **Critic over-flagging.** If the critic flags every item, you're paying for Sonnet on the full set and the critic is adding no signal. Calibrate during the first week — the critic should flag 10–30 % of items. Below 5 % means the critic is rubber-stamping; above 50 % means it's too strict.
9. **Free-tier rate limits.** Finnhub free tier is 60 req/min; Alpaca News has its own limits. For ~6 tickers per day this is fine; if you scale watchlist past 30, add a small `time.sleep(0.3)` between requests.
10. **`news_event` retention.** Headlines pile up. After 6 months, prune `llm_material=false` entries. Keep material ones forever for retrospectives.
11. **Markdown fences around JSON output — by both Haiku and Sonnet.** Phase 3a Bug 2 caught this for Sonnet level scoring; Haiku does the same thing in news classification. The `_strip_fences` helper in `services/llm.py` (already shipped in 3a) is the universal fix; every graph node uses `llm_node_call` which calls into `LLMClient.call()` which calls `_strip_fences` internally. **Do not bypass the helper** by calling `client.messages.create` directly from a node — you'll re-introduce the bug. Test row 11 below explicitly covers this.
12. **`max_tokens` truncation — start higher than you think.** Phase 3a Bug 1 was a `max_tokens=1500` cap that silently truncated every Sonnet response, producing schema_error logs with no obvious cause. Classify and arbitrate nodes default to `max_tokens=4096`; bump to 8192 if you see schema_error logs where `output_tokens` equals `max_tokens` exactly.
13. **`llm_call_log.error` should reflect parse failure, not raw content.** Phase 3a Bug 3 was logging the raw fenced response in `error`, making logs unreadable. The `llm_node_call` helper above captures the actual Pydantic ValidationError. If you write a custom node that bypasses the helper, replicate this pattern.
14. **No retry on transient API errors.** Phase 3a documented this as a known limitation. In 3b it matters more because news triage runs daily — a single Anthropic 529 (overloaded) means no movers email that day. Accept silent degradation for now; Phase 3c may add retry-with-backoff if it becomes a real frequency. Today's choice is: graceful empty result rather than blocking the cron.
15. **Daily cost cap calibration.** Phase 3a defaulted `llm_daily_cost_cap_usd=1.00` which was correct for one weekly Sonnet pass. Phase 3b adds 2–3× call volume on movers days. If you see the cap trip silently (suggestions still generated but `llm_call_log` shows the day's last few calls as `api_error` with "cap reached"), bump the cap.

---

## 9. ADRs to write in Phase 3b

- **`docs/adr/0011-news-source-priority.md`** — new. Alpaca News primary, Finnhub fallback. Why both. URL normalization for dedup. Retention policy (prune `llm_material=false` after 6 months; keep material ones).
- **`docs/adr/0012-langgraph-adoption.md`** — new. **The most important ADR in 3b.** Anchors the LangGraph-or-not decision rule from §1b — every future LLM workflow proposal gets evaluated against this. Documents the `SqliteSaver` checkpointing convention, version-pinning policy, and the critic-flagging-rate calibration target (10–30 %).

Two new ADRs. About 60 minutes.

---

## 10. Documentation drift to fix

- **CLAUDE.md** — add to "Things to never do": "Never mutate LangGraph node state in place — always return a new dict via `{**state, ...}`." "Never interleave SQLAlchemy `session.commit()` inside a LangGraph node — the SqliteSaver checkpointer and the OLTP engine share the same DB file." Add new entries under common gotchas: LangChain version pinning; news URL normalization. Update env vars to include `FINNHUB_API_KEY`. Add `graphs/` and `prompts/` to repo layout.
- **`product_plan.md`** — when 3b ships, mark it complete in §6.

---

*When all 10 smoke-test rows are green, you've received one movers email after a real ≥ 5 % weekly mover with at least one `arbitrated=true` row in `news_event`, and ADRs 0011 and 0012 are committed, Phase 3b is done. Tag `v0.3b.0` and start Phase 3c.*
