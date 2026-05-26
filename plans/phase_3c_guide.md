# Phase 3c — Suggestion Review via LangGraph: Step-by-Step Guide

**Goal:** Close Phase 3 by adding the second LangGraph workflow — the **suggestion review pipeline**. After Phase 2's mechanical engine produces draft suggestions with Phase 3a's confidence-weighted anchors, Phase 3c routes them through a graph: Sonnet writes a multi-sentence rationale for each draft considering full context (gap, scored levels, recent material news from 3b, indicators, account state, untracked positions), then Sonnet critiques all drafts as a *set* looking for cross-suggestion problems, then a deterministic Python `revise` node applies the critic's structured suggestions, then approved/revised drafts get persisted and emailed. Rationales in the weekly email shift from mechanical single-line strings to thoughtful 2–4 sentence explanations.

**Out of scope for 3c:** Auto-rebalancing based on critic verdicts. Multi-day suggestion validity. Moomoo adapter (Phase 4). News-driven suggestion *generation* (news only enters via the critic, not the proposer).

**Time budget:** 7–10 evenings (~1.5 weeks).

**Definition of done:** all 10 smoke-test rows pass, *and* you've received one Sunday weekly suggestions email where rationales are 2–4 sentences each, *and* the critic visibly rejected or revised at least one draft (verifiable in graph checkpoints), *and* the email's suggestions feel like a thoughtful analyst wrote them — not the mechanical Phase 2 or 3a output.

**Depends on:** Phase 3a (`v0.3a.0`) for LLM-scored levels and Phase 3b (`v0.3b.0`) for `news_event` content. Without 3b the critic has no news context to reason against; without 3a there are no confidence scores to feed the rationale step.

---

## Architecture context — what's new in Phase 3c

Phase 3c adds a second LangGraph workflow alongside the news triage graph from 3b. It does not add new tables — it reuses `order_suggestion` (with the columns already added in 3a) and reads from `sr_level`, `news_event`, and the gap engine.

```
                                  ┌─────────────────────────┐
                                  │ services/suggest.py     │
                                  │ generate_suggestions()  │  produces drafts
                                  │ (Phase 2 + 3a anchors)  │  ───────────────┐
                                  └─────────────────────────┘                  │
                                                                               ▼
            ┌────────────────────────────────────────────────────────────────────┐
            │ graphs/suggestion_review.py                                        │
            │                                                                    │
            │   START                                                            │
            │     │                                                              │
            │     ▼                                                              │
            │   gather_context  ──── reads gap, sr_level (with confidence),      │
            │     │                  news_event (last 7 days, material only),    │
            │     │                  indicators, account, untracked positions    │
            │     ▼                                                              │
            │   reason_about_drafts  (Sonnet, one bundled call)                  │
            │     │                                                              │
            │     ▼                                                              │
            │   critic              (Sonnet, reviews drafts as a SET)            │
            │     │                                                              │
            │     ▼                                                              │
            │   route_after_critic                                               │
            │     │                                                              │
            │     ├─ all approved ──▶ skip_revise ──▶ finalize ──▶ END           │
            │     └─ some revise/   ──▶ revise (Python only) ──▶ finalize ─▶ END │
            │        reject                                                      │
            └────────────────────────────────────────────────────────────────────┘
                                            │
                                            ▼ approved + revised
                                  ┌─────────────────────────┐
                                  │ order_suggestion table  │
                                  │ + weekly email          │
                                  └─────────────────────────┘
```

Three behaviours to internalize:

1. **The critic reviews drafts as a set, not individually.** Cross-suggestion problems — combined cash-floor violations, two suggestions on the same ticker, contradictory directions — are invisible to single-shot prompts. The critic step is where this graph earns its weight.
2. **The `revise` node is deterministic Python, not another LLM call.** When the critic returns `suggested_changes` (e.g., `{"limit_price": 182.40, "anchor_method": "pivot_weekly_S1"}`), Python applies them mechanically. An LLM-driven revise step could compound errors by hallucinating *new* changes beyond what the critic asked for. Rule: LLMs make judgments, Python applies them. Documented in ADR-0013.
3. **The graph reads from `news_event` for context but never writes to it.** News classification belongs to 3b's graph. 3c only consumes the `llm_material=true` rows from the last 7 days as input to the critic's reasoning. Strict separation of concerns.

4. **Graph state is ephemeral (`MemorySaver`, not `SqliteSaver`).** Phase 3b discovered that `SqliteSaver` and SQLAlchemy contend for the OLTP file's write lock — the checkpointer would block waiting on SQLAlchemy and the graph would silently fail. 3b switched to `MemorySaver`; 3c inherits this via the shared `make_checkpointer()` factory. Practical effect: each `graph.invoke()` has full per-run state, but state is gone after the call returns. `langgraph dev --thread-id <id>` won't show historical runs the way it would with SqliteSaver. If you need a reasoning trace after the fact, write it into `llm_call_log` (which is persistent) or into the `order_suggestion.reason` field.

5. **The "extract-to-plain-Python-inside-session" rule is non-negotiable.** This is the third recurrence of the DetachedInstanceError pattern — Phase 1 Bug 2 (`BrokerAccount` → `AccountSnapshot`), Phase 3b Bug 1 (`MoverState` → `dict[str, float]`), and now `gather_context_node` in 3c must finish materialising every value before the session closes. ORM rows leaking past `with session_scope():` is the bug pattern; the fix is always to project to a frozen dataclass or primitive dict at the boundary.

