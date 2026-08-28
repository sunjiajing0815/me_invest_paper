# ADR-0036 — Paper-Only Public Build

**Date:** 2026-08-28  
**Status:** Accepted  
**Deciders:** Jane

---

## Context

This repository is the public build of a private system (`sunjiajing0815/me_investing`)
shared on LinkedIn as a portfolio piece. The private system supports live brokers and
opt-in automated order placement. Before sharing the code, every path that could reach
real money had to be closed — not documented as "don't do this," but made structurally
unreachable.

Four paths reached real money in the pre-strip build:

1. `BROKER=alpaca_live` — Alpaca's live (non-paper) trading account.
2. `BROKER=moomoo` — the Moomoo/Futu adapter, connected to Jane's funded account.
3. Auto-trade mode promoted to `LIVE` (see ADR-0014) — places real orders through
   whichever adapter is primary.
4. `POST /admin/broker-accounts` with `{"connection_config": {"paper": false}, ...}` —
   a second, independent door into adapter construction that does not go through the
   `BROKER` env var at all, and so is not closed by (1) or (2) alone.

A reader cloning this repository must not be able to lose money by running it, even by
mistake, even by supplying credentials for a live account.

## Decision

Four independent layers enforce a hard paper-only constraint, implemented in
`src/investor/safety.py`. Each layer alone is sufficient to block live trading; all four
are present so that defeating the constraint has to be a deliberate, multi-file change —
never an accidental one.

| Layer | Location | What it blocks |
|---|---|---|
| L0 | `AlpacaAdapter.__init__` (`brokers/alpaca.py`) | Raises `LiveTradingBlocked` if constructed with `paper=False`, regardless of caller. |
| L1 | `config.VALID_BROKERS` | Accepts only `alpaca_paper`. `BROKER=alpaca_live` or `BROKER=moomoo` fails `Settings()` validation at startup — the process never boots. |
| L2 | `make_adapter()` and `make_account_adapter()` (`brokers/__init__.py`) | Both factories hardcode `paper=True` on every `AlpacaAdapter` they construct, ignoring any `connection_config["paper"]` value supplied by the caller. This closes door (4): `POST /admin/broker-accounts` can no longer request a live account, because the factory never looks at what it was asked for. |
| L3 | `assert_paper_only(adapter)` (`services/auto_trade.py`) | Called immediately before the sole `submit_order()` call site. Refuses to place an order unless the adapter proves `adapter.paper is True` — a missing or falsy attribute fails closed, not open. |

`PAPER_ONLY = True` in `safety.py` names the invariant; `LiveTradingBlocked` is the
exception every layer raises, giving a single, greppable signature for "this build
refused to go live."

`tests/test_paper_only.py` exercises all four layers directly. `tests/test_no_live_trading.py`
is a grep CI gate (mirroring `tests/test_no_unauthorized_submit_order.py`) that fails the
build if `paper=False`, `alpaca_live`, or a `MoomooAdapter` import reappears anywhere in
`src/` outside `safety.py` itself.

## Consequences

- The Moomoo adapter (`brokers/moomoo.py`), its `opend_*` settings, the
  `moomoo_parallel` job, and the `futu-api` dependency are removed from this build
  entirely. **ADR-0018 (Moomoo parallel-run) stays** as a design record — it documents
  real decisions (OpenD-as-host-dependency, the `remark` ↔ `client_order_id` mapping,
  the five soak criteria) that are still true of the private build and worth showing.
- The multi-broker data model (ADR-0024) is unchanged: `broker_account`,
  `account_ref` partitioning, per-broker `auto_trade_state`, and the per-account job/API
  scoping all remain exactly as built. Multi-broker plumbing is a design the repository
  is meant to demonstrate; only the second live broker is absent.
- The `OFF` → `DRY_RUN` → `LIVE` auto-trade mode ladder (ADR-0014) is retained rather
  than collapsed to a permanent no-op, because the promotion discipline — soak windows,
  guards, the kill switch — is itself worth showing. `LIVE` remains reachable in this
  build, but L0–L2 guarantee that the only adapter it can ever reach is
  `AlpacaAdapter(paper=True)`; L3 is the final check immediately before the order would
  be submitted.
- **This constraint is specific to this public build.** The private build at
  `sunjiajing0815/me_investing` is not paper-only — it runs live Alpaca and Moomoo
  trading for Jane's actual portfolio, and `safety.py` does not exist there. A reader of
  this repository must not assume the private system carries the same restriction.
- The full pre-strip build — Moomoo adapter, live-broker settings, and all — is
  reachable at tag `v0.4.9a-full` for anyone who wants to see what was removed.
