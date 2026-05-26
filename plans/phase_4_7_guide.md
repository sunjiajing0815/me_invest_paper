# Phase 4.7 — Context-Aware Weekly Order Sizing: Step-by-Step Guide

**Goal:** Make the Weekly Market Context (Phase 4.5) and a structured earnings calendar *drive the quantities and limit anchors* of the Sunday weekly order suggestions. A new `context_adjust` node in the Phase 3c suggestion-review graph scales each draft's `qty` up or down based on macro / sector / company event risk, and a deterministic earnings gate shrinks (and re-anchors to a deeper level) any draft whose ticker reports earnings in the coming week. The product stays **suggest-only**, and limit prices still come *only* from computed `sr_level` anchors — context never invents a price and never conjures a ticker the gap engine didn't already surface.

**Out of scope for Phase 4.7:**
- Generating *new* suggestions from context. Context scales drafts the gap engine produced; it cannot originate a buy/sell for a ticker that wasn't already under/over band. (This holds even with bidirectional "full influence" — see Behaviour 1.)
- Auto-trade changes. Adjusted suggestions, once accepted, flow through the existing Phase 4.6 auto-trade path unchanged. No new execution code.
- Real-time / intraday context. Still weekly, computed at the Sunday 18:00 ET run.
- New vendors. Earnings comes from Finnhub (already integrated in Phase 3b); narrative context from Tavily (Phase 4.5). Nothing new to onboard.
- Per-user context or per-user keys — Phase 5 multi-tenant work.

**Time budget:** 3–5 evenings. One new table + Alembic, one thin Finnhub-earnings wrapper, one new graph node with one Sonnet call, a prompt, three audit columns, template + settings + tests.

**Definition of done:** all 11 smoke-test rows pass, *and* you've received at least **two consecutive Sunday weekly-suggestion emails** where at least one suggestion shows a context- or earnings-driven size adjustment with a human-readable reason, *and* the earnings gate has been verified against a real ticker with a confirmed upcoming earnings date (qty shrunk, anchor moved deeper). The adjustments read as sensible risk management — not as hallucinated market calls. Tag: `v0.4.7.0`.

