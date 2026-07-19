# Top-Up Suggestions — Design

**Date:** 2026-07-19 · **Status:** Approved (implementation in progress)
**Related:** `plans/post_4_9a_changes.md` §17 (on ship), ADR-0021 (context sizing), ADR-0030 (sentiment source)

## Problem

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

## Product decisions (user-confirmed)

1. **Sizing:** max whole shares that fit under `band_high`, scaled by a **deterministic
   market-sentiment fraction** (VIX / Fear & Greed), floored at 1 share.
2. **Lifecycle:** first-class `order_suggestion` rows (`kind='topup'`) — Accept/Reject
   magic links, Friday expiry, full audit trail, **and accepted top-ups flow through the
   normal 09:35 ET auto-trade pass** (all guards apply).
3. **Highlight:** deterministic Python over existing AI outputs — anchor confidence ≥
   `TOPUP_HIGHLIGHT_MIN_CONF` (0.75) AND no bearish material news in 7 days. The LLM
   supplies scores/labels; Python decides (inside the no-LLM-trade-recommendations rule).

## Eligibility

A ticker gets a top-up draft in a weekly run iff **all** of:

| # | Condition | Rationale |
|---|---|---|
| 1 | Active target and `gap_pct > 0` (current < target) | "hasn't reached the target" — covers in-band-under AND under-band-sub-share |
| 2 | No regular buy draft this run | mutual exclusivity; preserves the `(account, week_of, ticker, side)` unique constraint |
| 3 | Anchor found by the SAME path as regular buys — scored `select_anchor` (fresh scores only, ≤15% distance) with nearest-support fallback | one anchor-quality bar for the whole product; no forked logic |
| 4 | ≥1 whole share at the anchor keeps the holding ≤ `band_high`: `current_pct + (price/equity)·100 ≤ band_high` | the user's core rule — never suggest past the upper band |
| 5 | Cost fits the shared cash budget (after regular drafts, − $100 floor) | regular rebalancing has priority over opportunistic top-ups |

## Sizing

```
band_cap    = max { n ∈ ℕ : current_pct + n·(price/equity)·100 ≤ band_high }   # safety cap
gap_shares  = floor(gap_usd / price)          # whole shares that close the gap TO TARGET
base_shares = max(1, gap_shares)
qty         = min( max(1, floor(base_shares × fraction)), band_cap )
```

> **Corrected 2026-07-20:** the first shipped version sized `base` from the band-headroom
> (`band_cap`) — deploying ~2× the gap (AMZN: \$3,511 vs a \$1,751 gap). The base is the
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

### Worked example (account 62, illustrative)
NEE: target 5%, band_high 8%, current 4.2%, equity $30.2k, anchor $71.
Gap to target = (5 − 4.2)% × $30.2k = $242 → gap_shares = 3 → base 3. F&G = 38 (fear)
→ ×0.75 → qty = 2 (≈$142, within the gap). Band cap (16 shares) doesn't bind.
Tiny-gap case: gap $30 < 1 share → base floors to 1 IF one share stays ≤ band_high.

## Review-graph interplay

Top-ups are **already sentiment-sized at creation**, so:
- `context_adjust_node` **exempts `kind='topup'` drafts** — no narrative resize (would
  double-count the same F&G signal), no earnings resize/re-anchor (they're minimal and
  deliberately simple). Pass-through preserves their base_qty/size_factor/context_note.
- `reason` node includes them (payload gains `kind`) so each gets a Sonnet rationale.
- `critic` reviews the full set (its combined-cash-floor / over-concentration checks see
  top-ups alongside regular drafts and can still reject/revise them).

## Highlight

After finalize, in the job (Python only):

```
is_highlighted = confidence_at_creation ≥ settings.topup_highlight_min_conf   # 0.75
                 AND no llm_material bearish news_event for ticker in last 7d
```

Persisted on the row (`is_highlighted`) so re-sends render identically. Regular
suggestions always persist `is_highlighted=False` (highlight is a top-up affordance).

## Email (weekly suggestions)

New section **"Top-Up Opportunities"** below the main suggestions table:
- Explainer line: "Near-target tickers with headroom below their band — sized by market
  sentiment. Buying the suggested qty keeps the holding within its band."
- Same columns as the main table + Accept/Reject buttons (same `sign_action` links — the
  ids are ordinary suggestion ids).
- **Highlighted rows**: distinct AA-safe background + "★ strong entry" pill
  (`_components.html.j2` gains an `HL_BG`/`HL_INK` token pair). Non-highlighted rows render
  normally within the section.
- Plain-text mirror in `weekly_suggestions.txt.j2` (`[*]` marker for highlights).

## Schema

`order_suggestion` + two columns (migration after `c3d4e5f6a7b8`):
- `kind` TEXT NOT NULL DEFAULT `'regular'` (`regular` | `topup`)
- `is_highlighted` BOOLEAN NOT NULL DEFAULT 0

`OrderSuggestionRow` gains matching fields; `persist_suggestions` writes them (upsert
semantics unchanged — never overwrites accepted/rejected rows).

## Explicitly unchanged

Auto-trade (`_fetch_accepted_unexecuted` stays kind-agnostic — decision #2), wash-sale/
caps/idempotency guards, reconciliation, expiry sweep, un-accept, daily email,
weekly-review funnel (top-ups count as ordinary suggestions; a kind-split funnel line is a
noted follow-up).

## Risks / notes

- **Sentiment staleness:** `load_latest_weekly_context` already enforces
  `context_max_age_days=4`; a stale/absent row degrades to fraction 0.50 (neutral) — never
  blocks the top-up.
- **Cheap tickers under a wide band** can produce large base_shares; the fraction, the
  cash budget, and the auto-trade caps bound actual exposure.
- **Both a regular sell and a topup buy** for one ticker can't collide: top-ups require
  `gap_pct > 0`, sells require overweight.
- Highlight uses the anchor's LLM confidence — post-§16, only **fresh** (≤7d) scored
  levels feed anchors, so a highlight can't ride a stale score.