---

## 0. Pre-flight checklist (~15 minutes)

- [ ] Phase 3a (`v0.3a.0`) and Phase 3b (`v0.3b.0`) both tagged. Confirm via `git tag -l`. **Both have pre-tag checklists that require observing live behaviour — don't start 3c until both tags are pushed.** If you only have 3a tagged but 3b is still pending the first movers email, wait.
- [ ] Confirm `sr_level.confidence` is being populated weekly: `sqlite3 data/investor.db "SELECT COUNT(*) FROM sr_level WHERE confidence IS NOT NULL"` is non-zero.
- [ ] Confirm `news_event` has at least one week of data: `sqlite3 data/investor.db "SELECT COUNT(*) FROM news_event WHERE llm_material = 1"` is non-zero. If you haven't had a movers day yet, manually trigger the movers job to seed.
- [ ] Confirm `make_checkpointer()` in `graphs/__init__.py` returns `MemorySaver` (3b shipped with this after Bug 3 — write-lock contention with the OLTP engine). 3c's suggestion-review graph uses the same factory. Do **not** switch to `SqliteSaver` — same write-lock contention will silently empty 3c's output too.
- [ ] **`LLM_DAILY_COST_CAP_USD` raised to at least $3.00** (3b defaulted it there). 3c adds news-augmented level scoring + reason node + critic node = roughly 3× the per-weekly-run LLM volume vs. 3a alone. If the cap trips silently mid-run, fallback paths fire but rationales degrade. Bump to $5 if you see misses.
- [ ] **`LLM_CLI_PATH` configuration — personal-use choice acknowledged.** The project owner (Jane) has explicitly chosen to route `LLM_BACKEND=agent_sdk` through a system-installed `claude` CLI authenticated with consumer (Pro/Max) OAuth, for solo personal use. This is technically against Anthropic's published terms (which prohibit automated/unattended OAuth use), and the choice is being made with eyes open about the trade-off. Operational guardrails to keep risk minimal: (a) keep `ANTHROPIC_API_KEY` configured as a working fallback so you can flip `LLM_BACKEND=anthropic_api` in seconds if Anthropic changes enforcement or flags the account; (b) treat this configuration as **single-user only — if Phase 5 multi-tenant ever ships, the OAuth path must be removed before any second user signs up**; (c) keep daily call volume reasonable (don't blast the cron during dev iteration); (d) monitor for any "unusual activity" emails from Anthropic and flip to `anthropic_api` immediately if any arrive. ADR-0016 documents the choice and the guardrails.
- [ ] All Phase 3a + 3b smoke tests still pass.

---

## 1. `ReviewContext` and `gather_context` node (~half evening)

This is pure Python — no LLM call. It assembles every input the reasoning + critic steps need into one frozen dataclass.

```python
# src/investor/graphs/suggestion_review.py (partial)

@dataclass(frozen=True)
class ReviewContext:
    gap_rows: list[GapRow]
    scored_levels: dict[str, list[ScoredLevel]]      # ticker -> sorted by confidence desc
    recent_news: dict[str, list[NewsTriageItem]]     # last 7 days, material only
    indicators: dict[str, IndicatorRow]
    account: AccountSnapshot
    untracked_positions: list[UntrackedPosition]


class SuggestionReviewState(TypedDict):
    week_of: date
    context: ReviewContext
    drafts: list[OrderSuggestionRow]
    rationales: dict[int, str]                       # draft_index -> Sonnet's reasoning
    critic_decisions: dict[int, CriticDecision]      # draft_index -> verdict + changes
    finals: list[OrderSuggestionRow]
    telemetry: dict


def gather_context_node(state: SuggestionReviewState, session_factory) -> SuggestionReviewState:
    with session_factory() as s:
        # CRITICAL: every loader below must return frozen dataclasses or
        # primitive dicts/lists — NEVER bare SQLAlchemy ORM rows. The
        # DetachedInstanceError pattern (Phase 1 Bug 2, Phase 3b Bug 1)
        # is the most-recurrent bug in this project; the only defense is
        # to refuse to let ORM objects leave the session.
        gap_rows = compute_gap(s)                          # list[GapRow] (frozen dataclass)
        scored_levels = load_latest_scored_levels(s)       # dict[str, list[ScoredLevel]]
        recent_news = load_recent_material_news(s, days=7) # dict[str, list[NewsTriageItem]]
        indicators = compute_indicators(get_watchlist())   # list[IndicatorRow] (3a)
        account = get_latest_account_snapshot(s)           # AccountSnapshot (3a)
        untracked = get_untracked_positions(s)             # list[UntrackedPosition]
    ctx = ReviewContext(gap_rows=gap_rows, scored_levels=scored_levels,
                        recent_news=recent_news, indicators=indicators,
                        account=account, untracked_positions=untracked)
    return {**state, "context": ctx}
```

Three rules for every `load_*` helper inside `gather_context_node`:

1. **Return frozen dataclasses, not ORM rows.** Each helper must call `.model_dump()` or manually project to a `@dataclass(frozen=True)` *before* returning. If a helper currently returns ORM rows, that's a bug — fix at the helper, not in the node.
2. **No lazy relationships.** If a helper queries `news_event` and reads `.ticker` later outside the session, eager-load that column inside the session.
3. **Session lifetime is the `with` block. Period.** Nothing reads from `s` after the `with` block exits. Test row 2 of the smoke-test checklist enforces this with an assertion.

