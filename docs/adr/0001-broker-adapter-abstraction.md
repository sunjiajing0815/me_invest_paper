# ADR-0001: Broker Adapter Abstraction and Market-Data Separability

**Date:** 2026-04-28 (retroactive — decision taken during Phase 0; written up 2026-08-28)
**Status:** Accepted
**Deciders:** Jane

> **Backfill note.** This decision was made and enforced from Phase 0 onward, but the ADR
> file was never written. It is reconstructed here from the constraint as it exists in the
> code (`src/investor/brokers/base.py`, the `make_adapter` factory) and from the six places
> that cite it: ADR-0018, `plans/phase_2_completion.md`, and `plans/phase_4_guide.md`
> (§488, §569, §1137, §1167). Nothing here is new policy — it is the rule the codebase has
> followed since the beginning, finally given its own file.
>
> The number `0001` was originally earmarked in `plans/phase_0_guide.md` §14 for the
> rebalance-band formula. That assignment lapsed; every later citation of ADR-0001 means
> the broker-adapter decision recorded here. The band decision now lives in
> [ADR-0008](0008-rebalance-bands.md).

---

## Context

The product's long-term plan was always to start on one broker and move to another: Alpaca
first (free paper trading, REST-only, no gateway process, open to AU residents), with the
real capital living at a broker chosen later. A migration that requires touching the
snapshot service, the gap calculator, the suggestion engine, and the reporting layer is a
migration that never happens.

Two separate facts about brokers drove the design:

1. **Broker SDKs leak their shape everywhere they are imported.** `alpaca-py` returns
   numeric fields as strings, uses its own enums for order side and status, and models an
   account differently from any other vendor. Code written against those types is code
   rewritten when the broker changes.

2. **Market data and execution are separable products.** The broker holding the money need
   not be the broker supplying the price bars. Alpaca's IEX bars are free and adequate;
   Moomoo's free-tier market-data quota is materially lower. Binding both concerns to one
   "the broker" object would force a downgrade in data quality purely as a side effect of
   moving where the cash sits.

## Decision

**1. `BrokerAdapter` is the only door to a broker SDK.**

No file outside `src/investor/brokers/*` may import `alpaca`, or any future broker SDK.
Everything else in the application speaks the frozen dataclasses defined in
`brokers/base.py` — `Account`, `Position`, `Activity`, `OrderRequest`, `OrderConfirmation`
— and the `BrokerAdapter` Protocol over them.

Adapters convert vendor types to domain types *at the boundary*. `alpaca-py`'s stringified
numerics are wrapped in `float()` inside `AlpacaAdapter`, never by a caller.

**2. Market data may come from a different adapter than execution data.**

`get_bars` is not part of the `BrokerAdapter` Protocol precisely so that the bar source can
be chosen independently of the execution broker. Bars stay on Alpaca regardless of which
broker holds the positions.

**3. Domain IDs are not broker IDs.**

Domain tables key on `(broker_account_id, ticker)`, never on a vendor's `asset_id`. Vendor
identifiers live in sidecar columns for reconciliation only. See
[ADR-0024](0024-multi-broker-single-user-data-model.md) for how this generalised to
multiple simultaneous accounts.

## Consequences

**Good:**

- A broker swap is an adapter plus a factory branch. Phase 4 added a second broker against
  a live gateway without changing the snapshot, gap, suggestion, or reporting layers.
- Adapters are trivially fakeable in tests — the Protocol is small and the dataclasses are
  frozen, so the suite exercises broker-dependent logic with no network and no SDK.
- A broker with a weak market-data tier can still be adopted for execution.

**Costs:**

- Every adapter carries conversion code that would be unnecessary if the app spoke one
  vendor's types directly.
- The Protocol is a lowest-common-denominator surface. Broker-specific capabilities are
  either modelled for all adapters or hidden inside one (see ADR-0018's
  `client_order_id` ↔ `remark` mapping, which exists entirely to keep a vendor quirk
  invisible to callers).
- The rule needs enforcement, not just documentation. It is restated as convention 1 in
  `CLAUDE.md` and, for the order-submission path specifically, guarded by the grep test in
  `tests/test_no_unauthorized_submit_order.py`.

## Notes for this build

This is the paper-only public build. `AlpacaAdapter` is the only adapter that ships, and
the factory accepts only `alpaca_paper` — see
[ADR-0036](0036-paper-only-public-build.md). The abstraction itself is unchanged, and the
multi-broker data model it feeds is retained in full; only the second adapter is absent.
