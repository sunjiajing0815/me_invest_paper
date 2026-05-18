# ADR-0019 — Weekly Review Email Composition

**Date:** 2026-05-18  
**Status:** Accepted  
**Deciders:** Jane

---

## Context

The Friday weekly review email (`jobs/weekly_review.py`) is a reflection tool, not an
action tool. It covers the past week's activity and provides context for the upcoming Sunday
suggestions email. Its composition, section order, and data sources must be stable enough
for the user to navigate by memory.

## Decision

### Seven fixed sections (in order)

| # | Section | Data source |
|---|---------|-------------|
| 1 | Header — week-of date, equity, total realised PnL | `broker_account`, `order_execution` |
| 2 | Suggestions-vs-fills — suggested → user action → fill outcome | `order_suggestion`, `order_execution` |
| 3 | Drift state after this week — allocation % vs target %, bands | `positions_snapshot`, `target_allocation` |
| 4 | Material news this week — LLM-classified events for held tickers | `news_event` |
| 5 | Next Sunday preview — suggestions run without persisting (non-authoritative) | fresh `generate_suggestions()` call |
| 6 | Auto-trade activity — mode changes, placements, cap spend, kill-switch events | `auto_trade_promotion_log`, `order_execution`, `kill_switch_log` |
| 7 | Moomoo parallel status — position/account divergences (green ✓ if clean) | `jobs/moomoo_parallel.py` logs |

Section order is fixed. Removing or reordering sections requires a new ADR.

### Friday reflection vs Sunday action cadence

The weekly review runs at 17:00 ET Friday. The weekly suggestions run at 18:00 ET Sunday.
The review intentionally looks *backward* (what happened this week) while the suggestions
email looks *forward* (what to do next week). The preview in § 5 is clearly labelled
non-authoritative because market conditions change between Friday close and Sunday market
data.

### Session discipline for `WeeklyReview`

All ORM data must be extracted to plain Python inside the `with session_scope()` block.
The `WeeklyReview` frozen dataclass receives only primitive types, frozen dataclasses, and
plain dicts — never live SQLAlchemy ORM objects. This is the standard pattern from CLAUDE.md
§ Architecture convention #9 applied to the weekly review context.

The `_build_review()` helper performs all DB reads inside the session. Any exception in the
preview generation (§ 5) is caught and surfaced as an empty list with a warning — the rest
of the email must still render.

### Moomoo-section sunset criteria

Section 7 should be removed from the template after:
1. Moomoo is set to primary broker (the Alpaca paper account is no longer used for execution).
2. A 4-week post-flip observation period has passed with no divergence alerts.

Removal requires a template edit and a `jobs/moomoo_parallel.py` retirement, but no new ADR.

## Consequences

- `templates/weekly_review.html.j2` and `templates/weekly_review.txt.j2` implement the
  7-section layout.
- `WeeklyReview` is a frozen dataclass — all fields are plain types.
- The admin endpoint `POST /admin/run-weekly-review` allows manual triggering for testing.
- Cron runs at 17:00 ET Friday with a 1-hour misfire grace window.