---

## 2. `reason_about_drafts` node — per-draft rationale (~1 evening)

Sonnet writes a 2–4 sentence rationale for each draft considering the full context. Per-draft logic, but bundled into one prompt with a JSON-list output to save a round trip.

```python
class DraftRationale(BaseModel):
    draft_index: int
    rationale: str                       # 2-4 sentences, ≤ 600 chars


class DraftRationales(BaseModel):
    items: list[DraftRationale]


def reason_node(state: SuggestionReviewState, llm: LLMClient) -> SuggestionReviewState:
    system = load_prompt("suggestion_reason_v1.txt")
    user = json.dumps({
        "drafts": [
            {
                "index": i, "ticker": d.ticker, "side": d.side,
                "qty": d.qty, "limit_price": d.limit_price,
                "confidence_at_creation": d.confidence_at_creation,
                "anchor_method": d.anchor_method,
            }
            for i, d in enumerate(state["drafts"])
        ],
        "gap_summary": [
            {"ticker": g.ticker, "current_pct": g.current_pct, "target_pct": g.target_pct,
             "gap_pct": g.gap_pct, "band_status": g.band_status}
            for g in state["context"].gap_rows
        ],
        "scored_levels": {
            t: [{"method": lv.method, "price": lv.price, "confidence": lv.confidence,
                 "rationale": lv.rationale} for lv in levels[:5]]
            for t, levels in state["context"].scored_levels.items()
        },
        "recent_material_news": {
            t: [{"sentiment": n.sentiment, "summary": n.summary}
                for n in news if n.is_material]
            for t, news in state["context"].recent_news.items()
        },
        "indicators": {
            t: {"close": ind.close, "rsi_14": ind.rsi_14,
                "pct_from_sma_50": ind.pct_from_sma_50,
                "pct_from_sma_200": ind.pct_from_sma_200}
            for t, ind in state["context"].indicators.items()
        },
        "account": {"cash_usd": state["context"].account.cash_usd,
                    "equity_usd": state["context"].account.equity_usd},
    }, default=str)

    parsed, tel = llm_node_call(
        purpose="suggestion_reason", model=SONNET, system=system, user=user,
        schema=DraftRationales, fallback_factory=lambda: DraftRationales(items=[]),
        llm=llm,
    )
    rationales = {it.draft_index: it.rationale for it in parsed.items}
    return {**state, "rationales": rationales,
            "telemetry": {**state["telemetry"], **tel}}
```

Prompt (`prompts/suggestion_reason_v1.txt`) instructs Sonnet to write a 2–4 sentence rationale per draft. Hard rules carry over: no invented prices, no fundamental claims, no buy/sell recommendations beyond what the engine already proposed. Cite specific evidence from the context — confidence score, recent news sentiment, RSI, distance from MA, gap %.

---

## 3. `critic` node — review drafts as a set (~1 evening)

Sonnet reviews all drafts + their rationales as a *set*. This is where cross-suggestion problems get caught.

```python
class CriticDecisionOut(BaseModel):
    draft_index: int
    verdict: Literal["approve", "revise", "reject"]
    reason: str                                      # human-readable
    suggested_changes: dict[str, Any] | None         # for "revise" only


class CriticDecisions(BaseModel):
    items: list[CriticDecisionOut]


@dataclass(frozen=True)
class CriticDecision:
    verdict: Literal["approve", "revise", "reject"]
    reason: str
    suggested_changes: dict[str, Any] | None


def critic_node(state: SuggestionReviewState, llm: LLMClient) -> SuggestionReviewState:
    system = load_prompt("suggestion_critic_v1.txt")
    user = json.dumps({
        "drafts_with_rationales": [
            {
                "index": i, "ticker": d.ticker, "side": d.side,
                "qty": d.qty, "limit_price": d.limit_price,
                "confidence_at_creation": d.confidence_at_creation,
                "anchor_method": d.anchor_method,
                "rationale": state["rationales"].get(i, ""),
            }
            for i, d in enumerate(state["drafts"])
        ],
        "account": {"cash_usd": state["context"].account.cash_usd,
                    "cash_floor": 100},
        "untracked_positions": [
            {"ticker": p.ticker, "qty": p.qty, "market_value": p.market_value}
            for p in state["context"].untracked_positions
        ],
        "recent_material_news_by_ticker": {
            t: [{"sentiment": n.sentiment, "summary": n.summary}
                for n in news if n.is_material]
            for t, news in state["context"].recent_news.items()
        },
    }, default=str)

    parsed, tel = llm_node_call(
        purpose="suggestion_critic", model=SONNET, system=system, user=user,
        schema=CriticDecisions, fallback_factory=lambda: CriticDecisions(items=[]),
        llm=llm,
    )
    decisions = {
        it.draft_index: CriticDecision(verdict=it.verdict, reason=it.reason,
                                       suggested_changes=it.suggested_changes)
        for it in parsed.items
    }
    return {**state, "critic_decisions": decisions,
            "telemetry": {**state["telemetry"], **tel}}
```

### 3a. Critic prompt (`prompts/suggestion_critic_v1.txt`)

