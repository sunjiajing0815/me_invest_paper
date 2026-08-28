# Paper-Only Public Build — Design

**Date:** 2026-08-28
**Status:** Approved (pending spec review)
**Deciders:** Jane
**Supersedes:** nothing
**Related:** ADR-0014 (auto-trade mode discipline), ADR-0018 (Moomoo parallel run),
ADR-0019 (weekly review composition), ADR-0024 (multi-broker data model)

## Context

`sunjiajing0815/me_invest_paper` is a new public repository created to share this
project on LinkedIn. The private original, `sunjiajing0815/me_investing`, remains the
working trading system and keeps the full multi-broker build.

The public build must not let a reader who clones it spend real money. Three paths to
real money exist in the full build:

| Path | Mechanism |
|---|---|
| `BROKER=alpaca_live` | `VALID_BROKERS` accepts it; `make_adapter` builds `AlpacaAdapter(paper=False)` |
| `BROKER=moomoo` | Real Moomoo account via host-side OpenD; `paper=False` is hardcoded |
| auto-trade `LIVE` | `services/auto_trade.py` calls `adapter.submit_order()` after promotion |

A fourth, less obvious door: `make_account_adapter()` builds from a `broker_account`
row, so `POST /admin/broker-accounts` with `{"broker": "alpaca", "connection_config":
{"paper": false}}` yields a live adapter regardless of the `BROKER` env var. Any lock
must cover both factories.

## Goals

1. No configuration of this build can reach a live brokerage account.
2. The multi-broker architecture and its reasoning stay legible to a reader — the
   design is the thing worth showing, and gutting the data model would obscure it.
3. The repo is legally shareable and honestly labelled.

## Non-goals

- Rewriting git history. The full build is reachable at tag `v0.4.9a-full`; recovering
  it is a deliberate act, which is an acceptable bar.
- Changing the data model. `broker_account`, `account_ref` partitioning, and per-broker
  `auto_trade_state` stay exactly as they are.
- Removing the auto-trade subsystem. The `OFF` → `DRY_RUN` → `LIVE` ladder, promotion
  token, spending caps, and kill switch all remain; `LIVE` simply cannot reach beyond
  paper money.
- Sanitising `config/targets.yaml`. The owner elected to ship the real allocation.

## Design

### 1. The paper-only invariant

A new module `src/investor/safety.py` holds the constraint in one greppable place:

```python
"""Hard constraint: this build can only ever reach an Alpaca *paper* account.

This is a public build of a private system that supports live brokers. Each layer
below is independently sufficient; all four are present so that defeating the
constraint has to be deliberate, never accidental.
"""

PAPER_ONLY = True


class LiveTradingBlocked(RuntimeError):
    """Raised when any code path attempts to reach a live brokerage account."""


def assert_paper_only(adapter: object) -> None:
    """Raise LiveTradingBlocked unless `adapter` is demonstrably paper-mode."""
```

`assert_paper_only` reads a `paper` attribute off the adapter and raises when it is
absent or falsy. Absence is treated as failure, not as a pass — an adapter that cannot
prove it is paper-mode is refused.

Four enforcement layers:

| # | Location | Blocks |
|---|---|---|
| L0 | `AlpacaAdapter.__init__` raises `LiveTradingBlocked` when `paper is False` | Every caller, including code written later |
| L1 | `config.py`: `VALID_BROKERS = {"alpaca_paper"}` | `BROKER=alpaca_live` and `BROKER=moomoo`, at startup, with a clear error |
| L2 | `make_adapter` and `make_account_adapter` pass `paper=True` unconditionally, ignoring `connection_config["paper"]` | The `POST /admin/broker-accounts` door |
| L3 | `assert_paper_only(adapter)` immediately before `adapter.submit_order(req)` in `services/auto_trade.py` | Anything that reached the order path regardless |

The `paper` parameter stays on `AlpacaAdapter` rather than being deleted. Its docstring
explains the constraint. This keeps the adapter reading as a general design that has
been deliberately narrowed rather than as a crippled class.

### 2. Moomoo excision

Deleted outright:

- `src/investor/brokers/moomoo.py`
- `src/investor/jobs/moomoo_parallel.py`
- `tests/test_moomoo.py`
- The `moomoo` branch in `make_adapter` and in `make_account_adapter`
- `opend_host`, `opend_port`, `opend_security_firm`, `opend_rsa_key_path`,
  `opend_currency` from `config.py`
- `moomoo_parallel_func` parameter, job registration, and docstring lines in
  `scheduler.py`; the `run_moomoo_parallel` import and `moomoo_parallel_fn` wiring in
  `main.py`
