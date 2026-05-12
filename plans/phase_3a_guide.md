# Phase 3a — Foundational LLM + Scored Levels + Accept/Reject: Step-by-Step Guide

**Goal:** Three deliverables. (1) Build the shared LLM client wrapper that Phases 3b and 3c also depend on — cost guard, JSON-schema validation, daily call-log persistence, model-pinning convention. (2) Replace the mechanical "nearest level" anchor selection in Phase 2's suggestion engine with LLM-scored confidence weights computed by Claude Sonnet 4.6. (3) Ship the suggestion accept/reject endpoint plus HMAC-signed magic-link buttons in the weekly email so the `order_suggestion.status` column finally mutates.

**Out of scope for 3a:** LangGraph (introduced in 3b), news triage (3b), the multi-step suggestion review pipeline (3c), Moomoo adapter (Phase 4).

**Time budget:** 9–12 evenings (~2 weeks).

**Definition of done:** all 11 smoke-test rows pass, *and* you've received one Sunday weekly suggestions email where the limit prices reflect LLM confidence scores rather than nearest-distance picks, *and* you've clicked Accept on one suggestion and seen the row mutate to `status='accepted'` in SQLite.

**Depends on:** Phase 2 (`v0.2.0-phase-2`) tagged.

---

## Architecture context — what's new in Phase 3a

Phase 3a adds the first LLM-driven layer to the system, plus the first mutation-endpoint flow. None of these change the three-tier storage architecture — they add new columns on `sr_level` and `order_suggestion`, one new table (`llm_call_log`), and one new service (`services/llm.py`).

```
                                              ┌─────────────────────┐
                                              │  Anthropic API      │
                                              │  Claude Sonnet 4.6  │
                                              │  (level scoring)    │
                                              └──────────┬──────────┘
                                                         │
                                              ┌──────────▼──────────┐
                                              │ services/llm.py     │
                                              │ • cost guard        │
                                              │ • JSON-schema       │
                                              │ • retry policy      │
                                              │ • call log          │
                                              └──────────┬──────────┘
                                                         │
                                                         ▼
                                              ┌─────────────────────┐
                                              │ services/           │
                                              │   llm_levels.py     │
                                              │ Sonnet single-call  │
                                              │ structured output   │
                                              └──────────┬──────────┘
                                                         │ confidence ⊕ rationale
                                                         ▼
                                              ┌─────────────────────┐
                                              │ services/suggest.py │
                                              │ select_anchor()     │
                                              │ prefers             │
                                              │ high-confidence-    │
                                              │ within-band         │
                                              └──────────┬──────────┘
                                                         │ drafts → /suggestions
                                                         ▼
                       ┌─────────────────────────────────────────────────────────────────┐
                       │  main.py: PATCH /suggestions/{id}  +  GET /suggestions/{id}/…   │
                       │  • PATCH for programmatic use (X-Admin-Token)                   │
                       │  • GET for one-click email magic links (HMAC-signed, time-bound)│
                       └─────────────────────────────────────────────────────────────────┘
```

Three behaviours to internalize:

1. **The LLM is allowed to score existing computed levels and write a one-line rationale; it is never allowed to invent prices, fundamental claims, or trade recommendations.** Any output that violates a node's JSON schema is rejected; the suggestion engine falls back to nearest-distance. Hardcoded fallback path, not graceful degradation hope.
2. **HMAC-signed magic links are single-use, time-bound, and verify against a server-side secret separate from `ADMIN_TOKEN`.** Two distinct trust domains: `ADMIN_TOKEN` is bearer auth on admin endpoints, `MAGIC_LINK_SECRET` is a signing key.
3. **Phase 3a sets the rules but does not introduce LangGraph yet.** That arrives in 3b. The `services/llm.py` wrapper is designed so it can be used both standalone (here in 3a) and as a node-level helper in 3b/3c without rework.

---

## 0. Pre-flight checklist (~30 minutes)