```
You are a senior reviewer for a long-term US-equity investor's
order-suggestion engine. The engine has produced draft suggestions with
rationales. Your job: for each draft, return APPROVE, REVISE, or REJECT.

Review for these problems (in order of severity):
1. Disqualifying news: any material bearish/bullish news in
   recent_material_news_by_ticker that contradicts the suggestion?
   (Example: a "buy" suggestion on a ticker with material bearish news
   from this week.)
2. Cross-suggestion cash: do all the BUY suggestions combined
   leave account.cash_usd below the cash_floor?
3. Rationale ≠ math: does the written rationale match the
   confidence_at_creation and the chosen limit_price? (Example:
   rationale says "strong support" but confidence is 0.3.)
4. Direction wrong: would executing this suggestion push the ticker
   through its band rather than toward target?
5. Untracked overlap: does any suggestion buy a ticker that also
   appears in untracked_positions? (Suggests the user already has an
   off-target position there — at minimum the reason should acknowledge.)

For REVISE: include suggested_changes with the fields you want changed.
Supported fields:
   - limit_price (must be a price from scored_levels for that ticker)
   - qty (a positive number, in shares)
   - anchor_method (must match an existing scored level)
The engine will apply changes deterministically — do NOT suggest
arbitrary new prices.

For REJECT: include a clear reason. The engine will drop the draft.

Hard rules:
- You do NOT invent new prices or new tickers.
- You do NOT make claims about company fundamentals beyond what's in
  recent_material_news_by_ticker.
- You judge only against the data you've been given.

Output ONLY JSON, no preamble:
{"items": [{"draft_index":0, "verdict":"approve", "reason":"...",
            "suggested_changes":null}, ...]}
```

---

## 4. `revise` node — deterministic Python (~half evening)

This is the most important architectural decision in 3c. The revise node applies the critic's structured `suggested_changes` mechanically. No LLM call. Why:

1. **LLMs compound errors.** An LLM-driven revise node could decide to "improve" the change beyond what the critic asked for. Then on the next run the critic might see the new state and want to revise *again*. Loop risk.
2. **Auditability.** A deterministic node is fully testable; you can prove that for any `(draft, suggested_changes)` pair the output is exactly what you expect.
3. **Cost.** No additional LLM call per revise.

```python
def revise_node(state: SuggestionReviewState) -> SuggestionReviewState:
    """Apply critic decisions. Pure Python, no LLM."""
    decisions = state["critic_decisions"]
    finals: list[OrderSuggestionRow] = []
    for i, draft in enumerate(state["drafts"]):
        dec = decisions.get(i)
        if dec is None or dec.verdict == "approve":
            finals.append(draft)
            continue
        if dec.verdict == "reject":
            log.info("critic rejected draft %s/%s: %s", i, draft.ticker, dec.reason)
            continue
        # revise: apply suggested_changes
        changes = dec.suggested_changes or {}
        revised = _apply_changes(draft, changes, state["context"])
        if revised is None:
            log.warning("critic asked for invalid changes on draft %s: %r", i, changes)
            finals.append(draft)              # keep original on bad changes
            continue
        finals.append(revised)
    return {**state, "finals": finals}


def _apply_changes(draft: OrderSuggestionRow, changes: dict,
                   ctx: ReviewContext) -> OrderSuggestionRow | None:
    """Mechanically apply changes. Return None if any change is invalid."""
    new_limit = draft.limit_price
    new_qty = draft.qty
    new_anchor = draft.anchor_method

    if "anchor_method" in changes:
        ticker_levels = ctx.scored_levels.get(draft.ticker, [])
        match = next((lv for lv in ticker_levels if lv.method == changes["anchor_method"]), None)
        if not match:
            return None                       # critic referenced unknown method
        new_anchor = match.method
        new_limit = match.price               # anchor_method change always re-sets limit_price

    if "limit_price" in changes:
        # only honour an explicit limit_price if anchor_method wasn't also changed
        if "anchor_method" not in changes:
            requested = float(changes["limit_price"])
            ticker_levels = ctx.scored_levels.get(draft.ticker, [])
            if not any(abs(lv.price - requested) < 0.01 for lv in ticker_levels):
                return None                   # critic invented a new price
            new_limit = requested

    if "qty" in changes:
        new_qty = max(0.0, float(changes["qty"]))
        if new_qty < 1:
            return None

    return replace(draft, limit_price=new_limit, qty=new_qty, anchor_method=new_anchor)
```

Notice how `_apply_changes` rejects any change that would invent a new price — `limit_price` must match a known scored level, `anchor_method` must reference an existing one. The critic can choose between known levels but cannot fabricate new ones.

### Conditional edge — skip revise if all approved

```python
def route_after_critic(state: SuggestionReviewState) -> Literal["revise", "skip_revise"]:
    has_changes = any(
        d.verdict in ("revise", "reject")
        for d in state["critic_decisions"].values()
    )
    return "revise" if has_changes else "skip_revise"


def skip_revise_node(state: SuggestionReviewState) -> SuggestionReviewState:
    return {**state, "finals": list(state["drafts"])}
```

Happy path (all approved) skips the revise node entirely. Faster, no Python pointlessly running.

---

## 5. `finalize` + persist (~1 evening)

Reuses `persist_suggestions()` from Phase 2 — its "never overwrite non-pending rows" semantic is exactly what's wanted here.

```python
def finalize_node(state: SuggestionReviewState, session_factory) -> SuggestionReviewState:
    targets_id = state.get("targets_id")
    with session_factory() as s:
        persist_suggestions(s, state["finals"], targets_id, week_of=state["week_of"])
    return state
```

State is unchanged — the node has the side effect of persisting. Final email is rendered outside the graph using `state["finals"]` and `state["rationales"]`.

---