- `futu-api>=9.3` from `pyproject.toml`, with `uv.lock` regenerated
- The `OPEND_*` block from `.env.example`; the OpenD comment on `extra_hosts` in
  `docker-compose.yml`

Weekly review section 9 (`moomoo_status` on the report dataclass, its computation in
`jobs/weekly_review.py`, and the blocks in `templates/weekly_review.html.j2` and
`weekly_review.txt.j2`) is removed. ADR-0019 already defines Moomoo-section sunset
criteria, so this follows the documented path rather than working around it.

`main.py` narrows `broker_scope` from `Literal["alpaca_paper", "alpaca_live",
"moomoo"]` to `Literal["alpaca_paper"]`, and drops the `("moomoo", "LIVE"): 28` soak
entry.

Kept deliberately:

- ADR-0018 in full, as the record of a design decision.
- ADR-0024 and the multi-broker data model.
- README and CLAUDE.md prose explaining why the model is multi-broker.
- Incidental prose references to Moomoo in docstrings that describe *why* a piece of
  code is shaped as it is (`jobs/sync.py`, `services/snapshot.py`, `main.py`,
  `graphs/suggestion_review.py`, `services/auto_trade.py`, `brokers/base.py`). These
  are design rationale, not wiring.

`models.py` comments reading `# "alpaca" | "moomoo"` gain a short note that Moomoo is
designed-for but not shipped in this build, so a reader does not hit a dangling
reference.

### 3. Public-facing surface

- A `> ⚠️ **Paper trading only.**` callout as the first content in `README.md`, above
  the description, stating that the build cannot reach a live account and pointing at
  `safety.py`.
- **ADR-0036 — Paper-only public build**, recording this decision in the repository's
  own idiom.
- A `LICENSE` file: MIT, plus an explicit notice that the software carries no warranty
  and is not financial or investment advice. Without a license the repository is "all
  rights reserved" and cannot legally be used or forked.
- README updates: broker sections, the schedule table (drop the 16:50 Moomoo parallel
  row), the env-var table (drop `OPEND_*`), the repo-layout listing, the test-file
  listing, and the auto-trade soak table.
- CLAUDE.md updates: mission paragraph, current-phase note, repo layout, the Moomoo
  gotchas (numbered 4, 16, 17, 18), required-env-vars section, and a new note
  describing the paper-only invariant.

### 4. Testing

Test-driven: the invariant tests are written first and must fail before the
implementation lands.

New `tests/test_paper_only.py`:

- `Settings(broker="alpaca_live")` raises `ValidationError`
- `Settings(broker="moomoo")` raises `ValidationError`
- `AlpacaAdapter(..., paper=False)` raises `LiveTradingBlocked`
- `make_account_adapter(broker="alpaca", connection_config={"paper": False}, ...)`
  builds a paper adapter rather than honouring the flag
- `assert_paper_only` raises on an object with `paper=False` and on one with no
  `paper` attribute
- The auto-trade `LIVE` path raises `LiveTradingBlocked` rather than calling
  `submit_order` when handed a non-paper adapter

New `tests/test_no_live_trading.py`, copying the `test_no_unauthorized_submit_order.py`
grep-gate idiom: assert that neither `paper=False` nor `alpaca_live` appears anywhere
under `src/`.

Existing tests requiring updates: `test_broker_factory.py` (its
`test_make_account_adapter_alpaca_paper_vs_live` currently asserts `paper=False`
works — it inverts), `test_auto_trade_routing.py`, `test_multibroker_sync.py`,
`test_weekly_review.py`, `test_snapshot.py`, `test_config.py`,
`test_suggestion_review.py`.

Completion bar: `uv run pytest` fully green, `uv run mypy src/` clean under strict,
`uv run ruff check` clean.

## Risks

| Risk | Mitigation |
|---|---|
| A test fixture constructs `AlpacaAdapter(paper=False)` and now raises | Swept during the test-update pass; the grep gate catches stragglers in `src/` |
| Removing `opend_*` settings breaks a `.env` that still sets them | pydantic-settings ignores unknown env vars by default; verified during implementation |
| `uv.lock` regeneration pulls unrelated upgrades | Regenerate with `uv lock` and inspect the diff for anything beyond the `futu-api` removal |
| A reader assumes the private build is equally locked | ADR-0036 states plainly that this constraint is specific to the public build |

## Sequencing

1. Tag `v0.4.9a-full` on the pre-strip commit. *(done — pushed 2026-08-28)*
2. Write failing invariant tests.
3. Implement `safety.py` and the four layers.
4. Excise Moomoo across code, config, templates, and dependencies.
5. Update the existing test suite.
6. Rewrite docs: README, CLAUDE.md, ADR-0036, LICENSE.
7. Full green run, then commit and push to `main`.
