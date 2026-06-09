# Design spec — Un-accept path + daily order status (2026-06-09)

## Problem

Once a suggestion is `accepted`, there is no way to pull it back. The accept/reject
endpoints 409 on anything but `pending`, so:
- A working LIVE order (auto-trade placed, unfilled) can only be stopped by cancelling
  in the broker UI — and even then auto-trade **re-places** it the same week (a
  `broker_cancelled` execution doesn't block re-placement; the suggestion stays
  `accepted`).
- An accepted-but-unplaced to-do (manual / auto-trade-OFF account) can't be un-committed.
- The daily email shows no order/commitment status, so there's no place to see or act on
  a working order.

## Goals

1. **Daily email** shows a per-account "what's committed right now" view: working broker
   orders **and** accepted-but-unplaced suggestions, with status.
2. **Un-accept**: cancel any working broker order and revert the suggestion, safely, from
   the daily email.

## Decisions (confirmed)

- **Scope:** both — working LIVE orders *and* accepted manual/OFF to-dos, per account.
- **Result state:** new terminal suggestion status **`cancelled`** (distinct from
  `rejected`). Auto-trade only picks `status='accepted'`, so this also closes the
  re-place footgun.
- **Trigger:** prefetch-safe **two-step confirm page** (GET shows confirm, POST acts) —
  un-accept is destructive (cancels a real order) and email GET links get pre-fetched.
- **Race handling** (re-query broker at confirm): fully filled → refuse; partially filled →
  cancel remainder (keep filled shares); working → cancel; not-yet-placed → just mark.
- **Architecture:** shared cancel helper reused by un-accept *and* the expiry sweep; a thin
  un-accept service reused by the email endpoint and an admin endpoint.

Key constraint: `OrderConfirmation` (from `adapter.get_order`) exposes only `status` (no
`filled_qty`), so partial detection uses the status string; the partial *quantity* is
recorded by the next reconciliation pass via `get_activities` (`Activity.filled_qty`).

## Design

### 1. Data model
- Add `cancelled` to the suggestion status set: `pending | accepted | rejected | expired |
  cancelled`. No migration (free String column). Terminal.
- `cancelled` semantics: pulled back *after* accepting. `rejected` stays "declined before
  acting". Next week's fresh suggestion for the ticker is unaffected.

### 2. Shared cancel helper — `services/orders.py`
- `cancel_working_execution(adapter, session, execution) -> CancelOutcome` (extracted from
  the expiry sweep's inline logic). Re-queries `adapter.get_order(broker_order_id)`:
  - `filled` → `already_filled` (do **not** cancel).
  - `partially_filled` → `adapter.cancel_order` (cancels the remainder); `partial`. Filled
    shares stand; reconciliation records the filled qty later.
  - working (`new`/`accepted`/`held`/…) → `cancel_order`; set execution `broker_cancelled`;
    `cancelled`.
  - already terminal (`canceled`/`expired`/`rejected`) → `noop`.
- Refactor `jobs/suggestion_expiry.py` onto this helper (it gains explicit partial handling).

### 3. Un-accept service — `services/unaccept.py`
- `unaccept_suggestion(session, adapter, suggestion_id) -> UnacceptResult`:
  - Load suggestion (scoped by `broker_account_id`). Guard: only `accepted` is un-acceptable
    (else return a "not actionable" result with the current status).
  - Find the latest real (`dry_run=False`, `broker_order_id` set) execution. If present →
    `cancel_working_execution`; if `already_filled` → return `filled` (suggestion stays
    `accepted`/filled, no change). Else cancel.
  - If no execution (manual / OFF / not-yet-placed) → nothing to cancel.
  - Set suggestion `status='cancelled'`, `acted_at=now`.
  - Return a result enum: `cancelled | partial | filled | not_actionable | not_found`.
- Pure-ish service (opens no email/broker beyond the adapter passed in); reused by both
  endpoints.

### 4. Endpoints
- `GET /suggestions/{sid}/unaccept?token=` — verify HMAC (`verify_action(sid, "unaccept",
  token, secret)`); render an HTML **confirm page** with the order (ticker/side/qty/limit,
  live status from `get_order`) and a `POST` "Confirm cancel" button. **No side effects.**
- `POST /suggestions/{sid}/unaccept?token=` — re-verify token, resolve the account's adapter
  (`app.state.adapters[broker_account_id]`), call `unaccept_suggestion`, render the outcome.
- `POST /admin/suggestions/{sid}/unaccept` (admin token) — same service, for ops.
- `sign_action`/`verify_action` already accept a free action string → use `"unaccept"`; no
  change to `magic_link.py`.

### 5. Daily email — "Open & committed orders"
- Extend `compose_daily_report` (already has `session` + `broker_account_id`) to return
  **committed rows**: this-week `accepted` suggestions for the account joined to their latest
  real execution → `ticker, side, qty, limit_price, status_label, filled_price, sid`.
  `status_label` ∈ Working / Partially filled / Filled / Awaiting placement.
- New section in `daily_report.html.j2` (+ `.txt.j2`) via the shared component system: a
  table of committed rows; rows that are still cancellable (Working / Partially filled /
  Awaiting placement) render an **"Un-accept"** signed GET link; Filled rows don't.
- `jobs/daily_report.py` passes `base_url=settings.app_base_url` + a signed `unaccept` token
  per row to the render (mirrors weekly suggestions). Per-account already.

### 6. Consistency
- Weekly review "Suggestions vs Fills" renders `cancelled` distinctly (status already shown).
- Any query that assumed `accepted` is the only post-accept state (audit/expiry) accounts
  for `cancelled`.

## Acceptance criteria
- Un-accepting a working LIVE order cancels it at the broker and the suggestion shows
  `cancelled`; the next auto-trade pass does **not** re-place it.
- Un-accepting a manual/OFF accepted suggestion marks it `cancelled` (no broker call).
- Confirm page performs **no** action on GET (prefetch-safe); cancel happens only on POST.
- Fully-filled at confirm time → refused, suggestion unchanged; partial → remainder
  cancelled, filled shares retained and later recorded.
- Daily email lists the account's working orders + accepted to-dos with correct status and
  an un-accept link on cancellable rows only.
- Expiry sweep behaves as before, now via the shared helper (+ partial handling).

## Out of scope
- Un-doing a **filled** order (that's a new sell, not an un-accept).
- Changing accept/reject (they stay one-click; only the destructive un-accept gets a confirm
  page).
- Bulk un-accept.