## 6. Graph assembly + wire into weekly job (~half evening)

```python
def build_suggestion_review_graph(llm: LLMClient, session_factory):
    g = StateGraph(SuggestionReviewState)
    g.add_node("gather_context", lambda s: gather_context_node(s, session_factory))
    g.add_node("reason",         lambda s: reason_node(s, llm))
    g.add_node("critic",         lambda s: critic_node(s, llm))
    g.add_node("revise",         revise_node)
    g.add_node("skip_revise",    skip_revise_node)
    g.add_node("finalize",       lambda s: finalize_node(s, session_factory))
    g.add_edge(START, "gather_context")
    g.add_edge("gather_context", "reason")
    g.add_edge("reason", "critic")
    g.add_conditional_edges("critic", route_after_critic,
                            {"revise": "revise", "skip_revise": "skip_revise"})
    g.add_edge("revise", "finalize")
    g.add_edge("skip_revise", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=CHECKPOINTER)
```

In `jobs/weekly_suggestions.py`:

```python
def run_weekly_suggestions(settings, adapter, emailer, llm):
    update_bars(settings.watchlist)
    indicators = compute_indicators(settings.watchlist)
    scored_levels = {
        t: score_levels_for_ticker(llm=llm, ticker=t, ...)
        for t in settings.watchlist
    }

    with session_scope() as s:
        take_snapshot(adapter, s)
        gap_rows = compute_gap(s)
        drafts = generate_suggestions(gap_rows, scored_levels, ...)
        # NOTE: do NOT persist drafts here. Suggestion-review graph persists finals.

    # Review graph runs OUTSIDE the session
    graph = build_suggestion_review_graph(llm, session_scope)
    result = graph.invoke(
        {"week_of": next_monday(), "drafts": drafts,
         "context": None, "rationales": {}, "critic_decisions": {}, "finals": [],
         "telemetry": {}},
        config={"configurable": {"thread_id": f"weekly-{next_monday()}"}},
    )

    # Email — uses result["finals"] AND result["rationales"]
    html = render_template("weekly_suggestions.html.j2",
                           suggestions=result["finals"],
                           rationales=result["rationales"],
                           critic_decisions=result["critic_decisions"],
                           ...)
    emailer.send(...)
```

The weekly email template now picks up the Sonnet-written rationale (2–4 sentences) as the visible reason, while the mechanical `order_suggestion.reason` string remains in the DB for the audit trail.

Cron schedule from Phase 2 is unchanged — Sunday 18:00 ET. Misfire grace stays 6 hours.

### Cost expectation

| Node | Model | Calls per weekly run | Approx tokens | Cost |
|---|---|---|---|---|
| `gather_context` | n/a | 0 | — | $0.00 |
| `reason_about_drafts` | Sonnet 4.6 | 1 | ~3k in / ~1k out | ~$0.024 |
| `critic` | Sonnet 4.6 | 1 | ~3k in / ~0.5k out | ~$0.017 |
| `revise` | n/a (Python) | 0 | — | $0.00 |
| **Per weekly run** | | | | **~$0.04** |

Phase 3 total stays well under $10/month at single-user volume.

---

## 6.5. Three small additions pulled in from 3b's recommendation (~1.5 evenings combined)

The Phase 3b completion suggested three optimisations/closures for 3c. All small, all worth doing before the close-out tag.

### 6.5a. News-augmented level scoring — `score_levels_v2.txt` (~half evening)

Phase 3a's `prompts/score_levels_v1.txt` includes a placeholder note: *"In a future version you will also receive recent news headlines and earnings context per ticker."* Phase 3c is when that placeholder is closed.

Steps:

1. Pull the prompt forward: copy `score_levels_v1.txt` → `score_levels_v2.txt`. Add a new top-level input field `recent_material_news`: a list of `{sentiment, summary}` objects from the last 24 hours' material news for that ticker. Add an explicit instruction: *"If recent_material_news contains material bearish events, downgrade confidence on support levels; if bullish, downgrade confidence on resistance levels. The level itself is not invalidated — its near-term reliability is reduced."*
2. Update `Settings.level_prompt_version` default to `"v2"`. The mechanism for prompt versioning already exists from Phase 3a (the version is named in Settings, the loader picks the file).
3. Update `score_levels_for_ticker()` in `llm_levels.py` to accept an optional `recent_news` parameter and include it in the user-prompt JSON. Backward-compatible — if `recent_news=None`, behaviour is identical to v1.
4. In `jobs/weekly_suggestions.py`, before scoring each ticker's levels, load the last-24h material news for that ticker from `news_event` and pass to the scorer.

**Test (smoke row 11):** with a known-bearish news event in `news_event` for AAPL, the confidence on AAPL's `sma_50_support` (or whatever the nearest support is) should be measurably lower than the same level scored without news context. Snapshot-test the prompt version transition by running both v1 and v2 on the same fixture and asserting the rationale text differs in expected ways (mentions news, lower confidence on support).

This change feeds into Phase 3c's overall reasoning quality: when the suggestion-review critic later sees a buy suggestion on AAPL, both the rationale (which mentions the lower-than-usual confidence) and the news context (which the critic sees independently) point at the same concern. Two signals, same conclusion.

### 6.5b. Parallel level scoring with `ThreadPoolExecutor` (~half evening)