- [ ] Phase 2 (`v0.2.0-phase-2`) tagged. If the first Sunday weekly suggestions email wasn't sensible, that's a Phase 3a blocker — workstream A in this phase is the fix.
- [ ] **Anthropic API key** generated and stored. `ANTHROPIC_API_KEY=` in `.env`. Console at `https://console.anthropic.com` — set a $50/month spend cap.
- [ ] **HMAC secret for magic links.** `MAGIC_LINK_SECRET=$(openssl rand -hex 32)` in `.env` and `.env.example`. Distinct from `ADMIN_TOKEN`.
- [ ] **Phase 2 smoke tests still pass.** `uv run pytest -m "not integration"` clean.
- [ ] **Cash-buffer invariant test exists** (Phase 2 cleanup carryover): confirm `tests/test_gap.py` has an explicit assertion that with $100k equity / $5k cash / targets summing to 95, every `gap_pct == 0.0`. Add if missing.

---

## 1. `services/llm.py` — the shared LLM client (~1 evening)

This wrapper is built once and used by 3a, 3b, and 3c. It enforces a daily cost cap, validates LLM output against pydantic schemas, and logs every call to `llm_call_log` for audit.

### 1a. The client

```python
# src/investor/services/llm.py
import hashlib, time, logging
import anthropic
from anthropic.types import Message
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

HAIKU = "claude-haiku-4-5"          # used in 3b
SONNET = "claude-sonnet-4-6"        # used here for level scoring

class LLMResponse(BaseModel):
    content: str
    model: str
    prompt_hash: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int


class LLMClient:
    def __init__(self, api_key: str, daily_cost_cap_usd: float = 5.0):
        self._client = anthropic.Anthropic(api_key=api_key)
        self._daily_cap = daily_cost_cap_usd
        self._spent_today: float = 0.0

    def call(
        self, *,
        model: str, system: str, user: str,
        max_tokens: int = 2048,
        response_schema: type[BaseModel] | None = None,
    ) -> tuple[LLMResponse, BaseModel | None]:
        if self._spent_today >= self._daily_cap:
            raise RuntimeError(f"daily LLM cost cap ${self._daily_cap} reached")

        prompt_hash = hashlib.sha256((system + user).encode()).hexdigest()[:12]
        t0 = time.monotonic()
        msg: Message = self._client.messages.create(
            model=model, system=system,
            messages=[{"role": "user", "content": user}],
            max_tokens=max_tokens,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        content = msg.content[0].text
        cost = _calc_cost(model, msg.usage.input_tokens, msg.usage.output_tokens)
        self._spent_today += cost

        resp = LLMResponse(
            content=content, model=model, prompt_hash=prompt_hash,
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
            cost_usd=cost, latency_ms=latency_ms,
        )

        parsed: BaseModel | None = None
        if response_schema is not None:
            try:
                parsed = response_schema.model_validate_json(content)
            except ValidationError as e:
                log.warning("LLM output failed schema: %s; raw=%r", e, content[:500])
                return resp, None
        return resp, parsed


def _calc_cost(model: str, in_toks: int, out_toks: int) -> float:
    rates = {
        HAIKU:  {"in": 1.00 / 1_000_000, "out":  5.00 / 1_000_000},
        SONNET: {"in": 3.00 / 1_000_000, "out": 15.00 / 1_000_000},
    }[model]
    return in_toks * rates["in"] + out_toks * rates["out"]
```

Three principles:

- **JSON-schema validation is the safety boundary.** Every call returns `(LLMResponse, parsed_or_None)`. If `parsed` is None, the caller treats the call as failed and falls back to deterministic logic. Never trust the content string directly.
- **Daily cost cap is a hard limit.** Better to silently degrade to deterministic behavior than to wake up to a $400 bill from a retry loop.
- **`prompt_hash` is for the audit trail.** Persisted to `llm_call_log` (§1b). When you wonder "why did the engine suggest this last Tuesday," grep by hash.

### 1b. `llm_call_log` table via Alembic

```python
class LLMCallLog(Base):
    __tablename__ = "llm_call_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    purpose: Mapped[str]                              # e.g. "score_levels"
    model: Mapped[str]
    prompt_hash: Mapped[str]
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    cost_usd: Mapped[float]
    latency_ms: Mapped[int]
    status: Mapped[str]                               # "ok" | "schema_error" | "api_error"
    error: Mapped[str | None] = mapped_column(default=None)
```

`uv run alembic revision --autogenerate -m "phase3a llm_call_log"` then `upgrade head`.

Caller persists a row after each `llm.call()`. Retention: forever — entries are ~200 bytes each.

### 1c. Prompt versioning convention

