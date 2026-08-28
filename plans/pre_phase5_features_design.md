# Pre-Phase-5 Features — Design

**Status:** living document · **Scope:** standalone product features shipped during the
post-4.9a soak window, before the Phase 5 multi-tenant work.

This doc collects the *design rationale* for the discrete features added on top of Phase
4.9a. Each feature is a top-level section below; **append new features here** rather than
creating separate `plans/*_design.md` files. The *shipping* details (commit, files, test
counts, live verification) live in `plans/post_4_9a_changes.md` — this doc is the "why and
how it's shaped"; the changelog is the "what landed when". Cross-reference by section.

| # | Feature | Changelog | Section |
|---|---|---|---|
| 1 | Top-up suggestions (sentiment-sized near-target buys) | §17 | [Top-Up Suggestions](#1-top-up-suggestions) |
| 2 | OHLCV-aware decision logic (candle semantics) | §18 | [OHLCV-Aware Decision Logic](#2-ohlcv-aware-decision-logic) |
| 3 | Upcoming-earnings warning (weekly email) | §20 | [Upcoming-Earnings Warning](#3-upcoming-earnings-warning) |
| 4 | Weekly-review reflection / lessons-learned | §21 | [Reflection / Lessons Learned](#4-weekly-review--reflection--lessons-learned) |

---

## 1. Top-Up Suggestions

**Related:** `plans/post_4_9a_changes.md` §17 (on ship), ADR-0021 (context sizing), ADR-0030 (sentiment source)

### Problem

The weekly engine only produces suggestions for **out-of-band** tickers:
- `generate_suggestions` skips `band_status == "in_band"` rows entirely (`suggest.py:176`).
- Under-band tickers whose half-the-gap sizing rounds below 1 share are skipped
  ("gap too small for a full-share order").

So a ticker sitting just under its target — e.g. QQQ at 28.8% vs a 25% target with
`band_high=29` on account 62 — never gets an entry price, even when whole shares of
headroom exist below `band_high` and a well-scored support level is available. The user
wants those opportunistic "top-up" entries surfaced weekly, visually separated from the
regular rebalancing suggestions, and highlighted when the AI's own signals say the entry
is strong.

### Product decisions (user-confirmed)

1. **Sizing:** max whole shares that fit under `band_high`, scaled by a **deterministic
   market-sentiment fraction** (VIX / Fear & Greed), floored at 1 share.
2. **Lifecycle:** first-class `order_suggestion` rows (`kind='topup'`) — Accept/Reject
   magic links, Friday expiry, full audit trail, **and accepted top-ups flow through the
   normal 09:35 ET auto-trade pass** (all guards apply).
3. **Highlight:** deterministic Python over existing AI outputs — anchor confidence ≥
   `TOPUP_HIGHLIGHT_MIN_CONF` (0.75) AND no bearish material news in 7 days. The LLM
   supplies scores/labels; Python decides (inside the no-LLM-trade-recommendations rule).

### Eligibility

A ticker gets a top-up draft in a weekly run iff **all** of:

| # | Condition | Rationale |
|---|---|---|
| 1 | Active target and `gap_pct > 0` (current < target) | "hasn't reached the target" — covers in-band-under AND under-band-sub-share |
| 2 | No regular buy draft this run | mutual exclusivity; preserves the `(account, week_of, ticker, side)` unique constraint |
| 3 | Anchor found by the SAME path as regular buys — scored `select_anchor` (fresh scores only, ≤15% distance) with nearest-support fallback | one anchor-quality bar for the whole product; no forked logic |
| 4 | ≥1 whole share at the anchor keeps the holding ≤ `band_high`: `current_pct + (price/equity)·100 ≤ band_high` | the user's core rule — never suggest past the upper band |
| 5 | Cost fits the shared cash budget (after regular drafts, − $100 floor) | regular rebalancing has priority over opportunistic top-ups |

### Sizing

```
band_cap    = max { n ∈ ℕ : current_pct + n·(price/equity)·100 ≤ band_high }   # safety cap
gap_shares  = floor(gap_usd / price)          # whole shares that close the gap TO TARGET
base_shares = max(1, gap_shares)
effective   = fraction × anchor_confidence    # per-ticker (unscored fallback conf = 0.5)
qty         = min( max(1, floor(base_shares × effective)), band_cap )
```

> **Per-ticker scaling (added 2026-07-20):** the market-level sentiment fraction is
> modulated by each anchor's LLM confidence, so a 0.72-confidence level sizes larger than
> a 0.60 one in the same week (e.g. fear ×0.75 × conf 0.72 = ×0.54). Both inputs are
> AI-supplied metrics; the arithmetic stays deterministic Python. `size_factor` and
> `context_note` record the effective per-ticker value.

> **Corrected 2026-07-20:** the first shipped version sized `base` from the band-headroom
> (`band_cap`) — deploying ~2× the gap (illustrative: an AMZN \$4,000 order vs a \$2,000
> gap — real order size redacted; ratio matches the actual bug). The base is the
> **gap to target**; `band_cap` only binds in the 1-share-floor case (a whole share may
> overshoot a tiny gap but must never cross `band_high`). Since the targets loader enforces
> `target ≤ band_high` (§10), gap-based sizing respects the band by construction.

`fraction = topup_size_fraction(vix, fear_greed_score)` — deterministic table; F&G is
primary, VIX is the fallback when F&G is missing; 0.50 when both are missing:

| Fear & Greed | fraction | | VIX (fallback) | fraction |
|---|---|---|---|---|
| ≤ 25 (extreme fear) | 1.00 | | ≥ 30 | 1.00 |
| 26–45 (fear) | 0.75 | | 20–30 | 0.75 |
| 46–55 (neutral) | 0.50 | | < 20 | 0.50 |
| 56–75 (greed) | 0.25 | | | |
| > 75 (extreme greed) | 0.25 | | | |

Contrarian by design (buy more in fear), consistent with the v2 context-sizing prior
(ADR-0021). Sentiment comes from the Friday-persisted `weekly_market_context` row
(`load_latest_weekly_context`) — the same source the review graph already trusts; no new
scrape. Audit fields: `base_qty = base_shares`, `size_factor = fraction`,
`context_note = "top-up sized ×F (fear&greed=N)"` (or `vix=V` / `defaults` when absent) —
so the email reuses the familiar `N (base B · ×F)` badge.

#### Worked example (account 62, illustrative)
NEE: target 5%, band_high 8%, current 4.2%, equity $30k (illustrative, rounded), anchor $71.
Gap to target = (5 − 4.2)% × $30k = $240 → gap_shares = 3 → base 3. F&G = 38 (fear)
→ ×0.75 → qty = 2 (≈$142, within the gap). Band cap (16 shares) doesn't bind.
Tiny-gap case: gap $30 < 1 share → base floors to 1 IF one share stays ≤ band_high.

### Review-graph interplay

Top-ups are **already sentiment-sized at creation**, so:
- `context_adjust_node` **exempts `kind='topup'` drafts** — no narrative resize (would
  double-count the same F&G signal), no earnings resize/re-anchor (they're minimal and
  deliberately simple). Pass-through preserves their base_qty/size_factor/context_note.
- `reason` node includes them (payload gains `kind`) so each gets a Sonnet rationale.
- `critic` reviews the full set (its combined-cash-floor / over-concentration checks see
  top-ups alongside regular drafts and can still reject/revise them).

### Highlight

After finalize, in the job (Python only):

```
is_highlighted = confidence_at_creation ≥ settings.topup_highlight_min_conf   # 0.75
                 AND no llm_material bearish news_event for ticker in last 7d
```

Persisted on the row (`is_highlighted`) so re-sends render identically. Regular
suggestions always persist `is_highlighted=False` (highlight is a top-up affordance).

### Email (weekly suggestions)

New section **"Top-Up Opportunities"** below the main suggestions table:
- Explainer line: "Near-target tickers with headroom below their band — sized by market
  sentiment. Buying the suggested qty keeps the holding within its band."
- Same columns as the main table + Accept/Reject buttons (same `sign_action` links — the
  ids are ordinary suggestion ids).
- **Highlighted rows**: distinct AA-safe background + "★ strong entry" pill
  (`_components.html.j2` gains an `HL_BG`/`HL_INK` token pair). Non-highlighted rows render
  normally within the section.
- Plain-text mirror in `weekly_suggestions.txt.j2` (`[*]` marker for highlights).

### Schema

`order_suggestion` + two columns (migration after `c3d4e5f6a7b8`):
- `kind` TEXT NOT NULL DEFAULT `'regular'` (`regular` | `topup`)
- `is_highlighted` BOOLEAN NOT NULL DEFAULT 0

`OrderSuggestionRow` gains matching fields; `persist_suggestions` writes them (upsert
semantics unchanged — never overwrites accepted/rejected rows).

### Explicitly unchanged

Auto-trade (`_fetch_accepted_unexecuted` stays kind-agnostic — decision #2), wash-sale/
caps/idempotency guards, reconciliation, expiry sweep, un-accept, daily email,
weekly-review funnel (top-ups count as ordinary suggestions; a kind-split funnel line is a
noted follow-up).

### Risks / notes

- **Sentiment staleness:** `load_latest_weekly_context` already enforces
  `context_max_age_days=4`; a stale/absent row degrades to fraction 0.50 (neutral) — never
  blocks the top-up.
- **Cheap tickers under a wide band** can produce large base_shares; the fraction, the
  cash budget, and the auto-trade caps bound actual exposure.
- **Both a regular sell and a topup buy** for one ticker can't collide: top-ups require
  `gap_pct > 0`, sells require overweight.
- Highlight uses the anchor's LLM confidence — post-§16, only **fresh** (≤7d) scored
  levels feed anchors, so a highlight can't ride a stale score.

---

## 2. OHLCV-Aware Decision Logic

**Related:** `plans/post_4_9a_changes.md` (§18 on ship), ADR-0029 (split-adjusted bars),
§1 above (shared `_select_buy_anchor`).

### Problem

The bar store already holds full OHLCV (+vwap) and the S/R *computation* already uses
highs/lows (pivots, swings). But every *decision* consults only the daily **close**:
`current_price = ind.close` drives level proximity (`build_nearby_levels`), the
support/resistance side split, the 15% distance guard (`_select_buy_anchor`,
`_find_level`), sizing, and the single "Current" price tag in emails. Consequences:

- A support the day's **low pierced but the close reclaimed** (tested-and-held — bullish)
  is indistinguishable from one that was never approached.
- A support with a recent **close below it** (broken) still qualifies as a buy anchor.
- Volume — the difference between a meaningful test and noise — is never consulted.
- The email shows one number for "Current", hiding the day's range.

### Decisions (user-approved)

Keep **daily bars** (no granularity change, no Parquet/DB migration). Make the decision
layer candle-aware. All new metrics are **deterministic Python computed from bars at
runtime** — no schema migration, no new LLM output surface.

### Candle semantics matrix (the core definitions)

For a level `L` and a daily bar `(o, h, l, c, v)`:

| Event | Definition | Interpretation |
|---|---|---|
| **Touch** | `l ≤ L ≤ h` | the market actually traded at the level |
| **Tested & held** (support) | touch AND `c ≥ L` | buyers defended it — strengthens the level |
| **Broken** (support) | `c < L` | closed through — the level failed |
| **Reclaimed** | broken on an earlier bar, later bar closes back ≥ L | ambiguous; treated as *not currently broken* only if the most recent close-through is older than the lookback |
| (Resistance rows mirror: broken = `c > L`) | | |

Close remains the **market-side reference** (a support must sit at/below the close to be a
buy anchor; MA side-classification unchanged) — the candle adds *history quality*, it
doesn't move the reference point.

### New in-memory structures (no on-disk change)

```python
@dataclass(frozen=True)
class Candle:                    # services/indicators.py
    as_of: date
    open: float; high: float; low: float; close: float
    volume: float

# IndicatorRow gains: open/high/low/volume (float | None = None) — last bar's values.

@dataclass(frozen=True)
class LevelStats:                # services/levels.py
    last_touch: date | None      # most recent bar whose range included the level
    touch_count: int             # touches within LEVEL_TOUCH_WINDOW_DAYS (30)
    touched_today: bool          # last bar's range includes the level
    closed_through_recently: bool  # close beyond the level (breaking direction)
                                   # within LEVEL_BROKEN_LOOKBACK_DAYS (10)
    touch_volume_ratio: float | None  # mean volume on touch bars ÷ 20-bar mean volume

# NearbyLevels gains:
#   current: Candle | None = None                      (current_price stays — zero churn)
#   stats:   dict[tuple[str, float], LevelStats]       key = (method, round(price, 4))
#   + helper stats_for(level) -> LevelStats | None
```

Stats are computed inside `build_nearby_levels(..., bars_dir=None)` from the last ~60 bars
per ticker (DuckDB `price_bar`), **only for the nearby levels that survive filtering**
(≤6/ticker). `bars_dir=None` or any fetch failure → no stats, never a failed run.
Pure core: `compute_level_stats(bars_df, level_price, level_type, …) -> LevelStats` so the
test matrix runs on synthetic frames.

### Consumption map (later steps)

- **Step 3 (anchor guard):** `_select_buy_anchor` rejects supports with
  `closed_through_recently` (a broken level is not a pullback target); `tested_today` and
  `touch_count` flow into reason strings. Distance guard stays close-based.
- **Step 4 (LLM payloads):** `score_levels_for_ticker` and the reason node get the stats as
  structured input fields (prompt gains field descriptions only).
- **Step 5 (emails):** "Current" becomes `close (low–high)`; nearby-level cells gain
  touch markers. Weekly review's `levels_table` macro inherits.

### Config knobs (defaults; optional)

`LEVEL_TOUCH_WINDOW_DAYS=30`, `LEVEL_BROKEN_LOOKBACK_DAYS=10` (vol window fixed at 20).

### Explicitly unchanged

Bar backfill (already OHLCV; gains only a column-schema guard), indicators' close-based
math (SMA/EMA/RSI/MACD — standard), pivots/swings (already candle-based), auto-trade,
reconciliation, top-up sizing (×conf), the persisted `sr_level` schema.

### Migration

None on disk. In-memory only: new optional fields with defaults, so existing fabricators
and callers work unchanged; call sites opt in by passing `bars_dir`.

---

## 3. Upcoming-Earnings Warning

**Related:** `plans/post_4_9a_changes.md` §20, `services/earnings.py`, `_components.html.j2` `earnings_box`.

### Problem
The context_adjust earnings *gate* already cuts size / re-anchors for a ticker reporting
within `earnings_lookahead_days` (7), but that adjustment is invisible in the email and
covers only ~1 week. A user placing a manual order had no heads-up that a watchlist ticker
reports in the next fortnight.

### Decision (user-confirmed)
A display-only **warning box** in the weekly suggestions email listing **all watchlist**
tickers with a scheduled earnings report **this week or next** (suggested tickers flagged).
Separate from — and wider than — the sizing gate; the gate is unchanged.

### Design
- **Window:** rolling, `today → week_of + 13 days` (this calendar week through the end of
  next week). Robust to mid-week manual reruns.
- **Data:** reuse the existing Finnhub `EarningsClient.upcoming_earnings(tickers, start, end)`
  the gate already uses — no new dependency. Empty `FINNHUB_API_KEY` → `FakeEarningsClient`
  → empty map → no box. Any feed error is caught in the job so the email still sends.
- **Builder:** pure `build_earnings_warnings(earnings_map, *, week_of, suggested_tickers,
  names, today) -> list[EarningsWarning]` (`services/earnings.py`). `this_week = date <
  week_of + 7`. Sort: suggested-first, then soonest date. `has_suggestion` drives the ★.
- **Email:** `earnings_box` macro (WARN palette) above the untracked box; plain-text mirror
  with `*` markers. Shows date, this/next-week label, days-away.

### Explicitly unchanged
The `context_adjust` earnings gate (sizing/re-anchor within 7 days) — this is a parallel,
display-only concern with its own wider window.

---

## 4. Weekly Review — Reflection / Lessons Learned

**Related:** `plans/post_4_9a_changes.md` §21, `services/reflection.py`,
`prompts/weekly_reflection_v1.txt`, `jobs/weekly_review.py`, table `reflection_insight`.

### Problem
The weekly review shows "Suggestions vs Fills" but never asks *were the calls any good?* and
keeps no memory. There's no feedback loop from suggested price → actual fill → current price →
related news, and no accumulating record of what the process is learning.

### Decisions (user-confirmed)
1. **Scope:** all *resolved* suggestions for the reviewed week — filled (entry vs current),
   expired-unfilled / accepted-but-unfilled (missed level?), rejected. Critic-vetoed drafts
   aren't persisted today → out of v1 scope (follow-up: persist critic rejections first).
2. **Storage:** one table of **generalized lessons only** (`reflection_insight`).
3. **Learning loop:** each reflection sees the last ~8 stored insights (confirm/contradict,
   avoid repetition).

### Hard guardrail
Methodology/process observations ONLY — never price targets, buy/sell/hold recommendations, or
fundamental claims beyond the supplied news (CLAUDE.md:170). Same wall as `WeeklyMarketContext`
(§ ADR-0020): informational, never touches `generate_suggestions` or any broker path. The
prompt reuses the hard-rule register of `score_levels_v2.txt` / `suggestion_critic_v1.txt`.

### Design
- **Table `reflection_insight`** (model `ReflectionInsight`, `FundsEvent` shape): `id`,
  `broker_account_id` (per-account — 61 auto-trade vs 62 manual differ), `week_of` (Date),
  `category` (`anchor`|`sizing`|`limit_placement`|`news_timing`|`outcome_pattern`), `lesson`
  (Text), `tickers` (Text JSON), `relation_to_prior` (`confirms`|`contradicts`|None),
  `created_at`. Plain `op.create_table`; migration after head `d4e5f6a7b8c9`.
- **Deterministic evidence** (`services/reflection.py`): `SuggestionOutcome` frozen dataclass
  from `review.suggestion_audits` + per-ticker current close (`compute_indicators`, already in
  scope) + news sentiment (`review.material_news`); computes `outcome` label +
  `entry_vs_current_pct`. Feeds the LLM payload AND the email evidence table. Not stored.
- **LLM reflection** — direct Sonnet call (not a graph), mirrors `build_weekly_market_context`:
  `reflect_on_week(llm, session, *, outcomes, prior_insights, ...)`. Loads the last N insights,
  `llm.call(SONNET, weekly_reflection_v{ver}.txt, response_schema=_ReflectionOutput,
  temperature=0.3)`, `persist_llm_call_log(purpose="weekly_reflection")`. Parsed None/[] →
  `[]` → section skipped, email still sends. No outcomes this week → skip.
- **Wiring:** `run_weekly_review_for_account` gains `llm`; build outcomes after indicators,
  reflect in a short session scope (llm_call_log), persist insights in a separate scope
  (write-lock discipline — mirror `_build_and_persist_context`); try/except → `[]` on failure.
  `WeeklyReview` gains `reflection` + `outcomes` (threaded through the frozen rebuild).
- **Email:** "Reflection — Lessons from This Week" after "Suggestions vs Fills" — an evidence
  table (suggested/fill/current/entry-vs-current%/news/outcome) + a lessons list (category chip
  + lesson + tickers + confirm/contradict note). HTML + txt; preview before deploy.
- **Config:** `reflection_enabled=True`, `reflection_prior_insights_count=8`,
  `reflection_prompt_version="1"`.

### Explicitly unchanged
Suggestion/execution engine, auto-trade, reconciliation. Insights never flow back into the
engine — read-only + writes only to `reflection_insight`.

---

## Future features

Append the next pre-Phase-5 feature as `## 5. <name>` here, following the same shape
(Problem → Decision → Design → Explicitly unchanged / Risks), and add a changelog row.
