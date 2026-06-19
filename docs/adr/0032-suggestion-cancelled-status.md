# ADR-0032 — Suggestion Status `cancelled` is Terminal; Auto-Trade Ignores It

**Status:** Accepted
**Date:** 2026-06-09
**Commits:** `72d0d5c` … `0b48307`

## Context

Through Phase 4 the `order_suggestion` lifecycle had `pending | accepted | rejected | expired`.
The un-accept product question — "I changed my mind after accepting" — had no first-class
representation. Two operational gaps:

1. No way to un-accept without going to the broker UI. The post-Phase-4 GTC update excluded
   `broker_cancelled` execution rows from `_check_idempotency` so legitimate cancel-and-re-place
   flows worked — but as a side effect, a manual broker-UI cancel plus a still-`accepted`
   suggestion meant auto-trade would re-place the order the next morning.
2. No audit distinction between "declined before acting" and "changed mind after acting".

## Decision

A new terminal status **`cancelled`**: `pending | accepted | rejected | expired | cancelled`
(string column; no migration). Distinct semantics:

- `rejected` = declined *before* acting (no `order_execution` ever existed).
- `cancelled` = un-accepted *after* acting (an `order_execution` may have been routed).
- `expired` = no action by the Friday rollover.

`auto_trade._fetch_accepted_unexecuted` selects only `accepted` rows, so `cancelled` is ignored
by the re-placement loop — closing the GTC-update footgun.

**Shared cancel helper** `services/orders.py::cancel_working_execution`: re-queries the broker
for authoritative status, then — filled → refuse; partially_filled → cancel remainder (filled
shares stand); working → cancel + flip to `broker_cancelled`; terminal/cancel-failure → leave
for reconciliation. The expiry sweep was refactored onto this same helper (gaining partial-fill
handling).

**Un-accept entry point** `services/unaccept.py::unaccept_suggestion`: guard `accepted`, cancel
via the helper, refuse if fully filled, else flip the suggestion to `cancelled`.

**Endpoints:** `GET /suggestions/{sid}/unaccept` renders a confirm page (no side effect; shows
live broker status); `POST` performs the action; HMAC-signed via `sign_action(sid, "unaccept",
…)` for email-linkable use. Admin variant: `POST /admin/suggestions/{sid}/unaccept`.

**Daily email "Open & Committed Orders"** section lists this-week `accepted` suggestions with
their latest execution status and a signed Un-accept link on cancellable rows.

## Consequences

- First-class un-accept path; audit distinction between "declined" and "changed mind" preserved.
- The shared cancel helper deduplicates a previously-divergent code path (un-accept + expiry).
- **Known gap — manual broker-UI cancel without clicking the un-accept link.** A user who
  cancels in the broker UI and doesn't click the link leaves the suggestion `accepted`;
  reconciliation marks the execution `broker_cancelled`; auto-trade re-places next morning
  (exactly the footgun this ADR set out to close). The un-accept path closes the footgun *only
  when the user uses the link*. Mitigation deferred (tracked in `plans/post_4_9a_changes.md`):
  detect a manual broker cancel via reconciliation and auto-mark `cancelled` if no user action
  within N hours.
- Negative: the GET confirm page queries the broker for live status, so URL prefetchers
  (Microsoft 365 SafeLinks, Slack unfurl, Gmail link-preview) hit the GET on hover — benign at
  solo scale, could rate-limit the broker at multi-tenant scale (tracked).

## References

- `services/orders.py::cancel_working_execution`, `services/unaccept.py::unaccept_suggestion`,
  `jobs/suggestion_expiry.py` (refactored onto the helper),
  `services/daily_report.py::CommittedOrderRow`.
- Templates: `daily_report.*`, `unaccept_confirm.html.j2`, `unaccept_result.html.j2`.
- Related: the post-Phase-4 GTC update introduced the `broker_cancelled` re-place behaviour this
  ADR partially closes.