Phase 3a reported ~30 s per ticker for scoring. At 8 tickers that's 4 minutes of serial Sonnet calls per weekly run. `LLMClient.call()` is sync and thread-safe (both backends — `AnthropicAPIClient` uses the thread-safe `anthropic.Anthropic` client, `AgentSDKClient` creates a fresh event loop per call). Parallel scoring drops the wall-clock to roughly the slowest ticker's latency.

```python
# in jobs/weekly_suggestions.py
from concurrent.futures import ThreadPoolExecutor, as_completed

def score_all_tickers_parallel(
    tickers: list[str], llm: LLMClient, max_workers: int = 4,
) -> dict[str, list[ScoredLevel]]:
    """Score levels for all tickers in parallel.

    max_workers=4 is conservative — Anthropic rate-limits on the free
    tier are tighter than you'd think during bursts. Tune up if you see
    no 429s.
    """
    out: dict[str, list[ScoredLevel]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(score_levels_for_ticker, llm=llm, ticker=t, ...): t
            for t in tickers
        }
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                out[t] = fut.result()
            except Exception as e:
                log.warning("scoring failed for %s: %s", t, e)
                out[t] = []          # falls back to nearest-distance for this ticker
    return out
```

The daily cost cap in `LLMClient` is shared across threads via the same instance — concurrent calls share `_spent_today`. Race on the cap check is benign: at worst, you slightly exceed the cap before the next call notices and trips. No need for explicit locking.

**Test (smoke row 12):** wall-clock time for `score_all_tickers_parallel(tickers=[...]*8, llm=mock_llm)` is < 2× the per-call mock latency (vs. ≥ 8× if serial). Add `tests/test_weekly_suggestions.py::test_parallel_scoring_wall_clock`.

### 6.5c. Suggestion expiry sweep (~half evening)

`order_suggestion` has an `expires_at` column (set to Friday EOD of `week_of`). Phase 3a writes the column; nothing reads or sweeps it. The result: stale `pending` rows from past weeks accumulate, and the daily report's "pending suggestions" count grows misleadingly.

Add a small daily sweep:

```python
# new file: jobs/suggestion_expiry.py
def sweep_expired_suggestions(session_factory):
    with session_factory() as s:
        now = datetime.now(UTC)
        stale = s.scalars(
            select(OrderSuggestion)
            .where(OrderSuggestion.status == "pending",
                   OrderSuggestion.expires_at < now)
        ).all()
        for sug in stale:
            sug.status = "expired"
            sug.acted_at = now
        s.commit()
        log.info("expired %d stale suggestions", len(stale))
```

Schedule alongside the existing daily report:

```python
sched.add_job(
    sweep_expired_suggestions,
    trigger=CronTrigger(day_of_week="mon-fri", hour=16, minute=20,
                       timezone="America/New_York"),
    id="suggestion_expiry", misfire_grace_time=60 * 30,
)
```

16:20 ET runs between the 16:15 daily report and the 16:30 movers. Sequenced this way, the daily email reflects the freshly-swept state.

**Test (smoke row 13):** seed a `pending` row with `expires_at` in the past; call `sweep_expired_suggestions`; assert status is now `expired` and `acted_at` is populated.

---

## 7. Smoke-test checklist (Phase 3c done when all green)

| # | Step | Pass criteria |
|---|---|---|
| 1 | `uv run pytest -m "not integration"` | All tests pass; total now ≥ 82; per-node + graph-integration + critic-prompt golden tests included |
| 2 | Per-node test: `gather_context_node` populates `ReviewContext` from a seeded DB | `ctx.scored_levels`, `ctx.recent_news`, `ctx.indicators`, `ctx.account` all non-empty |
| 3 | Per-node test: `reason_node` with mocked LLM returns rationales for each draft index | `state["rationales"]` keys cover all `state["drafts"]` indices |
| 4 | Per-node test: `critic_node` with mocked LLM returns `CriticDecision` for each draft | Same key coverage |
| 5 | Per-node test: `revise_node` applies a `suggested_changes={"limit_price": <known level>}` | Output `finals[i].limit_price` equals the requested known level |
| 6 | Per-node test: `revise_node` with `suggested_changes={"limit_price": 999.99}` (unknown) | Falls back to original draft, warning logged |
| 7 | Conditional edge: all approved → skip_revise | `state["finals"]` equals `state["drafts"]` |
| 8 | Conditional edge: one revise/reject → revise | Critic decisions applied; rejected drafts absent from finals |
| 9 | Manual: `curl -H "X-Admin-Token: …" -X POST /admin/run-weekly-suggestions` | 200; graph checkpoint exists for `weekly-{next_monday}`; rationales present in result |
| 10 | One Sunday email observed | Rationales are 2–4 sentences (not single mechanical lines); at least one critic decision was `revise` or `reject` (verifiable in DB-level logs / `llm_call_log` — *not* `langgraph dev` since 3b switched to `MemorySaver`); email feels deliberately reasoned through |
| 11 | News-augmented level scoring works (§6.5a) | With a known-bearish material `news_event` for AAPL injected, the confidence on AAPL's nearest support is measurably lower than the same level scored without news context. `level_prompt_version='v2'` written into `sr_level.prompt_version`. |
| 12 | Parallel scoring wall-clock (§6.5b) | `score_all_tickers_parallel` with 8 mock tickers + 200 ms mock latency per call completes in < 800 ms (vs. ≥ 1.6 s serial). Cost-cap behaviour under concurrent calls correctly accumulates. |
| 13 | Suggestion expiry sweep (§6.5c) | Seed a `pending` `order_suggestion` with `expires_at` in the past; call `sweep_expired_suggestions`; status becomes `expired`, `acted_at` populated. Row that's still in-window (future `expires_at`) is untouched. |
| 14 | Session-leak guard | `gather_context_node` returns; outside the `with session_scope():` block, accessing any field on `ctx.gap_rows[0]`, `ctx.scored_levels[...]`, `ctx.recent_news[...]`, `ctx.indicators[0]`, `ctx.untracked_positions[0]` does NOT raise `DetachedInstanceError`. (This is the structural fix for Phase 1 Bug 2 / Phase 3b Bug 1 — Phase 3c is the third recurrence opportunity.) |
| 15 | Critic invents a level price (adversarial) | Mock the critic node to return `suggested_changes={"limit_price": 999.99}` for AAPL where no scored level has that price. `_apply_changes` rejects the change; warning logged; original draft kept unchanged. |