Prompts live in `src/investor/prompts/<purpose>_v<N>.txt`. Bump `N` when a prompt changes meaningfully. The active version is named in `Settings` (`LEVEL_PROMPT_VERSION=1` initially). This makes A/B comparison possible later and keeps the audit trail readable.

---

## 2. Workstream A — LLM-scored levels (~1 week)

### 2a. Alembic migration

```python
def upgrade():
    with op.batch_alter_table("sr_level") as b:
        b.add_column(sa.Column("confidence", sa.Float(), nullable=True))
        b.add_column(sa.Column("llm_rationale", sa.Text(), nullable=True))
        b.add_column(sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("scored_by_model", sa.String(), nullable=True))
        b.add_column(sa.Column("prompt_version", sa.String(), nullable=True))
    with op.batch_alter_table("order_suggestion") as b:
        b.add_column(sa.Column("confidence_at_creation", sa.Float(), nullable=True))
```

The `order_suggestion.confidence_at_creation` snapshot makes retrospectives possible: "of suggestions with confidence ≥ 0.7, what fraction filled at or better than limit?"

### 2b. `services/llm_levels.py`

```python
@dataclass(frozen=True)
class ScoredLevel:
    method: str
    price: float
    confidence: float                    # 0.0 – 1.0
    rationale: str                       # one sentence

class ScoredLevelOut(BaseModel):
    method: str
    confidence: float
    rationale: str

class LevelScoreSchema(BaseModel):
    levels: list[ScoredLevelOut]


def score_levels_for_ticker(
    *, llm: LLMClient, ticker: str,
    computed_levels: list[SRLevelRow], price_history: pd.DataFrame,
) -> list[ScoredLevel]:
    system = load_prompt(f"score_levels_v{settings.level_prompt_version}.txt")
    user = json.dumps({
        "ticker": ticker,
        "current_price": float(price_history["close"].iloc[-1]),
        "recent_bars_60d": price_history.tail(60)[
            ["date","open","high","low","close","volume"]
        ].to_dict(orient="records"),
        "computed_levels": [
            {"method": lv.method, "price": lv.price, "type": lv.type, "as_of": str(lv.as_of)}
            for lv in computed_levels
        ],
    }, default=str)

    resp, parsed = llm.call(
        model=SONNET, system=system, user=user,
        max_tokens=1500, response_schema=LevelScoreSchema,
    )
    persist_llm_call_log(resp, purpose="score_levels",
                         status="ok" if parsed else "schema_error")
    if parsed is None:
        log.warning("level scoring failed for %s; engine will fall back to nearest", ticker)
        return []

    out: list[ScoredLevel] = []
    for entry in parsed.levels:
        match = next((lv for lv in computed_levels if lv.method == entry.method), None)
        if not match:                                  # LLM invented a method? drop it.
            continue
        out.append(ScoredLevel(
            method=entry.method, price=match.price,
            confidence=max(0.0, min(1.0, entry.confidence)),
            rationale=entry.rationale[:240],
        ))
    return out
```

Defenses against LLM misbehaviour:

- LLM must reference levels by `method` string. Any unknown `method` is dropped — it cannot invent a new level with a fabricated price.
- `confidence` is clamped to `[0, 1]` regardless of model output.
- `rationale` is truncated to 240 characters.
- Schema-validation failure → empty result → engine falls back to nearest-distance.

### 2c. The scoring prompt (`prompts/score_levels_v1.txt`)

```
You are a technical-analysis assistant evaluating support and resistance levels
for a US equity. The user is a long-term investor placing limit orders.

You will receive:
- The ticker, the current price, and 60 days of recent OHLCV bars.
- A list of mechanically-computed S/R levels with method names like
  "pivot_weekly_S1", "sma_50", "sma_200", "swing_low_5bar".

Your job: for EACH provided level, return a confidence score (0.0 to 1.0) and
a one-sentence rationale.

Hard rules:
- You MUST only score levels that appear in `computed_levels`. Do NOT invent
  new prices, methods, or levels.
- You MUST NOT recommend buying or selling.
- You MUST NOT make claims about company fundamentals, news, earnings, or
  future prices.
- Your rationale must reference only what's visible in the OHLCV history
  (e.g., "tested twice in last 30 days as support", "confluence with
  rising 50-SMA", "untested since gap-down on day 12").

Output format — return ONLY valid JSON, no preamble:
{
  "levels": [
    {"method": "<exact method string>", "confidence": 0.0-1.0, "rationale": "<1 sentence>"},
    ...
  ]
}

Confidence rubric:
  0.0–0.3: weak / untested / contradicted by recent action
  0.4–0.6: plausible but unconfirmed
  0.7–0.9: tested multiple times, confluence, recent action respects it
  0.9+:    very strong (rare; reserve for clear multi-touch confluence)
```