**Depends on:**
- **Phase 4.5** (`v0.4.5.0` code-complete) — `WeeklyMarketContext`, `services/weekly_context.py`, `services/tavily.py`. Phase 4.7 *persists* that context (4.5 deliberately didn't) and consumes it.
- **Phase 3c** (`v0.3.0-phase-3`) — the suggestion-review graph (`graphs/suggestion_review.py`). Phase 4.7 adds one node to it.
- **Phase 3b** (`v0.3b.0`) — the Finnhub client. Phase 4.7 adds an earnings-calendar method alongside the existing news fetcher.

---

## Architecture context — what's new in Phase 4.7

```
  FRIDAY 17:00 ET  (jobs/weekly_review.py — Phase 4.5, extended)
  ┌──────────────────────────┐
  │ build_weekly_market_      │
  │ context()  → Sonnet       │
  └────────────┬─────────────┘
               │ NEW: persist the synthesis
               ▼
  ┌──────────────────────────┐
  │ weekly_market_context     │   append-only table
  │ (week_of, payload_json)   │   one row per Friday run
  └────────────┬─────────────┘
               │
  ===========  Friday → Sunday bridge  ===========
               │
  SUNDAY 18:00 ET  (jobs/weekly_suggestions.py → suggestion-review graph)
               │
        ┌──────▼───────────────────────────────────────────────────────┐
        │ graphs/suggestion_review.py                                   │
        │   gather_context ──┐                                          │
        │     • loads latest weekly_market_context (≤ max_age)          │
        │     • services/earnings.py: fresh Finnhub earnings calendar   │
        │     │              for the watchlist, next-week window         │
        │     ▼                                                         │
        │   reason  (Sonnet — unchanged)                                │
        │     │                                                         │
        │     ▼                                                         │
        │   context_adjust   ◀── NEW NODE                               │
        │     • 4a deterministic earnings gate (Python)                 │
        │     • 4b Sonnet bounded size multiplier (macro/sector)        │
        │     • 4c apply: clamp · re-anchor · drop sub-1-share          │
        │     ▼                                                         │
        │   critic  (Sonnet — sees the adjustments, final cross-set gate)│
        │     ▼                                                         │
        │   route → revise / skip_revise → finalize → END               │
        └───────────────────────────────────────────────────────────────┘
```

Five behaviours to internalize:

1. **Context scales, it never originates.** The gap engine decides *which* tickers and the scored `sr_level` set decides *which* prices. `context_adjust` only multiplies `qty` and may re-pick among *existing* scored levels for that ticker. It cannot add a draft for a ticker the gap engine didn't surface, and it cannot produce a price that isn't already a computed level. This single invariant is what keeps "full bidirectional influence" on the right side of the suggest-only product line — even when the multiplier is > 1, the suggestion was already justified by the allocation gap.

2. **Two signals, two independent failure modes.** The earnings gate (Finnhub, structured, deterministic) and the narrative multiplier (Sonnet over Tavily synthesis, judgmental) are wired so either runs when the other's data source is down. If Friday's Tavily synthesis failed or is stale, the earnings gate still fires (it fetches fresh Sunday). If Finnhub is rate-limited Sunday, the narrative multiplier still applies. Never collapse them into one path.

3. **LLMs judge, Python applies — again.** This is the third instance of ADR-0013's rule (level scoring, critic→revise, now context sizing). The Sonnet pass emits a *bounded* `size_multiplier` plus an optional `prefer_anchor`; deterministic Python clamps it to `[context_size_min, context_size_max]`, validates the anchor against known levels, and applies. The model never writes the final qty or a raw price.

4. **The critic stays the final cross-set gate.** `context_adjust` runs *before* `critic`. Upsizing can create combined cash-floor or concentration problems — those are caught by the existing critic, which already reviews drafts as a set. Do **not** move cash checks into `context_adjust`; that duplicates logic and breaks the single-responsibility split. `context_adjust` sizes per-draft; `critic` reasons across the set.

5. **Persisted context is the Friday→Sunday bridge — and an audit artifact.** Phase 4.5 §8 deliberately discarded Tavily results. Phase 4.7 must persist the *synthesis* (`WeeklyMarketContext`), because (a) the Sunday engine consumes Friday's narrative, and (b) "what context drove this size" belongs in the suggested-vs-filled audit trail (convention #5). We persist the synthesized dataclass as JSON — not raw Tavily result pages.

---

## 0. Pre-flight checklist (~15 minutes)

- [ ] **Phase 4.5 code-complete.** `services/weekly_context.py::build_weekly_market_context` returns a `WeeklyMarketContext`, and `jobs/weekly_review.py` calls it. Confirm `WeeklyMarketContext` is a frozen dataclass (it is, per 4.5 §2) — that makes it safe to serialize and to pass across the session boundary in `gather_context`.
- [ ] **Phase 3c review graph live.** `graphs/suggestion_review.py` has `gather_context → reason → critic → route → revise/skip_revise → finalize`. Phase 4.7 splices `context_adjust` between `reason` and `critic`.
- [ ] **`FINNHUB_API_KEY` set and working** (from Phase 3b). Finnhub's free tier covers `/calendar/earnings`. Quick check: `curl "https://finnhub.io/api/v1/calendar/earnings?from=2026-05-25&to=2026-05-31&token=$FINNHUB_API_KEY"` returns an `earningsCalendar` array.
- [ ] **`make_checkpointer()` still returns `MemorySaver`** (3b/3c constraint — `SqliteSaver` contends with the OLTP write lock). The new node inherits the shared checkpointer; do not switch.
- [ ] **`LLM_DAILY_COST_CAP_USD` ≥ $5.00.** Phase 4.7 adds one Sonnet call per weekly run (~$0.02). Negligible, but confirm headroom over 3a/3c.
- [ ] **At least one watchlist ticker has a confirmed earnings date in the next ~2 weeks**, so smoke row 7 (live earnings gate) is testable. Check an earnings calendar; if none, the gate is correct but unobservable this week.

---

## 1. Persist `WeeklyMarketContext` — new table + Friday write (~half evening)

Phase 4.5's synthesis was render-once-and-discard. The Sunday engine needs it, so persist it as an **append-only event row** (convention #9 — events don't get edited).

### 1a. Model + Alembic

```python
# src/investor/models.py
class WeeklyMarketContextRow(Base):
    __tablename__ = "weekly_market_context"
    id: Mapped[int] = mapped_column(primary_key=True)
    week_of: Mapped[date]                                  # Monday of the week the context covers
    payload_json: Mapped[str] = mapped_column(Text)        # json.dumps(asdict(WeeklyMarketContext))
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    __table_args__ = (Index("ix_wmc_week_of", "week_of"),)  # NOT unique — Friday may re-run
```

No unique constraint: the Friday job can legitimately re-run (misfire grace). The Sunday loader takes the **most recent** row for the week. This is an append-only event table — never UPDATE it.

```bash
uv run alembic revision --autogenerate -m "phase4.7 weekly_market_context"
# eyeball the DDL (batch mode for SQLite), then:
uv run alembic upgrade head
```