Tag and push — this closes all of Phase 3:

```bash
git add -A
git commit -m "phase 3c: suggestion review via LangGraph"
git tag v0.3.0-phase-3
git push --tags
```

---

## 8. Common Phase 3c pitfalls

1. **Critic over-rejection.** A critic prompt too strict will reject everything; one too loose adds no value. Calibrate by replaying the suggestion-review graph against 3–4 weeks of historical data (from `order_suggestion` rows) — target a critic reject-or-revise rate in 10–25%. Below 5% means the critic is rubber-stamping; above 40% means it's blocking legitimate suggestions. Adjust the prompt rubric and version-bump.
2. **Critic invents new prices via `suggested_changes`.** `_apply_changes` rejects any `limit_price` not matching a known scored level — test row 6 protects. If you see lots of these in logs, the critic prompt is straying; tighten the "do NOT invent" rules in the prompt.
3. **Graph state mutability.** As in 3b: never mutate `state` in place. Always return `{**state, "key": value}`.
4. **Session-leak hazard inside graph nodes.** Multiple graph nodes potentially open SQLAlchemy sessions. `gather_context_node` opens once and closes once before returning. Subsequent LLM nodes do not touch the DB. Only `finalize_node` reopens. If you find yourself wanting to read DB state in `reason_node` or `critic_node`, refactor — gather everything you need into `context` first.
5. **Rationale truncation.** Rationales must be ≤ 600 chars (per the schema). Sonnet occasionally overshoots; truncate at parse time. The email template should also truncate display to 2–4 sentences if longer.
6. **The reasoner contradicts the critic.** Sonnet sometimes writes a glowing rationale and then the critic rejects the same draft. This is by design — the rationale is per-draft, the critic looks across the set. But if you see *too much* of this, the reasoner prompt isn't conditioning on the same context the critic uses. Re-share the same context in both prompts.
7. **Critic decisions reference drafts by index.** If `drafts` is reordered between reason and critic nodes, indices break. Don't reorder in any node. If you must, propagate the new ordering through state.
8. **Re-running the same week.** `persist_suggestions` already enforces never-overwrite-non-pending. But the graph itself, if re-invoked with `thread_id=f"weekly-{next_monday()}"`, will overwrite the previous checkpoint. Either bump thread_id with a counter (`weekly-2026-05-25-attempt-2`) for re-runs or accept that only the latest reasoning trace is preserved.
9. **Cost spike on initial dev runs.** While iterating on prompts, you may invoke the full graph many times. The `daily_cost_cap_usd` guard catches catastrophes but watch `llm_call_log` daily during the first week.
10. **`MemorySaver`, not `SqliteSaver` — checkpoint state is gone after `graph.invoke()` returns.** Phase 3b discovered the write-lock contention bug; do not revert. If you need to debug a graph run after the fact, the audit trail lives in `llm_call_log` (purpose, model, cost, error) and `order_suggestion` (final outputs, rationales). The "reasoning trace per node" granularity LangGraph provides with SqliteSaver is forfeit at single-user scale.
11. **`LLM_CLI_PATH` and Pro/Max OAuth — solo-personal-use accepted, multi-tenant prohibited.** The project owner has explicitly chosen to route `LLM_BACKEND=agent_sdk` through a Pro/Max-authenticated `claude` CLI. Anthropic's published terms prohibit automated/unattended OAuth use; practical enforcement risk against a solo user is low but non-zero. Operational rules: (a) `ANTHROPIC_API_KEY` must remain configured as a working fallback — the `make_llm_client` factory should still be able to construct `AnthropicAPIClient` if the OAuth path is revoked; (b) test `LLM_BACKEND=anthropic_api` periodically (monthly minimum) so the fallback is known-good when you need it; (c) **this configuration is single-user only forever** — Phase 5's multi-tenant work explicitly removes the OAuth path before any second user signs up, because gray-area-for-one-user becomes unambiguously-abusive-for-many; (d) if you ever receive an "unusual activity" notice from Anthropic, flip to `anthropic_api` immediately and consider the OAuth path permanently retired. ADR-0016 documents the choice and the guardrails.
12. **DetachedInstanceError is the most-recurrent pattern in this codebase.** Phase 1 Bug 2 on `BrokerAccount`, Phase 3b Bug 1 on `MoverState`, and now `gather_context_node` is the third opportunity to introduce it. Every load helper inside the node must return plain dataclasses or primitives. Smoke test row 14 enforces.
13. **`AgentSDKClient` async-bridge inheritance.** 3c calls `LLMClient.call()` via `llm_node_call`, which means it inherits 3b's manual event-loop teardown workaround for `claude-agent-sdk` 0.1.x. When the SDK is upgraded to 0.2.x, re-test with the simpler `asyncio.run()` form first — if the `aclose()` error from 3b Bug 4 is gone, revert.