### 2d. Refactor `services/suggest.py` — confidence-weighted anchor

```python
def select_anchor(
    levels: list[ScoredLevel], current_price: float,
    *, max_distance_pct: float = 8.0, min_confidence: float = 0.4,
) -> ScoredLevel | None:
    in_band = [
        lv for lv in levels
        if abs((lv.price / current_price - 1) * 100) <= max_distance_pct
    ]
    if not in_band:
        return None
    scored = [lv for lv in in_band if lv.confidence >= min_confidence]
    if scored:
        return max(scored, key=lambda lv: lv.confidence)
    # fall back to nearest-distance
    return min(in_band, key=lambda lv: abs(lv.price - current_price))
```

The fallback path matters: when LLM fails or all confidences are below threshold, the system degrades to Phase 2 behavior. Not to silence.

When persisting an `OrderSuggestion`, snapshot the anchor's confidence into `confidence_at_creation`.

### 2e. Wire into weekly job

In `jobs/weekly_suggestions.py`:

```python
def run_weekly_suggestions(settings, adapter, emailer, llm):
    update_bars(settings.watchlist)
    indicators = compute_indicators(settings.watchlist)

    # NEW: score levels via LLM before generating suggestions
    scored_levels = {
        t: score_levels_for_ticker(llm=llm, ticker=t,
                                   computed_levels=compute_levels_for(t),
                                   price_history=load_bars(t))
        for t in settings.watchlist
    }

    with session_scope() as s:
        take_snapshot(adapter, s)
        gap_rows = compute_gap(s)
        suggestions = generate_suggestions(gap_rows, scored_levels, ...)
        persist_suggestions(s, suggestions, ...)

    # email (Phase 2 logic unchanged)
    ...
```

### 2f. Update ADRs 0006 and 0007 — partial

Both ADRs were marked ⚠ Pending LLM Review in Phase 2. Phase 3a partially closes them:

- **ADR-0006 (S/R methodology):** add a section "Scoring pass (Phase 3a)" describing the Sonnet-driven confidence assignment. Keep the ⚠ flag on the *anchor selection* aspect — that's revisited in 3c when the suggestion-review graph adds another reasoning layer.
- **ADR-0007 (position sizing):** sizing rule unchanged; only anchor selection changed. Note the change; keep the ⚠ flag for the same reason.

3c will fully remove the ⚠ flags.

---

## 3. Workstream B — Accept/reject endpoint (~2 days)

### 3a. Alembic migration

```python
def upgrade():
    with op.batch_alter_table("order_suggestion") as b:
        b.add_column(sa.Column("acted_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("note", sa.Text(), nullable=True))
```

### 3b. `PATCH /suggestions/{id}` — programmatic mutation

```python
class StatusUpdate(BaseModel):
    status: Literal["accepted", "rejected", "expired"]
    note: str | None = None

@app.patch("/suggestions/{sid}", dependencies=[Depends(admin_auth)])
def update_suggestion_status(sid: int, body: StatusUpdate):
    with session_scope() as s:
        sug = s.get(OrderSuggestion, sid)
        if not sug:
            raise HTTPException(404)
        if sug.status != "pending":
            raise HTTPException(409, f"already {sug.status}")
        if sug.expires_at < datetime.now(UTC) and body.status != "expired":
            raise HTTPException(409, "suggestion has expired")
        sug.status = body.status
        sug.acted_at = datetime.now(UTC)
        sug.note = body.note
        s.commit()
        return {"id": sid, "status": sug.status}
```

Tests: 200 on valid pending → accepted; 409 on already-acted; 409 on expired; 404 on missing.

### 3c. HMAC-signed magic links — one-click email

