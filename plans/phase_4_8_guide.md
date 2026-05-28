# Phase 4.8 — Weekly Order Activity Summary: Step-by-Step Guide

**Goal:** Add an **Order Activity** section to the existing Friday 17:00 ET weekly review email that tells you, at a glance, *what actually happened with your orders this week.* Three classes of metric, all measured over the **operations week** (Monday open → Friday 16:00 ET): (a) a **suggestion funnel** — counts of suggested / accepted / placed-but-unfilled / filled / rejected / expired, with manual placements surfaced as their own honest-accounting bucket; (b) **dollar flow** — $ routed and $ filled, broken out buy vs. sell and LIVE vs. DRY_RUN; (c) **allocation drift** — per-ticker `gap_pct` Monday vs. Friday with a "moved toward target" indicator. Plus a small **per-ticker breakdown** table and a **4-week trend** strip. No new mutable state, no trade-attributable fill-rate fiction — the email tells you what your portfolio did, not what mechanically lined up.

**Out of scope for Phase 4.8:**
- **Trade-attributable fill rate.** Deliberately not measured (`$ filled ÷ $ suggested` is a metric that looks meaningful and isn't — manual placements break it, and so do organic drift, partial fills against GTC orders that span weeks, and re-placements after broker_cancelled). Allocation drift is the honest measure; ADR-0023 records the choice.
- **P&L / return / benchmark comparison.** Phase 5+ territory.
- **Per-user metrics.** Single-user throughout 4.x.
- **A materialised metrics table.** All numbers are *derived live* from `order_suggestion`, `order_execution`, `positions_snapshot`, and `target_allocation`. No new mutable rows; no migration of historical state to bring 4.8 up retroactively. (See pitfall 3 on caching, if metrics ever get slow.)
- **Real-time / mid-week metrics.** Friday-email-only. The Sunday weekly-suggestions email is not in scope here — that's still about *next* week.

**Time budget:** 2–4 evenings. Pure query-and-render work over data that already exists.

**Definition of done:** all 10 smoke-test rows pass, *and* you've received **two consecutive Friday weekly-review emails** with the new "Order Activity" section populated and every headline number cross-checked against a hand-written SQL query for that week (no rounding drift, no double-counting of dry-runs, no off-by-one on the funnel). Tag: `v0.4.8.0`.

**Depends on:**
- **Phase 4** (`v0.4.x`) — `order_execution` schema and reconciliation pipeline. The funnel queries `dry_run`, `status`, `qty_filled`, `avg_fill_price`.
- **Phase 4.5** (`v0.4.5.0` code-complete) — the Friday weekly-review email frame. 4.8 adds a section to it.
- **Phase 1+** — `positions_snapshot` populated daily by the daily report job. 4.8's drift query relies on a Monday open snapshot and a Friday 16:00 ET snapshot.

---

## Architecture context — what's new in Phase 4.8

```
                                     FRIDAY 17:00 ET
                                     jobs/weekly_review.py
                                            │
                       ┌────────────────────┴────────────────────┐
                       │ services/weekly_review_metrics.py (NEW) │
                       │                                          │
                       │   compute_order_funnel(s, mon, fri)      │
                       │     ──► funnel_counts.sql                │
                       │   compute_order_flow(s, mon, fri)        │
                       │     ──► order_flow.sql                   │
                       │   compute_allocation_drift(s, mon, fri)  │
                       │     ──► alloc_drift.sql                  │
                       │   compute_per_ticker_breakdown(...)      │
                       │     ──► per_ticker_breakdown.sql         │
                       │   compute_4_week_trend(s, fri)           │
                       │     ──► trend_4w.sql                     │
                       │                                          │
                       │   All return frozen dataclasses,          │
                       │   no ORM rows leave the session.         │
                       └────────────────────┬────────────────────┘
                                            │
                                            ▼
                       WeeklyReview gains five new fields, the
                       email template renders the new section
                       between "weekly suggestions performance" and
                       "auto-trade soak status" (Phase 4.6 block).
```

Four behaviours to internalize:

1. **No new schema. No new writes.** Every number rendered in the new section is *queried live* from existing OLTP tables. If you find yourself reaching for a `weekly_metrics` table, stop — the queries should run in well under a second at single-user scale, and a materialised table introduces a refresh-staleness problem that the live query simply doesn't have. (If the queries do get slow later, ADR-0023 records the upgrade path: a `weekly_metrics_cache` table populated by the same Friday job, with the email reading from cache. Not Phase 4.8.)

2. **Placed vs. filled vs. accepted-but-unrouted are three different states, surfaced separately.** Auto-trade may be `OFF` for the whole week (so "placed = 0" is correct, not a bug), or `LIVE` but a suggestion's `order_execution` row is missing because the user routed it manually in the broker UI (the system can't see this — it's the honest-accounting bucket). Conflating these is how dashboards mislead. The funnel has separate lines for each.

3. **DRY_RUN belongs in the email but in its own line.** During the auto-trade soak progression (Phase 4.6 ladder), DRY_RUN is the *only* execution activity for weeks at a time. Suppressing it would make the section look empty even though plenty is happening. Folding it into LIVE numbers would falsify the "what really moved money" headline. So: separate line, labelled `DRY_RUN (simulated)`, with all the same sub-metrics.

4. **Drift is the truthful measure. Trade-attribution is the seductive one.** "$ filled ÷ $ suggested" looks like a discipline KPI but breaks the moment a suggestion fills partially, gets re-placed after `broker_cancelled`, fills *next* week against a GTC order, or you place manually. Allocation drift (`gap_pct` Mon vs. Fri per ticker) just measures what the portfolio did, regardless of mechanism — which is what a long-term investor actually cares about. ADR-0023 spells this out so the next agent doesn't add the seductive metric back.

---

## 0. Pre-flight checklist (~15 minutes)

- [ ] **Phase 4.5 weekly review email is shipping.** Confirm by looking at last Friday's email — if the "Weekly Market Context" section is there, the template frame is alive.
- [ ] **`positions_snapshot` has data for the current Monday and the current Friday.** Quick SQL: `SELECT DISTINCT snapshot_date FROM positions_snapshot WHERE snapshot_date BETWEEN :mon AND :fri`. If the Friday row isn't there yet at email time, fall back to the most recent snapshot before email time (pitfall 5).
- [ ] **`order_execution` has at least one `dry_run=False` row to test against.** If the auto-trade soak is still in DRY_RUN, that's fine — smoke row 3 covers the DRY_RUN-only case.
- [ ] **`order_suggestion.week_of` semantics confirmed:** it's the Monday of the suggestion's intended trading week, per Phase 2. The funnel groups by `week_of`; the drift query uses the same Monday/Friday pair.
- [ ] **Decide the Monday baseline rule for holiday weeks.** If Monday is a market holiday (Memorial Day, etc.) there is no Monday snapshot. Phase 4.8 falls back to the most recent weekday snapshot strictly before Monday and labels the drift row footnote accordingly (pitfall 7). Confirm this is what you want before tagging.

---

## 1. New service: `services/weekly_review_metrics.py` (~1 evening)

Pure functions over the OLTP session. Each returns a frozen dataclass; nothing returns an ORM row. SQL lives in `src/investor/sql/` per convention.

### 1a. Frozen dataclasses

```python
# src/investor/services/weekly_review_metrics.py
from dataclasses import dataclass
from datetime import date

@dataclass(frozen=True)
class OrderFunnel:
    week_of: date                       # Monday of the operations week
    suggested:        int               # all order_suggestion rows for the week
    accepted:         int               # status='accepted' (at any point, including now-expired)
    routed_live:      int               # order_execution rows: dry_run=False, status in {accepted_for_routing, filled, partially_filled, broker_cancelled}
    filled_live:      int               # order_execution rows: dry_run=False, status='filled' (qty_filled >= qty)
    partial_live:     int               # dry_run=False, status='partially_filled' (0 < qty_filled < qty)
    accepted_not_routed: int            # accepted suggestions with no dry_run=False order_execution row — assumed manual placement
    rejected:         int               # status='rejected'
    expired:          int               # status='expired'
    dry_run_count:    int               # dry_run=True execution rows that week (informational only)


@dataclass(frozen=True)
class OrderFlow:
    week_of: date
    buy_routed_usd:   float             # sum(qty * limit_price) for routed buy rows (dry_run=False)
    sell_routed_usd:  float             # mirror for sells
    buy_filled_usd:   float             # sum(qty_filled * avg_fill_price) for filled buys (dry_run=False)
    sell_filled_usd:  float             # mirror for sells
    dry_run_buy_usd:  float             # buy notional in dry_run rows
    dry_run_sell_usd: float
    distinct_tickers: int               # how many distinct tickers had any execution this week (LIVE only)


@dataclass(frozen=True)
class AllocationDriftRow:
    ticker: str
    target_pct:        float
    current_pct_mon:   float
    current_pct_fri:   float
    gap_pct_mon:       float            # = target_pct - current_pct_mon  (positive = under-target)
    gap_pct_fri:       float            # mirror
    drift_pp:          float            # current_pct_fri - current_pct_mon  (signed, percentage points)
    moved_toward_target: bool           # True if |gap_pct_fri| < |gap_pct_mon|, OR signs flipped (over-correction still counts as "moved")
    targets_changed_midweek: bool       # for the footnote
    monday_is_fallback:  bool           # True if Monday was a holiday and an earlier snapshot was used


@dataclass(frozen=True)
class PerTickerWeekRow:
    ticker: str
    side_suggested:   str | None        # 'buy' | 'sell' | None if no suggestion this week
    suggested_qty:    float
    routed_qty_live:  float
    filled_qty_live:  float
    filled_usd_live:  float
    drift_pp:         float             # signed
    moved_toward_target: bool


@dataclass(frozen=True)
class WeekTrendRow:
    week_of: date
    suggested:       int
    filled_live:     int
    buy_filled_usd:  float
    sell_filled_usd: float
    abs_drift_pp_sum: float             # sum of |drift_pp| across all tickers — proxy for "how much did things move"
```

### 1b. The five pure functions

```python
def compute_order_funnel(s: Session, *, mon: date, fri: date) -> OrderFunnel:
    """Counts of every funnel state for the operations week.

    `mon` is the Monday of the suggestion week (order_suggestion.week_of).
    `fri` is the Friday close (16:00 ET, used only for the dry_run window).
    """
    row = s.execute(load_sql("funnel_counts.sql"),
                    {"mon": mon, "fri_eod": _eod(fri)}).mappings().one()
    return OrderFunnel(week_of=mon, **row)


def compute_order_flow(s, *, mon, fri) -> OrderFlow:        ...
def compute_allocation_drift(s, *, mon, fri) -> list[AllocationDriftRow]:  ...
def compute_per_ticker_breakdown(s, *, mon, fri) -> list[PerTickerWeekRow]: ...
def compute_4_week_trend(s, *, current_fri: date) -> list[WeekTrendRow]:    ...
```

Inside each, the SQL is loaded from `src/investor/sql/<name>.sql` (existing convention) — no inline strings. Every helper resolves to plain `dict`/`float`/`int`/`str` values before the session boundary; nothing returns an ORM row.

> **Targets-mid-week detection.** Inside `compute_allocation_drift`, a small extra query checks whether any `target_allocation` row has `effective_from > mon AND effective_from <= fri`. If yes, every drift row sets `targets_changed_midweek = True` and the email surfaces a footnote: *"Targets were edited mid-week (N changes); drift includes the target-shift component."* The drift number itself is still computed against the *Friday-effective* targets (which is what you care about going forward), but the footnote keeps the user honest.

---

## 2. SQL files in `src/investor/sql/` (~half evening)

Five new `.sql` files, one per metric. The core ones:

### 2a. `funnel_counts.sql`

```sql
-- :mon       — date (Monday of operations week)
-- :fri_eod   — datetime (Friday 16:00 ET, expressed UTC)

WITH suggestions AS (
  SELECT id, status, side, qty, ticker
    FROM order_suggestion
   WHERE week_of = :mon
),
live_exec AS (
  SELECT oe.suggestion_id, oe.status, oe.qty_filled
    FROM order_execution oe
    JOIN suggestions s ON oe.suggestion_id = s.id
   WHERE oe.dry_run = FALSE
),
dry_exec AS (
  SELECT oe.suggestion_id
    FROM order_execution oe
    JOIN suggestions s ON oe.suggestion_id = s.id
   WHERE oe.dry_run = TRUE
)
SELECT
  (SELECT COUNT(*) FROM suggestions)                                            AS suggested,
  (SELECT COUNT(*) FROM suggestions WHERE status = 'accepted')                  AS accepted,
  (SELECT COUNT(DISTINCT suggestion_id) FROM live_exec
     WHERE status IN ('accepted_for_routing', 'filled',
                      'partially_filled', 'broker_cancelled'))                  AS routed_live,
  (SELECT COUNT(DISTINCT suggestion_id) FROM live_exec WHERE status = 'filled') AS filled_live,
  (SELECT COUNT(DISTINCT suggestion_id) FROM live_exec
     WHERE status = 'partially_filled')                                         AS partial_live,
  -- "accepted, not routed": accepted suggestions with no live exec row
  (SELECT COUNT(*) FROM suggestions s
     WHERE s.status = 'accepted'
       AND NOT EXISTS (SELECT 1 FROM live_exec le
                        WHERE le.suggestion_id = s.id))                         AS accepted_not_routed,
  (SELECT COUNT(*) FROM suggestions WHERE status = 'rejected')                  AS rejected,
  (SELECT COUNT(*) FROM suggestions WHERE status = 'expired')                   AS expired,
  (SELECT COUNT(DISTINCT suggestion_id) FROM dry_exec)                          AS dry_run_count;
```

### 2b. `alloc_drift.sql`

The drift query needs the *latest available snapshot ≤ Monday open* for each ticker and the *latest available snapshot ≤ Friday EOD* for each ticker, the Friday-effective `target_allocation` row, and total equity at each end (for `current_pct`).

```sql
-- :mon, :fri  — dates

WITH mon_snap AS (
  SELECT ticker, market_value
    FROM positions_snapshot ps
   WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM positions_snapshot
                            WHERE snapshot_date <= :mon)
),
fri_snap AS (
  SELECT ticker, market_value
    FROM positions_snapshot ps
   WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM positions_snapshot
                            WHERE snapshot_date <= :fri)
),
mon_equity AS (SELECT SUM(market_value) AS total FROM mon_snap),
fri_equity AS (SELECT SUM(market_value) AS total FROM fri_snap),
targets AS (
  SELECT ticker, target_pct
    FROM target_allocation
   WHERE effective_from <= :fri AND (effective_to IS NULL OR effective_to > :fri)
)
SELECT
  t.ticker,
  t.target_pct,
  COALESCE(ms.market_value, 0) / NULLIF((SELECT total FROM mon_equity), 0) * 100 AS current_pct_mon,
  COALESCE(fs.market_value, 0) / NULLIF((SELECT total FROM fri_equity), 0) * 100 AS current_pct_fri
FROM targets t
LEFT JOIN mon_snap ms ON ms.ticker = t.ticker
LEFT JOIN fri_snap fs ON fs.ticker = t.ticker
ORDER BY t.target_pct DESC;
```

Python wraps this and computes `gap_pct_mon`, `gap_pct_fri`, `drift_pp`, and `moved_toward_target` from the four numeric columns. The "Monday is fallback" flag comes from comparing the Monday snapshot's actual date against `:mon`.

> The `NULLIF(..., 0)` guards against a zero-equity edge case (fresh deploy, no positions). The `LEFT JOIN` ensures targets-without-positions show up with `current_pct = 0` (= 100% under-target — useful information).

### 2c. `order_flow.sql`, `per_ticker_breakdown.sql`, `trend_4w.sql`

Same pattern — single statement, named params, `JOIN order_execution` filtered on `dry_run`. The 4-week trend simply unions `funnel_counts.sql` over four consecutive Mondays, ordered oldest→newest. Keep these in version control next to the existing `gap.sql` / `sr_level.sql` so they're easy to inspect and maintain.

---

## 3. Wire into `jobs/weekly_review.py` (~half evening)

Inside the existing `run_weekly_review` function, after the Phase 4.5 `build_weekly_market_context` call:

```python
def run_weekly_review(settings, adapter, emailer, llm, tavily, sentiment_client):
    week_mon = _last_monday()                          # Monday of the operations week ending today
    week_fri = _last_friday_close()                    # today's date if today is Fri, otherwise the most recent Fri

    with session_scope() as s:
        funnel    = compute_order_funnel(s, mon=week_mon, fri=week_fri)
        flow      = compute_order_flow(s, mon=week_mon, fri=week_fri)
        drift     = compute_allocation_drift(s, mon=week_mon, fri=week_fri)
        breakdown = compute_per_ticker_breakdown(s, mon=week_mon, fri=week_fri)
        trend     = compute_4_week_trend(s, current_fri=week_fri)

    # ... existing Phase 4.5 market context fetch / persist ...

    review = WeeklyReview(
        ...,
        order_funnel=funnel,
        order_flow=flow,
        drift_rows=drift,
        breakdown_rows=breakdown,
        trend_rows=trend,
    )
    # render + send unchanged
```

All five metric functions run inside one `with session_scope()` — single read transaction, consistent snapshot of the OLTP state. No write contention; this block is read-only.

> **The week pointer.** `_last_monday()` and `_last_friday_close()` resolve to *this* week's Monday and Friday when the job fires Friday 17:00 ET. If the job is manually re-triggered on a Saturday or Sunday, the same week is intended (the operations week just ended). If triggered on a Wednesday for some reason, it should refuse and log — week isn't over yet. Add that guard explicitly; smoke row 9 enforces.

---

## 4. Email template — new section (~half evening)

Add between the existing "weekly suggestions performance" and the "auto-trade soak status" sections of `templates/weekly_review.html.j2`:

```jinja
<h2 style="margin-top:30px;">Order Activity — week of {{ review.order_funnel.week_of.strftime("%b %d") }}</h2>

<h3 style="margin-bottom:4px;">Suggestion funnel</h3>
<table style="font-size:14px; border-collapse:collapse;">
  <tr><td>Suggested</td>                       <td><b>{{ review.order_funnel.suggested }}</b></td></tr>
  <tr><td>Accepted</td>                        <td><b>{{ review.order_funnel.accepted }}</b></td></tr>
  <tr><td>&nbsp;&nbsp;Routed (auto-trade LIVE)</td>
                                               <td><b>{{ review.order_funnel.routed_live }}</b></td></tr>
  <tr><td>&nbsp;&nbsp;Filled</td>              <td><b>{{ review.order_funnel.filled_live }}</b>
      {% if review.order_funnel.partial_live %}
      <span style="color:#888;">(+{{ review.order_funnel.partial_live }} partial)</span>
      {% endif %}</td></tr>
  <tr><td>&nbsp;&nbsp;Accepted, not auto-routed</td>
      <td><b>{{ review.order_funnel.accepted_not_routed }}</b>
          <span style="color:#888;">(presumed manual — system can't see broker-UI placements)</span></td></tr>
  <tr><td>Rejected</td>                        <td>{{ review.order_funnel.rejected }}</td></tr>
  <tr><td>Expired</td>                         <td>{{ review.order_funnel.expired }}</td></tr>
  {% if review.order_funnel.dry_run_count %}
  <tr><td colspan="2" style="color:#888; font-size:12px; padding-top:8px;">
      DRY_RUN (simulated): {{ review.order_funnel.dry_run_count }} executions —
      ${{ "%.2f"|format(review.order_flow.dry_run_buy_usd) }} buys,
      ${{ "%.2f"|format(review.order_flow.dry_run_sell_usd) }} sells
  </td></tr>
  {% endif %}
</table>

<h3 style="margin-top:16px; margin-bottom:4px;">Dollar flow (LIVE)</h3>
<table style="font-size:14px;">
  <tr><td>Buys routed</td>  <td>${{ "%.2f"|format(review.order_flow.buy_routed_usd) }}</td>
      <td>Buys filled</td>  <td>${{ "%.2f"|format(review.order_flow.buy_filled_usd) }}</td></tr>
  <tr><td>Sells routed</td> <td>${{ "%.2f"|format(review.order_flow.sell_routed_usd) }}</td>
      <td>Sells filled</td> <td>${{ "%.2f"|format(review.order_flow.sell_filled_usd) }}</td></tr>
  <tr><td>Distinct tickers w/ activity</td>
      <td colspan="3"><b>{{ review.order_flow.distinct_tickers }}</b></td></tr>
</table>

<h3 style="margin-top:16px; margin-bottom:4px;">Allocation drift (Mon → Fri)</h3>
<table style="font-size:13px; border-collapse:collapse;">
  <tr style="border-bottom:1px solid #ccc;">
    <th align="left">Ticker</th>
    <th align="right">Target%</th>
    <th align="right">Mon%</th>
    <th align="right">Fri%</th>
    <th align="right">Drift (pp)</th>
    <th></th>
  </tr>
  {% for r in review.drift_rows %}
  <tr>
    <td>{{ r.ticker }}{% if r.monday_is_fallback %}*{% endif %}</td>
    <td align="right">{{ "%.1f"|format(r.target_pct) }}</td>
    <td align="right">{{ "%.1f"|format(r.current_pct_mon) }}</td>
    <td align="right">{{ "%.1f"|format(r.current_pct_fri) }}</td>
    <td align="right" style="color:{{ '#2a8' if r.moved_toward_target else '#a44' }};">
        {{ "%+.2f"|format(r.drift_pp) }}
    </td>
    <td>{{ "→ closer" if r.moved_toward_target else "→ farther" }}</td>
  </tr>
  {% endfor %}
</table>
{% if review.drift_rows and review.drift_rows[0].targets_changed_midweek %}
<p style="font-size:12px; color:#888;">
  Targets were edited mid-week; drift includes the target-shift component.
</p>
{% endif %}
{% if review.drift_rows | selectattr("monday_is_fallback") | list %}
<p style="font-size:12px; color:#888;">
  * Monday was a market holiday; the most recent prior snapshot was used as the baseline.
</p>
{% endif %}

<h3 style="margin-top:16px; margin-bottom:4px;">Per-ticker breakdown</h3>
<!-- per_ticker_breakdown table — same shape as drift, with side/qty/$ filled added -->

<h3 style="margin-top:16px; margin-bottom:4px;">4-week trend</h3>
<table style="font-size:12px;">
  <tr><th>Week of</th><th>Suggested</th><th>Filled</th><th>$ buys</th><th>$ sells</th><th>|Σ drift|</th></tr>
  {% for w in review.trend_rows %}
  <tr>
    <td>{{ w.week_of.strftime("%b %d") }}</td>
    <td>{{ w.suggested }}</td>
    <td>{{ w.filled_live }}</td>
    <td>${{ "%.0f"|format(w.buy_filled_usd) }}</td>
    <td>${{ "%.0f"|format(w.sell_filled_usd) }}</td>
    <td>{{ "%.2f"|format(w.abs_drift_pp_sum) }}</td>
  </tr>
  {% endfor %}
</table>
```

Mirror the section in `templates/weekly_review.txt.j2` with plain-text alignment (use `{:>10}` column widths). Outlook will render the HTML cleanly with `border-collapse:collapse` and inline styles; avoid CSS classes and external stylesheets — Outlook ignores both.

> **Order of sections in the email.** The new Order Activity block goes **before** the Phase 4.5 Weekly Market Context (which is forward-looking) and **after** the auto-trade soak status (which is operational state). Backward-looking → forward-looking flows more naturally for a Friday digest.

---

## 5. Settings (~10 min)

```python
# config.py
weekly_review_trend_weeks: int = 4              # how many weeks back the trend strip shows
weekly_review_breakdown_top_n: int = 20         # cap per-ticker breakdown rows; tickers beyond top-N by |drift_pp| collapse to "+N more"
```

Both have sensible defaults; no env-var-required.

---

## 6. Smoke-test checklist (Phase 4.8 done when all green)

| # | Step | Pass criteria |
|---|---|---|
| 1 | `uv run pytest tests/test_weekly_review_metrics.py` — empty week | All five functions return zero-/empty-valued dataclasses with no crash; no `NULLIF` divide-by-zero |
| 2 | Typical week with mixed funnel | Suggested/accepted/routed/filled counts match a hand-written `SELECT COUNT(*) … WHERE week_of=…` query exactly |
| 3 | DRY_RUN-only week | `routed_live=0`, `filled_live=0`, `dry_run_count > 0`, and the email shows the DRY_RUN line; LIVE totals are zero (not omitted) |
| 4 | Manual-placement bucket | An accepted suggestion with no `dry_run=False` `order_execution` row counts in `accepted_not_routed`; the email labels it "presumed manual" |
| 5 | Drift sign correctness | Inject a position that moved up Mon→Fri for an under-target ticker → `drift_pp > 0`, `moved_toward_target=True`, green colour, "→ closer" text |
| 6 | Drift over-correction | A position that moved from −2pp under to +1pp over (signs flipped, smaller magnitude) → `moved_toward_target=True`, colour reflects movement, not band |
| 7 | Monday-holiday fallback | Set Monday = Memorial Day; query falls back to the prior Friday's snapshot; `monday_is_fallback=True`; email footnote rendered |
| 8 | Targets-changed-midweek footnote | Insert a `target_allocation` row with `effective_from = Wednesday`; footnote renders; drift values are against Friday-effective targets |
| 9 | Wrong-day re-trigger | Manually triggering the job on a Wednesday refuses with a clear error (week isn't over) |
| 10 | End-to-end | Two consecutive Friday emails arrive; every headline number in each cross-checks against a hand-written SQL query for that week (no rounding drift; partial-fill bucket counts where expected; manual-placement bucket counts where expected) |
| — | `uv run pytest` overall | New tests pass; total ≥ 308 (298 at 4.7 close + ~10 new); `ruff` and `mypy` clean |

Tag:

```bash
git add -A
git commit -m "phase 4.8: weekly order activity summary (funnel, flow, drift, breakdown, 4w trend)"
git tag v0.4.8.0
git push --tags
```

---

## 7. Common Phase 4.8 pitfalls

1. **The honest-accounting bucket is silent during DRY_RUN soak.** When auto-trade is DRY_RUN, *every* accepted suggestion lands in `accepted_not_routed` because no LIVE exec row exists. Don't read that as "user is routing manually" — read the auto-trade mode from `meta` and label the bucket accordingly. Better still, gate the "presumed manual" label on `auto_trade_mode == 'LIVE'`; in OFF/DRY_RUN, label it "not yet routed (auto-trade not LIVE)."
2. **Partial fills double-count if you're not careful.** A suggestion that filled partially has *one* `order_execution` row whose status flips through `accepted_for_routing → partially_filled → filled`. The funnel SQL uses `COUNT(DISTINCT suggestion_id)` for each bucket so a single suggestion never lands in multiple states. Verify with row 2.
3. **Don't materialise yet.** It will be tempting to add a `weekly_metrics` table after watching the queries run. Don't — at single-user scale, every metric query runs in well under 100 ms against indexed columns. A cache table introduces a staleness problem and a new schema migration. ADR-0023 records the upgrade path *if* the queries get slow at multi-user (Phase 5).
4. **`avg_fill_price` can be `NULL` until reconciliation runs.** The Friday 17:00 ET weekly review fires *after* the Friday 16:20 ET expiry sweep and *after* end-of-day reconciliation. Confirm reconciliation has run before metrics compute — otherwise filled rows have NULL price and `$ filled` is wrong. Smoke row 10 catches this if reconciliation timing slips.
5. **Friday-snapshot timing.** The daily report job takes the Friday close snapshot at some point after market close. If the weekly review fires before that snapshot is committed, drift uses Thursday's positions as "Friday" and reads silly. Either chain the cron explicitly (weekly review depends on Friday daily report completing), or have the drift query fall back to "most recent snapshot ≤ Friday 17:00 ET" and footnote when it's not the day-of.
6. **GTC orders that span weeks.** A buy suggested last week, accepted, routed Monday, that fills *next* Tuesday: this week's funnel sees it as `routed_live + 1, filled_live + 0`. Next week's funnel sees it as `filled_live + 1` against last week's `routed_live`. That's correct under the operations-week model — but it means `routed_live` and `filled_live` for *this week's suggestions* are not directly comparable across weeks until a quarter or so of data has accrued. The trend strip's "filled" column will lag for the same reason.
7. **Holiday-week drift baseline.** If Monday is a holiday, using the prior Friday's snapshot as the Mon baseline conflates "weekend drift" with "this-week's drift." Footnote it. Don't silently absorb it.
8. **Per-ticker breakdown overflow.** A user with 30 tickers gets a giant table. `weekly_review_breakdown_top_n` (default 20) caps it and renders "+N more" at the bottom; the SQL still considers all rows for trend math.
9. **Outlook table rendering.** Inline `border-collapse:collapse` on every `<table>`, no external CSS. Test Outlook on Windows once — it'll silently flatten anything fancy.

---

## 8. ADRs to write in Phase 4.8

- **`docs/adr/0023-weekly-order-activity-metrics.md`** — new. Three decisions to record:
  1. **Allocation drift as the gap metric, not trade-attributable fill rate.** Drift measures what the portfolio actually did; trade-attribution measures a fiction at the boundary of manual-placement / partial-fills / GTC-cross-week / re-placement-after-cancel. Record the rejected alternative explicitly so the next agent doesn't add it back as a "missing KPI."
  2. **No materialised metrics table at Phase 4.8.** Live queries are well under email-render budget at single-user scale; a cache introduces staleness and migrations. Document the upgrade trigger: if any metric query exceeds 500ms at the email-send measure point, *then* add `weekly_metrics_cache`. Not before.
  3. **Honest accounting for the manual-placement gap.** The system surfaces `accepted_not_routed` rather than guessing. Record why the alternative (reconciling via `positions_snapshot` delta) was rejected at Phase 4.8: false-positive matches when independent price moves shift `market_value` by the same dollar amount as a suggested qty. Phase 5+ may revisit with per-execution position deltas if that signal becomes important.

About 30 minutes.

---

## 9. Documentation drift to fix

- **CLAUDE.md** — add `services/weekly_review_metrics.py` and the five new `sql/*.sql` files to repo layout. Add to common gotchas: the operations-week model means GTC fills can cross suggestion-week boundaries (pitfall 6) — the trend table's "filled" column lags by design.
- **`product_plan.md`** — add a **Phase 4.8 — Weekly order activity summary** entry, mark code-complete with the standard "tag deferred until 2 observed Friday emails" pattern, earliest tag date = second Friday after merge.
- **`README.md`** — update the "what's in the Friday email" list to include the Order Activity section; update test count.
- **ADR index** — add 0023.
- **`pre_phase_5_manual_testing_checklist.md`** — add a row: "cross-check every Order Activity headline against hand-written SQL for one Friday before tag." This is the only way to catch a quietly wrong query in production.

---

## 10. What Phase 4.8 deliberately does not include

- **Trade-attributable fill rate.** Discussed above and in ADR-0023. The honest metrics are drift + funnel; the seductive one is a fiction.
- **P&L, return, or benchmark comparison.** Different problem class — needs a price-history join over `price_bar` (analytics tier), a definition of "cost basis" that handles partial fills and dividends, and a benchmark feed. Phase 5+ at earliest, and arguably a separate phase ("retrospective performance review") since the email already runs long.
- **Per-suggestion timing analytics** (time from suggestion → accepted → routed → filled). Useful for tuning auto-trade but the natural home is a separate `/admin/auto-trade/status` page, not the weekly email.
- **Materialised `weekly_metrics_cache`.** Performance escape hatch only — adopt when live queries cross 500ms, not before.
- **Mid-week activity emails.** The Sunday weekly-suggestions email looks forward; the Friday weekly-review looks back. Adding a Wednesday "how's the week going" email is a different product decision; not Phase 4.8.
- **Per-user dashboards** with these same metrics. Phase 5 productization.

---

*When all 10 smoke-test rows are green and you've received two consecutive Friday emails with the Order Activity section populated and every headline cross-checked against hand-written SQL, Phase 4.8 is done. Tag `v0.4.8.0`. The Phase 4 family is then complete (4 → 4.5 → 4.6 → 4.7 → 4.8) and Phase 5 can begin.*