---

## 9. ADRs to write in Phase 3c

- **`docs/adr/0013-suggestion-review-pipeline.md`** — new. The reason → critic → revise → finalize flow. **Why the `revise` node is deterministic Python and not LLM-driven** — prevents compounding errors. What the critic looks at and in what priority order. Calibration target (10–25% reject-or-revise rate). Anchors the architectural rule "LLMs make judgments, Python applies them."
- **`docs/adr/0006-sr-methodology.md`** — *final update*. Remove ⚠ Pending flag. The complete pipeline is now documented: mechanical computation → Sonnet single-call scoring (with `score_levels_v2.txt` news-augmented prompt from §6.5a) → suggestion engine selects confidence-weighted-within-band anchor → suggestion review graph (3c) refines via reason+critic+revise.
- **`docs/adr/0007-position-sizing.md`** — *final update*. Remove ⚠ Pending flag. Sizing rule unchanged (`HALF_THE_GAP`); anchor selection is confidence-weighted-within-band, refinable by critic.
- **`docs/adr/0016-llm-backend-abstraction.md`** — *update*. Add a "Consumer OAuth — personal-use choice" section that records: (a) the project owner has chosen to route `LLM_BACKEND=agent_sdk` through a Pro/Max-authenticated `claude` CLI for solo personal use; (b) this is technically against Anthropic's published TOS (which prohibits automated OAuth use), accepted as a low-but-nonzero risk for solo use; (c) `ANTHROPIC_API_KEY` is maintained as a tested fallback; (d) the configuration is irrevocably single-user — Phase 5's multi-tenant work must remove the OAuth path before any second user signs up. The ADR should record this as an explicit, dated decision so a future agent or contributor doesn't quietly extend the OAuth path into multi-tenant deployment.

One new ADR, three updates. About 90 minutes total.

---

## 10. Documentation drift to fix

- **CLAUDE.md** — add to "Things to never do": "Never make the `revise` step LLM-driven — LLMs propose changes, Python applies them." "Never extend the `LLM_CLI_PATH`-with-consumer-OAuth configuration into multi-tenant deployment — the current solo-personal-use routing through a Pro/Max-authenticated CLI is an explicit single-user choice (see ADR-0016). Any second user requires the OAuth path to be removed and `LLM_BACKEND` reset to `anthropic_api` for all users." Add new architectural convention #12: "Multi-source LLM workflows go in `graphs/`; the `gather` node materialises all DB state into a frozen dataclass *before* any LLM node runs, so no session is held across LLM calls. The DetachedInstanceError pattern (Phase 1 Bug 2, Phase 3b Bug 1) is the bug this convention prevents — non-negotiable, no exceptions." Add new architectural convention #13: "**`ANTHROPIC_API_KEY` must remain configured and tested even when `LLM_BACKEND=agent_sdk` is the primary path.** The `make_llm_client` factory must be able to construct a working `AnthropicAPIClient` at any moment. Test monthly that flipping `LLM_BACKEND=anthropic_api` in `.env` produces a working LLM call. The OAuth path is gray-area-tolerated for solo use; the API-key path is the canonical, TOS-clean route and must stay viable as the fallback." Add to common gotchas: "LangGraph checkpointer is `MemorySaver` (3b Bug 3 fix) — never switch to `SqliteSaver` while OLTP shares the same DB file." Add `prompts/suggestion_reason_v*.txt`, `prompts/suggestion_critic_v*.txt`, and `prompts/score_levels_v2.txt` references in repo layout.
- **`product_plan.md`** — mark Phase 3 fully complete (`v0.3.0-phase-3`). Update §6 to note that Phase 4 is next.
- **ADR index** — finalise entries for ADRs 0009–0013.

---

## 11. What Phase 3 deliberately does not include (recap)

Carries over from the original Phase 3 scope. Phase 3 ships with:

- ✅ LLM-scored confidence on every S/R level (3a)
- ✅ Confidence-weighted anchor selection in the suggestion engine (3a)
- ✅ Accept/reject endpoint + HMAC magic links (3a)
- ✅ News triage with critic + arbitration (3b)
- ✅ Movers email on ≥ 5% weekly moves (3b)
- ✅ Suggestion review graph with critic + deterministic revise (3c)
- ✅ Rich rationales in the weekly email (3c)

But deliberately NOT:

- **Moomoo adapter** — Phase 4. The reason is risk: Phase 3 introduces LLM-influenced suggestion logic that needs to soak for at least 4 weeks of paper trading before real Moomoo capital touches it.
- **Suggested-vs-filled reconciliation** — Phase 4 work. Requires broker `account/activities` integration.
- **Web UI** — Phase 5. The email + magic links cover 95% of value at single-user scale.
- **Backtesting framework** — Phase 5+. Live data first.

---

*When all 10 smoke-test rows are green, you've received one Sunday weekly suggestions email where rationales are 2–4 sentences and the critic visibly rejected or revised at least one draft, the news-triage and suggestion-review graph checkpoint traces both exist in SQLite, and ADR-0013 is committed plus 0006/0007 are fully closed (no ⚠ flags remaining), Phase 3 is complete. Tag `v0.3.0-phase-3` and start Phase 4.*