```python
import hmac, hashlib, time

def sign_action(sid: int, action: str, secret: str, ttl_seconds: int = 7 * 24 * 3600) -> str:
    expires = int(time.time()) + ttl_seconds
    msg = f"{sid}:{action}:{expires}".encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:32]
    return f"{expires}.{sig}"

def verify_action(sid: int, action: str, token: str, secret: str) -> bool:
    try:
        expires_s, sig = token.split(".", 1)
        expires = int(expires_s)
    except (ValueError, IndexError):
        return False
    if expires < int(time.time()):
        return False
    msg = f"{sid}:{action}:{expires}".encode()
    expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expected)


@app.get("/suggestions/{sid}/{action}")
def click_action(sid: int, action: Literal["accept", "reject"], token: str = Query(...)):
    if not verify_action(sid, action, token, settings.magic_link_secret):
        raise HTTPException(400, "invalid or expired link")
    new_status = "accepted" if action == "accept" else "rejected"
    with session_scope() as s:
        sug = s.get(OrderSuggestion, sid)
        if sug is None:
            raise HTTPException(404)
        if sug.status != "pending":
            raise HTTPException(409, f"already {sug.status}")
        sug.status = new_status
        sug.acted_at = datetime.now(UTC)
        s.commit()
    return HTMLResponse(f"<h2>Suggestion #{sid} {new_status}.</h2>")
```

Properties:

- Single-use enforced by `status != "pending"` check.
- Time-bound — token expires when the suggestion expires (Friday EOD of the suggestion week).
- Constant-time signature comparison via `hmac.compare_digest`.
- Action is part of the signed payload — accept-signed token cannot be reused on the reject path.

### 3d. Email template buttons

In `templates/weekly_suggestions.html.j2`, each suggestion row gets two inline links. Compute the tokens once per row in `compose_weekly_email`:

```python
for s in suggestions:
    s.accept_token = sign_action(s.id, "accept", settings.magic_link_secret)
    s.reject_token = sign_action(s.id, "reject", settings.magic_link_secret)
```

```jinja
<a href="{{ base_url }}/suggestions/{{ s.id }}/accept?token={{ s.accept_token }}"
   style="background: #28a745; color: white; padding: 6px 12px; text-decoration: none;">
  Accept</a>
<a href="{{ base_url }}/suggestions/{{ s.id }}/reject?token={{ s.reject_token }}"
   style="background: #6c757d; color: white; padding: 6px 12px; text-decoration: none;">
  Reject</a>
```

### 3e. Tests

- Tampering one byte of a token → 400 invalid/expired.
- Expired token (mock time) → 400.
- Valid accept → 200, `status='accepted'`, `acted_at` populated.
- Second click on same link → 409 already-acted.
- Accept-signed token used on `/reject` path → 400.

---

## 4. Smoke-test checklist (Phase 3a done when all green)

| # | Step | Pass criteria |
|---|---|---|
| 1 | `uv run alembic upgrade head` | `sr_level.confidence`, `sr_level.llm_rationale`, `sr_level.scored_at`, `sr_level.scored_by_model`, `sr_level.prompt_version`, `order_suggestion.confidence_at_creation`, `order_suggestion.acted_at`, `order_suggestion.note`, `llm_call_log` all exist |
| 2 | `uv run pytest -m "not integration"` | All tests pass in < 15 s; total ≥ 65 |
| 3 | Cash-buffer invariant test exists and passes | `tests/test_gap.py::test_cash_buffer_invariant` green |
| 4 | `curl -H "X-Admin-Token: …" -X POST localhost:8000/admin/run-weekly-suggestions` | 200; `sr_level` rows have `confidence` populated; `llm_call_log` has new "score_levels" rows |
| 5 | `sqlite3 data/investor.db "SELECT method, confidence, llm_rationale FROM sr_level ORDER BY scored_at DESC LIMIT 10"` | Confidences in [0, 1]; rationales human-readable; method strings match the deterministic computed ones (no invention) |
| 6 | `sqlite3 data/investor.db "SELECT confidence_at_creation FROM order_suggestion ORDER BY id DESC LIMIT 10"` | Non-null for new suggestions |
| 7 | LLM cost guard test: set `daily_cost_cap_usd=0.01` and run scoring | Engine logs "cap reached"; suggestions still generated using nearest-distance fallback |
| 8 | LLM schema-breach simulation | Logged as `schema_error` in `llm_call_log`; suggestion falls back to nearest |
| 9 | Manual: tamper one byte of an HMAC token in a weekly email link | Server returns 400; no mutation |
| 10 | Click an `Accept` link in a real weekly email | Page returns 200 with simple confirmation; row mutates; `acted_at` populated. Second click → 409 |
| 11 | One Sunday email observed with confidence-driven anchors | Read it; the limit prices are no longer mechanically nearest; at least one suggestion's `reason` references the LLM rationale; the anchors feel better than Phase 2's |