> **Convention note (#6/#7):** this is an OLTP table, so it belongs in SQLite/`models.py` — not the analytics tier. It stores *synthesized text*, not bars; no DuckDB, no Parquet. Keep it out of `analytics.py`.

### 1b. Persist + load helpers (`services/weekly_context.py`)

```python
def persist_weekly_context(s: Session, ctx: WeeklyMarketContext) -> None:
    s.add(WeeklyMarketContextRow(
        week_of=ctx.week_of,
        payload_json=json.dumps(asdict(ctx), default=str),
    ))
    s.commit()


def load_latest_weekly_context(
    s: Session, *, week_of: date, max_age_days: int,
) -> WeeklyMarketContext | None:
    """Most recent persisted context for the week, if fresh enough.

    Returns None when nothing is stored or the newest row is older than
    max_age_days — the Sunday engine then degrades to earnings-gate-only.
    """
    row = s.scalars(
        select(WeeklyMarketContextRow)
        .where(WeeklyMarketContextRow.week_of == week_of)
        .order_by(WeeklyMarketContextRow.created_at.desc())
    ).first()
    if row is None:
        return None
    if (datetime.now(UTC) - row.created_at).days > max_age_days:
        log.info("weekly_market_context for %s is stale (>%dd); skipping narrative pass",
                 week_of, max_age_days)
        return None
    data = json.loads(row.payload_json)
    return _weekly_context_from_dict(data)   # rebuild dataclass incl. NewsResult citations
```

`_weekly_context_from_dict` reverses `asdict` — reconstruct the `citations` list back into `NewsResult` objects (the email may re-surface them; tolerate missing keys defensively).

> **Friday→Sunday week alignment.** Phase 4.5 builds context with `week_of = that Friday's week_start`. The Sunday suggestion engine keys on `next_monday()`. Pick **one** convention and make both jobs agree — recommended: both use the *Monday of the upcoming trading week*. If they disagree, the Sunday loader silently finds nothing and you degrade to earnings-only without an error. Pin this down in step 4's smoke test (row 8).

### 1c. Friday write site (`jobs/weekly_review.py`)

```python
market_context = build_weekly_market_context(tavily=tavily, llm=llm,
                                              watchlist=settings.watchlist,
                                              week_of=week_of)
if market_context is not None:
    with session_scope() as s:
        persist_weekly_context(s, market_context)   # NEW
# ... existing email rendering unchanged ...
```

---

## 2. `services/earnings.py` — structured earnings calendar (~half evening)

Why not reuse Tavily's `forward_events`? Because that's free-text Sonnet output ("AAPL reports next week") — too fuzzy to *gate* on. A structured calendar gives `{ticker: date}`, which a deterministic rule can trust. Finnhub already provides it and you're already paying $0 for it.

```python
# src/investor/services/earnings.py
import logging
from datetime import date, timedelta
from typing import Protocol

log = logging.getLogger(__name__)


class EarningsClient(Protocol):
    def upcoming_earnings(
        self, tickers: list[str], *, start: date, end: date,
    ) -> dict[str, date]: ...
    """ticker -> the earliest earnings date within [start, end]; absent if none."""


class FinnhubEarningsClient:
    def __init__(self, api_key: str):
        import finnhub
        self._client = finnhub.Client(api_key=api_key)

    def upcoming_earnings(self, tickers, *, start, end) -> dict[str, date]:
        wanted = set(tickers)
        out: dict[str, date] = {}
        try:
            resp = self._client.earnings_calendar(
                _from=start.isoformat(), to=end.isoformat(), symbol="",
            )
        except Exception as e:                       # network, rate-limit, schema drift
            log.warning("Finnhub earnings calendar failed: %s", e, exc_info=True)
            return {}                                # empty → earnings gate no-ops this week
        for r in resp.get("earningsCalendar", []):
            sym, d = r.get("symbol"), r.get("date")
            if sym in wanted and d:
                try:
                    ed = date.fromisoformat(d)
                except ValueError:
                    continue
                if sym not in out or ed < out[sym]:  # keep the earliest in-window date
                    out[sym] = ed
        return out


class FakeEarningsClient:
    """Tests. Returns canned {ticker: date}."""
    def __init__(self, canned: dict[str, date] | None = None):
        self.calls: list[tuple[tuple[str, ...], date, date]] = []
        self._canned = canned or {}

    def upcoming_earnings(self, tickers, *, start, end):
        self.calls.append((tuple(tickers), start, end))
        return {t: d for t, d in self._canned.items()
                if t in set(tickers) and start <= d <= end}


def make_earnings_client(settings) -> EarningsClient:
    if not settings.finnhub_api_key:
        log.warning("FINNHUB_API_KEY not set; earnings gate will no-op")
        return FakeEarningsClient()
    return FinnhubEarningsClient(api_key=settings.finnhub_api_key)
```

Properties to verify (mirror the Tavily wrapper discipline from 4.5):

- Exception → empty dict + warning. Never raises into the graph.
- Earliest in-window date wins when a vendor returns multiple rows.
- `FakeEarningsClient` is the unit-test substitute; only the integration smoke test hits real Finnhub.

Wire into the lifespan: `app.state.earnings = make_earnings_client(_settings)`, and pass it into the weekly-suggestions job alongside the other deps.

---

## 3. Extend `ReviewContext` + `gather_context` node (~half evening)

Add the two new inputs to the frozen context dataclass and load them inside the existing session block. Both must be **plain data** before the session closes (the DetachedInstanceError rule — Phase 1 Bug 2, 3b Bug 1, 3c §5): `WeeklyMarketContext` is already a frozen dataclass, and `earnings_by_ticker` is `dict[str, date]` primitives, so both are safe.

```python
@dataclass(frozen=True)
class ReviewContext:
    gap_rows: list[GapRow]
    scored_levels: dict[str, list[ScoredLevel]]
    recent_news: dict[str, list[NewsTriageItem]]
    indicators: dict[str, IndicatorRow]
    account: AccountSnapshot
    untracked_positions: list[UntrackedPosition]
    market_context: WeeklyMarketContext | None        # NEW (None ⇒ narrative pass skipped)
    earnings_by_ticker: dict[str, date]                # NEW (empty ⇒ earnings gate no-ops)


def gather_context_node(state, session_factory, *, settings, earnings_client) -> ...:
    week_of = state["week_of"]
    earnings_window_end = week_of + timedelta(days=settings.earnings_lookahead_days)
    earnings_by_ticker = earnings_client.upcoming_earnings(   # fresh Sunday fetch
        get_watchlist(), start=week_of, end=earnings_window_end,
    )
    with session_factory() as s:
        gap_rows      = compute_gap(s)
        scored_levels = load_latest_scored_levels(s)
        recent_news   = load_recent_material_news(s, days=7)
        account       = get_latest_account_snapshot(s)
        untracked     = get_untracked_positions(s)
        market_ctx    = load_latest_weekly_context(           # NEW
            s, week_of=week_of, max_age_days=settings.context_max_age_days,
        )
    indicators = compute_indicators(get_watchlist())
    ctx = ReviewContext(gap_rows=gap_rows, scored_levels=scored_levels,
                        recent_news=recent_news, indicators=indicators,
                        account=account, untracked_positions=untracked,
                        market_context=market_ctx, earnings_by_ticker=earnings_by_ticker)
    return {**state, "context": ctx}
```

> Earnings is fetched **outside** the session (it's an HTTP call, not a DB read) — never hold the OLTP session open across a network call; that's a recipe for write-lock timeouts under the single-writer rule (#8).

---

## 4. `context_adjust` node — the heart of Phase 4.7 (~1.5 evenings)

Three sub-passes, in order. 4a and 4b produce *judgments*; 4c *applies* them deterministically (ADR-0013).

```python
class DraftSizeAdjustment(BaseModel):
    draft_index: int
    size_multiplier: float                  # clamped in 4c; model is asked for [min,max]
    prefer_anchor: str | None = None         # a scored-level method name, or null
    note: str                                # ≤ 200 chars, the "why"


class DraftSizeAdjustments(BaseModel):
    items: list[DraftSizeAdjustment]


def context_adjust_node(state, llm, *, settings) -> SuggestionReviewState:
    ctx = state["context"]
    drafts = state["drafts"]

    # ---- 4a. Deterministic earnings gate (Python, no LLM) ----------------
    earnings_factor: dict[int, float] = {}
    earnings_note:   dict[int, str]   = {}
    earnings_anchor: dict[int, str]   = {}
    for i, d in enumerate(drafts):
        ed = ctx.earnings_by_ticker.get(d.ticker)
        if ed is None:
            continue
        earnings_factor[i] = settings.earnings_size_factor          # default 0.5
        note = f"earnings {ed.isoformat()} in lookahead → size ×{settings.earnings_size_factor:g}"
        if settings.earnings_reanchor:
            deeper = _deeper_anchor(d, ctx.scored_levels.get(d.ticker, []))
            if deeper is not None:
                earnings_anchor[i] = deeper.method
                note += f"; anchor → {deeper.method} ${deeper.price:,.2f}"
        earnings_note[i] = note

    # ---- 4b. Narrative multiplier (Sonnet, bounded) ----------------------
    narrative: dict[int, DraftSizeAdjustment] = {}
    if ctx.market_context is not None:
        system = load_prompt(f"context_size_v{settings.context_adjust_prompt_version}.txt")
        user = json.dumps({
            "drafts": [
                {"index": i, "ticker": d.ticker, "side": d.side,
                 "qty_after_earnings_gate": _apply_factor(d.qty, earnings_factor.get(i, 1.0)),
                 "anchor_method": d.anchor_method,
                 "scored_levels": [{"method": lv.method, "price": lv.price,
                                    "confidence": lv.confidence}
                                   for lv in ctx.scored_levels.get(d.ticker, [])[:5]]}
                for i, d in enumerate(drafts)
            ],
            "macro_summary":  ctx.market_context.macro_summary,
            "sector_summary": ctx.market_context.sector_summary,
            "ticker_catchup": ctx.market_context.ticker_catchup,
            "forward_events": ctx.market_context.forward_events,
            "bounds": {"min": settings.context_size_min, "max": settings.context_size_max},
        }, default=str)
        parsed, tel = llm_node_call(
            purpose="context_size", model=SONNET, system=system, user=user,
            schema=DraftSizeAdjustments,
            fallback_factory=lambda: DraftSizeAdjustments(items=[]), llm=llm,
        )
        narrative = {it.draft_index: it for it in parsed.items}
        state = {**state, "telemetry": {**state["telemetry"], **tel}}

    # ---- 4c. Apply: clamp · combine · re-anchor · drop sub-1-share -------
    adjusted: list[OrderSuggestionRow] = []
    for i, d in enumerate(drafts):
        ef = earnings_factor.get(i, 1.0)
        nf = 1.0
        prefer = None
        notes = []
        if i in earnings_note:
            notes.append(earnings_note[i])
        if i in narrative:
            nf = _clamp(narrative[i].size_multiplier,
                        settings.context_size_min, settings.context_size_max)
            prefer = narrative[i].prefer_anchor
            if narrative[i].note:
                notes.append(narrative[i].note.strip())
        size_factor = ef * nf
        new_qty = round_qty(d.qty * size_factor)

        # anchor precedence: earnings re-anchor (defensive) wins over narrative preference
        new_limit, new_anchor = d.limit_price, d.anchor_method
        chosen_method = earnings_anchor.get(i) or prefer
        if chosen_method:
            lv = _find_level(ctx.scored_levels.get(d.ticker, []), chosen_method, d.side)
            if lv is not None:
                new_limit, new_anchor = lv.price, lv.method      # only ever a known level

        if new_qty < 1:                                          # shrank below 1 share → skip
            log.info("context_adjust dropped %s (qty %.2f→%.2f, factor %.2f): %s",
                     d.ticker, d.qty, new_qty, size_factor, "; ".join(notes))
            continue

        adjusted.append(replace(
            d, qty=new_qty, limit_price=new_limit, anchor_method=new_anchor,
            base_qty=d.qty, size_factor=size_factor,
            context_note="; ".join(notes) or None,
        ))
    return {**state, "drafts": adjusted}
```

Helpers:

- `_apply_factor(qty, f)` / `round_qty` — reuse Phase 2's rounding (respect the fractional-shares flag).
- `_clamp(x, lo, hi)` — defensive even though the prompt asks for in-bounds; never trust the model's arithmetic.
- `_deeper_anchor(draft, levels)` — for a **buy**, the nearest support *below* the current anchor (a deeper dip fills cheaper, ruling out a post-earnings pop); for a **sell/trim**, the nearest resistance *above*. Returns `None` if none deeper — then keep the original anchor (don't fabricate).
- `_find_level(levels, method, side)` — returns the scored level matching `method`, but only if it's the correct *type* for the side (support for buy, resistance for sell). Returns `None` otherwise → keep original. **This is the invariant from Behaviour 1: a price only ever comes from a known scored level.**

> **`OrderSuggestionRow` gains three fields** — `base_qty: float | None = None`, `size_factor: float = 1.0`, `context_note: str | None = None`. Default them so Phase 2/3 construction sites and tests keep compiling. They carry the audit story into the DB (step 6) and the email (step 7).

### 4d. The prompt — `prompts/context_size_v1.txt`

```
You size weekly orders for a long-term US-equity investor. The order
ENGINE has already decided WHICH tickers to trade and at WHICH price
levels, based on a target-allocation gap. Your ONLY job is to scale each
draft's quantity up or down based on this week's market context, and
optionally prefer a different (already-computed) price level.

For each draft, output:
  - size_multiplier: a number within [bounds.min, bounds.max].
      > 1.0  : context is favourable / low-risk — lean in (toward the cap)
      = 1.0  : no clear signal — leave as-is (THIS IS THE DEFAULT)
      < 1.0  : elevated risk (adverse macro, sector stress, event ahead)
               — size down
  - prefer_anchor: the `method` of a level from THIS ticker's scored_levels
      if a more conservative entry is warranted; otherwise null.
  - note: one sentence citing the SPECIFIC context that drove the number
      (e.g. "semis selling off on export-control headlines").

HARD RULES — output is rejected if violated:
- NO price targets. Do not predict where any ticker will go.
- NO buy/sell/hold recommendations. The engine already decided direction;
  you only scale size. You may NOT flip a side or add a ticker.
- prefer_anchor MUST be a method that appears in that draft's
  scored_levels. Never invent a price or a method.
- size_multiplier MUST be within [bounds.min, bounds.max]. If unsure,
  return 1.0 — neutral is always safe.
- NO fundamental claims beyond what the provided context supports. If the
  macro/sector/catchup text doesn't say it, don't assert it.

Output ONLY valid JSON: {"items":[{"draft_index":0,"size_multiplier":1.0,
"prefer_anchor":null,"note":"..."}, ...]}. No preamble, no markdown.
```

Set `settings.context_adjust_prompt_version = "v1"`. The prompt-versioning mechanism already exists from Phase 3a.

### 4e. Graceful degradation matrix

| Situation | Earnings gate (4a) | Narrative pass (4b) | Result |
|---|---|---|---|
| Both sources healthy | fires | fires | full adjustment |
| Tavily failed Friday / context stale | fires | skipped (`market_context is None`) | earnings-only sizing |
| Finnhub down Sunday | no-ops (empty dict) | fires | narrative-only sizing |
| Both down | no-ops | skipped | drafts pass through unchanged (= Phase 3c behaviour) |

The node can never crash the run — worst case it's a no-op and you get exactly the pre-4.7 suggestions.

---

## 5. Wire the node into the graph (~15 min)

Splice `context_adjust` between `reason` and `critic`:

```python
def build_suggestion_review_graph(llm, session_factory, *, settings, earnings_client):
    g = StateGraph(SuggestionReviewState)
    g.add_node("gather_context", lambda s: gather_context_node(
        s, session_factory, settings=settings, earnings_client=earnings_client))
    g.add_node("reason",         lambda s: reason_node(s, llm))
    g.add_node("context_adjust", lambda s: context_adjust_node(s, llm, settings=settings))  # NEW
    g.add_node("critic",         lambda s: critic_node(s, llm))
    g.add_node("revise",         revise_node)
    g.add_node("skip_revise",    skip_revise_node)
    g.add_node("finalize",       lambda s: finalize_node(s, session_factory))
    g.add_edge(START, "gather_context")
    g.add_edge("gather_context", "reason")
    g.add_edge("reason", "context_adjust")            # was: reason → critic
    g.add_edge("context_adjust", "critic")            # NEW
    g.add_conditional_edges("critic", route_after_critic,
                            {"revise": "revise", "skip_revise": "skip_revise"})
    g.add_edge("revise", "finalize")
    g.add_edge("skip_revise", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=CHECKPOINTER)
```

In `jobs/weekly_suggestions.py`, pass the new dependency:

```python
def run_weekly_suggestions(settings, adapter, emailer, llm, earnings_client):
    # ... bars / indicators / scored_levels / snapshot / gap / drafts (unchanged) ...
    graph = build_suggestion_review_graph(
        llm, session_scope, settings=settings, earnings_client=earnings_client)
    result = graph.invoke({...})        # unchanged invoke contract
    # email uses result["finals"]; rationales unchanged
```

> **Ordering matters.** `reason` writes per-draft rationales keyed by `draft_index`. `context_adjust` may *drop* a draft (sub-1-share). Re-index defensively: after `context_adjust`, the surviving drafts' original indices may have gaps. Either (a) keep the original index on each row and have `critic`/email look up rationales by a stable id rather than list position, or (b) re-key `rationales` in `context_adjust` when you drop a draft. Recommended: carry a stable `draft_id` on `OrderSuggestionRow` from creation and key everything off it — list-position keying is a latent bug the moment a node filters the list. Add this to the smoke tests (row 9).

---

## 6. Critic awareness of the adjustment (~half evening)

The critic must *see* that sizing was already adjusted, so it doesn't naively undo a defensive shrink or rubber-stamp a risky upsize. Add to the critic's `user` payload:

```python
"drafts_with_rationales": [
    {..., "base_qty": d.base_qty, "size_factor": d.size_factor,
     "context_note": d.context_note, "qty": d.qty, ...}
    for ...
],
"market_context": {
    "macro_summary":  ctx.market_context.macro_summary if ctx.market_context else None,
    "sector_summary": ctx.market_context.sector_summary if ctx.market_context else None,
},
"earnings_by_ticker": {t: d.isoformat() for t, d in ctx.earnings_by_ticker.items()},
```

Add to `prompts/suggestion_critic_v1.txt` (bump to `v2`):

```
6. Sizing already adjusted: each draft may carry size_factor and
   context_note showing a context/earnings adjustment already applied
   (size_factor < 1 = defensively shrunk; > 1 = leaned in). RESPECT these.
   Only override (REVISE qty) if the adjustment created a NEW problem —
   e.g. combined BUYs now breach the cash_floor after an upsize, or a
   ticker with earnings this week was upsized. Do NOT undo a defensive
   shrink without a specific reason in your `reason` field.
```

The critic's existing REVISE→`_apply_changes` path already clamps `qty` and validates anchors against scored levels, so no new apply logic is needed here.

---

## 7. Audit columns + email (~half evening)

### 7a. `order_suggestion` columns

```bash
uv run alembic revision --autogenerate -m "phase4.7 order_suggestion context audit cols"
```

Adds (all nullable / defaulted, so history and idempotency are untouched):

| Column | Type | Meaning |
|---|---|---|
| `base_qty` | float, null | qty the gap engine produced *before* context adjustment |
| `size_factor` | float, default 1.0 | combined earnings × narrative multiplier actually applied |
| `context_note` | varchar, null | human-readable "why" (earnings date, macro/sector driver) |

`persist_suggestions()` writes them. The "never overwrite a non-pending row" semantic (Phase 2 §4b) is unchanged — these are just three more fields on the insert/refresh path.

### 7b. Weekly suggestions email

In `templates/weekly_suggestions.html.j2`, surface the adjustment so the change is legible at a glance:

```jinja
<td>
  {{ s.qty }} sh
  {% if s.size_factor and s.size_factor != 1.0 %}
    <span style="color:#888; font-size:12px;">
      (base {{ s.base_qty }} · ×{{ "%.2f"|format(s.size_factor) }})
    </span>
  {% endif %}
</td>
...
{% if s.context_note %}
<div style="font-size:12px; color:#666;">{{ s.context_note }}</div>
{% endif %}
```

Mirror in `templates/weekly_suggestions.txt.j2`. A reader should see, e.g., *"NVDA — buy 4 sh (base 8 · ×0.50) — earnings 2026-05-28 in lookahead → size ×0.5; anchor → swing_low_5bar $103.20."*

---

## 8. Settings + env (~15 min)

Add to `config.py` (`pydantic-settings`), all with defaults so nothing breaks if unset:

```python
earnings_size_factor: float = 0.5         # qty multiplier when earnings in lookahead
earnings_reanchor: bool = True            # re-anchor to a deeper level on earnings
earnings_lookahead_days: int = 7          # window from week_of
context_size_min: float = 0.25            # lower clamp on narrative multiplier
context_size_max: float = 1.5             # upper clamp (full bidirectional influence)
context_max_age_days: int = 4             # Fri synthesis must be ≤ this old on Sun
context_adjust_prompt_version: str = "v1"
```

`FINNHUB_API_KEY` already exists (Phase 3b). No new secrets. Document the new knobs in `.env.example` comments (they're settings, not secrets, but list them for discoverability).

> **Why `context_size_max = 1.5`, not unbounded.** Jane chose full bidirectional influence, so the multiplier *can* exceed 1.0 — but an unbounded upsize lets a single favourable-sounding week blow past the gap-driven sizing and breach concentration sense. 1.5 is a starting cap; widen it once you've watched a few weeks. The clamp in 4c is the hard backstop regardless of what the model returns.

---

## 9. Smoke-test checklist (Phase 4.7 done when all green)

| # | Step | Pass criteria |
|---|---|---|
| 1 | `uv run pytest tests/test_earnings.py` | Wrapper tests pass — exception→empty-dict, earliest-in-window selection, `FakeEarningsClient` records calls |
| 2 | `uv run pytest tests/test_context_adjust.py` — earnings gate | A draft whose ticker is in `earnings_by_ticker` gets `qty × earnings_size_factor` and (if a deeper level exists) a deeper anchor; `context_note` records the earnings date |
| 3 | Narrative multiplier clamps | Sonnet returns `size_multiplier = 9.9`; applied factor is clamped to `context_size_max` (1.5). Returns `0.01`; clamped to `context_size_min` |
| 4 | Bidirectional | A draft with a favourable narrative gets `size_factor > 1.0` and a larger qty; an adverse-sector draft gets `< 1.0` |
| 5 | Sub-1-share drop | A small base qty × 0.5 that rounds below 1 share is dropped from `drafts` with an INFO log (effective skip) |
| 6 | Price invariant | Assert every emitted `limit_price` equals some `scored_levels[ticker]` price for the correct side. Inject a narrative `prefer_anchor` of a *resistance* method on a *buy* draft → it's ignored, original anchor kept |
| 7 | **Live earnings gate** | With a real watchlist ticker confirmed to report in the next 7 days, a manual `POST /admin/run-weekly-suggestions` produces a shrunk + re-anchored suggestion for it |
| 8 | Friday→Sunday round-trip | `persist_weekly_context` Friday → `load_latest_weekly_context` Sunday returns the same synthesis; week_of keys agree across both jobs; a row older than `context_max_age_days` returns `None` (narrative pass skipped) |
| 9 | Rationale re-keying | After `context_adjust` drops a draft, the email shows each surviving suggestion's correct rationale (no off-by-one) — proves stable-id keying, not list position |
| 10 | Graceful degradation | (a) `FINNHUB_API_KEY=""` → earnings gate no-ops, narrative still applies; (b) no persisted context → narrative skipped, earnings still applies; (c) both off → drafts unchanged vs. Phase 3c baseline |
| 11 | Critic respects adjustment | Critic prompt v2: a defensively-shrunk draft is not silently re-inflated; an upsize that breaches `cash_floor` across the BUY set is REVISED down |
| — | `uv run pytest` overall | Total above the post-4.5 count (264) + new tests; `ruff check src/ tests/` and `mypy src/` clean |
| — | **Two consecutive Sunday emails** | Each shows ≥ 1 suggestion with a sensible context/earnings adjustment and a legible `context_note` — risk management, not a market call |

Tag:

```bash
git add -A
git commit -m "phase 4.7: context-aware weekly order sizing (earnings gate + bounded narrative multiplier)"
git tag v0.4.7.0
git push --tags
```

---

## 10. Common Phase 4.7 pitfalls

1. **List-position keying after a node filters the list.** `context_adjust` can drop drafts; any code that keys rationales/decisions by list index breaks the moment that happens. Carry a stable `draft_id` from draft creation and key everything off it. Smoke row 9 enforces.
2. **Week-of mismatch between Friday and Sunday.** If `weekly_review` and `weekly_suggestions` compute `week_of` differently, the Sunday loader silently finds nothing and you degrade to earnings-only with *no error*. Pin both to "Monday of the upcoming trading week" and assert it (row 8).
3. **Holding the OLTP session open across the Finnhub call.** Fetch earnings *before/outside* `with session_scope()`. A network call inside the session risks write-lock timeouts under the single-writer rule (#8).
4. **Trusting the model's arithmetic.** Always `_clamp` the multiplier in Python even though the prompt says "within bounds." The clamp is the contract; the prompt is a request.
5. **Re-anchoring to a level on the wrong side.** A buy must only ever re-anchor to a *support*, a sell to a *resistance*. `_find_level` enforces the side; without it the model could "prefer" a resistance method on a buy and you'd place a limit above the market. Row 6 catches this.
6. **Finnhub earnings dates are estimates and revise.** A "2026-05-28" earnings date can shift. Fetching fresh each Sunday (not caching Friday) keeps it current. Don't persist earnings dates and reuse them across weeks.
7. **Free-tier rate limits during dev iteration.** Finnhub is 60 req/min; a tight test loop over a long watchlist can trip it. Use `FakeEarningsClient` for unit loops; hit real Finnhub only in row 7's integration test and the production cron.
8. **Upsizing quietly draining cash.** `context_adjust` does *not* check cash — by design (Behaviour 4). If you ever see the BUY set exceed available cash after an upsize, that's the *critic's* job to catch (row 11), not a bug in `context_adjust`. Resist the urge to add a cash check in two places.
9. **Stale-context silent skip looks like "context did nothing."** When `context_max_age_days` trips, the narrative pass is skipped with an INFO log, not a warning. If you're debugging "why no adjustment," check the log for the staleness line before assuming the node is broken.

---

## 11. ADRs to write in Phase 4.7

- **`docs/adr/0021-context-aware-order-sizing.md`** — new. Three decisions to record:
  1. **Amend the "context never feeds the suggestion engine" rule.** Phase 4.5 §8 and the CLAUDE.md "things to never do" line banned this outright. Phase 4.7 deliberately reverses it, *bounded*: context may scale `qty` within `[context_size_min, context_size_max]` and re-pick among existing scored levels, but may never originate a ticker, flip a side, or invent a price. Document *why this stays suggest-only*: the suggestion was already justified by the allocation gap; context only sizes it, and execution remains manual (or the separately-gated Phase 4.6 auto-trade on accepted rows). Record that Jane, as owner, chose full bidirectional influence with eyes open, and that the clamp + the price-from-scored-levels invariant are the hard backstops.
  2. **Expand the LLM's allowed outputs.** The CLAUDE.md rule "never let the LLM emit price targets, fundamental claims, or trade recommendations" gains a carved exception: *a bounded position-size multiplier on an already-gap-justified draft.* This is not a price target and not a new recommendation. Note the boundary explicitly so a future agent doesn't read the multiplier as license to widen LLM authority elsewhere.
  3. **Earnings via structured Finnhub calendar, not Tavily forward-events.** Deterministic gate needs structured `{ticker: date}`; Tavily's free-text `forward_events` is for human reading only. Records the two-signal / two-failure-mode split (Behaviour 2).
- Update the **ADR index** with the 0021 entry.

About 45 minutes.

---

## 12. Documentation drift to fix

- **CLAUDE.md:**
  - In *"Things to never do"*, **replace** the absolute Tavily-ban line (added per Phase 4.5 §7) with the bounded rule: *"Context (Tavily synthesis + earnings calendar) may scale weekly-suggestion quantities within `[context_size_min, context_size_max]` and re-pick among existing scored levels — via the `context_adjust` node only. It may never originate a ticker, flip a side, or invent a price. See ADR-0021."*
  - Amend the *"never let the LLM emit … trade recommendations"* line to carve out the bounded size multiplier (ADR-0021).
  - Add to *repo layout*: `services/earnings.py`, the `context_adjust` node in `graphs/suggestion_review.py`, `prompts/context_size_v1.txt`.
  - Add to *storage convention #6/#9*: `weekly_market_context` as a new append-only OLTP table.
  - Add the new `order_suggestion` columns (`base_qty`, `size_factor`, `context_note`) to the schema notes.
  - Add a *common gotcha*: the Friday→Sunday week-of alignment + stale-context silent skip.
  - Add the new settings to *required/optional env* discoverability list.
- **`product_plan.md`:** add a **Phase 4.7 — Context-aware weekly order sizing** entry (mark code-complete with the standard "tag deferred until 2 observed Sunday emails" pattern, earliest tag date = second Sunday after merge). Update the Phase 4.5 §8 note that said Tavily-driven suggestion generation is out of scope — point it at 4.7 and clarify the *bounded* nature.
- **`.env.example`:** list the new `context_*` / `earnings_*` settings as comments (they're settings, not secrets).

---

## 13. What Phase 4.7 deliberately does not include

- **Context-originated suggestions.** Context can only scale gap-driven drafts. A favourable macro week does not *create* a buy for an on-target ticker. (Behaviour 1.) If you ever want context to surface *new* ideas, that's a different product — and a different ADR — well beyond the suggest-only line.
- **Intraday / event-day re-sizing.** Sizing is decided once, Sunday. If a mid-week earnings surprise hits, the GTC order placed Monday (per the post-Phase-4 GTC update) stays as sized; the next Sunday's run re-prices. Real-time re-sizing is Phase 6 territory.
- **Persisting earnings dates.** Fetched fresh each Sunday and used transiently; the *triggering date* is recorded in `context_note` for audit, but there's no `earnings_event` table. Add one only if you later want retrospective "did earnings-gated weeks fill better?" analysis.
- **Tuning the multiplier from outcomes.** Phase 4.7 ships fixed bounds and a fixed earnings factor. A feedback loop that learns the right multiplier from `suggested-vs-filled` history is a Phase 5+ idea, and a good one — the audit columns added here (`base_qty`, `size_factor`) are exactly the data it would need.
- **Per-user context or per-user earnings keys.** Phase 5 multi-tenant work.

---

*When all 11 smoke-test rows are green, the earnings gate has been verified against a real upcoming-earnings ticker, and you've received two consecutive Sunday weekly-suggestion emails where context/earnings adjustments read as sensible risk management (not market calls), Phase 4.7 is done. Tag `v0.4.7.0`, then proceed to Phase 5.*
