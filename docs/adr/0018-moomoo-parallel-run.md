# ADR-0018 — Moomoo Parallel-Run

**Date:** 2026-05-18  
**Status:** Accepted  
**Deciders:** Jane

---

## Context

The long-term target is to execute real trades through the Moomoo account where actual
long-term capital lives. Phase 4 adds `MoomooAdapter` but does not yet flip Moomoo to
primary. Instead, a 4+ week parallel-run validates the adapter against Alpaca as a known-good
reference before any primary flip.

## Decision

### OpenD is a host-side dependency, not containerised

Moomoo's API gateway (OpenD) runs as a native process on macOS/Windows. The Docker container
connects to it via `host.docker.internal:11111`. OpenD must never be installed inside the app
image.

Required env vars (default values shown):
```
OPEND_HOST=host.docker.internal
OPEND_PORT=11111
OPEND_SECURITY_FIRM=FUTUSECURITIES
```

### Bars always come from Alpaca

`MoomooAdapter.get_bars()` raises `NotImplementedError`. Historical bar data is always fetched
via `AlpacaAdapter` (Alpaca IEX free tier). Even when trading via Moomoo, the Alpaca paper
account must remain active for bar access. See also ADR-0001 (original bar-source decision).

### `remark` ↔ `client_order_id` mapping (ADR-0018 canonical mapping)

Moomoo's order API does not have a `client_order_id` field. The adapter stores the
`client_order_id` in Moomoo's `remark` field when placing orders, and reads it back from
`remark` in `get_order()` and `get_activities()`. This mapping is enforced at the adapter
boundary — no other file should know that `client_order_id` lives in `remark`.

### `US.` ticker prefix stripping

Moomoo's API returns tickers with a market prefix (`US.AAPL`, `HK.0700`). The
`_strip_market_prefix()` helper strips the prefix at the adapter boundary. No ticker with a
market prefix should ever appear outside `brokers/moomoo.py`.

When submitting orders, the adapter adds `US.` prefix internally:
`code = f"US.{req.ticker}"`. Callers always use bare tickers (`AAPL`, not `US.AAPL`).

### Parallel-run cron

`jobs/moomoo_parallel.py` runs at 16:50 ET Mon–Fri (5 minutes after daily reconciliation)
and compares Moomoo positions/account against Alpaca. Divergences are logged at WARNING but
not emailed daily — they surface in Friday's weekly review (§ Moomoo status).

### Five soak-success criteria (gates primary flip)

Before flipping Moomoo to primary, all five must be green for ≥4 consecutive weeks:

1. No divergence alerts in `moomoo_parallel` logs.
2. All `get_activities()` fills reconcile correctly with suggestions.
3. `client_order_id` round-trips through the `remark` field with no truncation.
4. Account equity (within 2%) and individual position quantities (within 0.01 shares) match
   Alpaca at each daily comparison point.
5. No `RET_OK != 0` errors in any of the four API calls (`accinfo_query`,
   `position_list_query`, `deal_list_query`, `place_order`).

### Moomoo-status section sunset

The Moomoo parallel-status section in the Friday weekly review email should be removed after
the primary flip and a 4-week post-flip observation period. A future commit removes the
section and retires `jobs/moomoo_parallel.py`.

## Consequences

- `MoomooAdapter` is the only file that imports `futu`. Imports are lazy (`import futu` in
  `__init__`) so the module remains importable in tests without OpenD running.
- `futu-api>=9.3` must be in `pyproject.toml` dependencies.
- Alpaca paper account must remain active even during and after the Moomoo primary flip
  (for bar data access).