Tag and push:

```bash
git add -A
git commit -m "phase 3a: LLM-scored levels + accept/reject + HMAC magic links"
git tag v0.3a.0
git push --tags
```

---

## 5. Common Phase 3a pitfalls

1. **Anthropic API rate limits on first dev runs.** Free/starter tiers limit a few requests per minute. Scoring N tickers sequentially is fine at single-user volume; if you re-run during dev many times, you'll hit limits. Add `time.sleep(1)` between calls if you see 429s.
2. **JSON-mode hallucinations.** Sonnet usually returns valid JSON but not always. The schema-validation net catches malformed output. Do not parse `content` directly without going through the schema.
3. **Model pinning.** Pin `claude-sonnet-4-6`, never `claude-sonnet-latest`. A future model swap can change behaviour subtly; force the upgrade to be explicit and re-validate prompts.
4. **HMAC secret rotation invalidates live links.** If you rotate `MAGIC_LINK_SECRET`, every magic link already in inboxes becomes invalid. For a single-user app this is fine — write it down so it doesn't surprise you mid-Sunday.
5. **Accept-link forwards.** If you forward a weekly email to someone, they could click Accept. Single-user assumption is fine for v1; multi-user (Phase 5) needs user-id binding in the signed payload.
6. **Time-of-click drift.** A user opens Friday afternoon, clicks Saturday morning. If `expires_at` is Friday EOD, the click 400s. Either extend TTL beyond `expires_at` slightly (+24h grace) or send a Friday morning reminder. Decide and document.
7. **`sr_level.confidence` is NULL for pre-Phase-3a rows.** The suggestion engine treats NULL as "fall back to nearest," not as "confidence = 0." Test this case explicitly.
8. **LLM cost on first run.** If your watchlist grows or you re-run the weekly job many times during dev, you can spend a couple of dollars in an afternoon. The `daily_cost_cap_usd` guard prevents disasters but doesn't prevent slow burn — monitor `llm_call_log` for the first week.
9. **Forgot to update `confidence_at_creation` snapshot.** Easy to miss when refactoring `generate_suggestions`. Test: a freshly-created suggestion has non-null `confidence_at_creation`.

---

## 6. ADRs to write in Phase 3a

- **`docs/adr/0009-llm-guardrails.md`** — new. The three-rule guardrail: never invent prices, never recommend trades, never claim fundamentals beyond the input. JSON-schema validation, daily cost cap, model pinning.
- **`docs/adr/0010-magic-link-auth.md`** — new. HMAC-SHA256 over `(id, action, expires)`, short tokens, time-bound, single-use enforced by status check, separate secret from `ADMIN_TOKEN`.
- **`docs/adr/0006-sr-methodology.md`** — *update*. Add "Scoring pass (Phase 3a)" section describing Sonnet-driven confidence assignment. Keep ⚠ flag on anchor selection — fully closed in 3c.
- **`docs/adr/0007-position-sizing.md`** — *update*. Anchor selection now confidence-weighted-within-band. Keep ⚠ flag pending 3c.

Two new, two partial updates. About 60 minutes total.

---

## 7. Documentation drift to fix

- **CLAUDE.md** — add to "Things to never do": "Never let LLM output flow into the suggestion engine without schema validation and an explicit deterministic fallback path." Update env vars to include `ANTHROPIC_API_KEY` and `MAGIC_LINK_SECRET`. Add `services/llm.py` and `prompts/` to repo layout. Add gotcha entry: HMAC secret rotation invalidates live links.
- **`product_plan.md`** — when Phase 3a ships, mark it complete in §6 and note that 3b and 3c are next.

---

*When all 11 smoke-test rows are green, you've received one Sunday email with visibly LLM-driven anchor choices, and you've clicked Accept on a real suggestion and seen `status='accepted'` in SQLite, plus ADRs 0009 and 0010 are committed and 0006/0007 are partially updated, Phase 3a is done. Tag `v0.3a.0` and start Phase 3b.*
