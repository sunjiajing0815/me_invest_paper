# Paper-Only Public Build — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make this public repository incapable of reaching a live brokerage account, and remove the Moomoo adapter, while leaving the multi-broker architecture and its documented reasoning intact.

**Architecture:** A new `src/investor/safety.py` holds a four-layer paper-only invariant — the adapter constructor (L0), config validation (L1), both adapter factories (L2), and the `submit_order` chokepoint (L3). Moomoo wiring is deleted from code, config, scheduler, templates and dependencies, while Moomoo *rationale* prose in docstrings and ADR-0018 is deliberately kept. Two CI gates (a unit suite and a grep gate) prevent regression.

**Tech Stack:** Python 3.12, pydantic-settings, FastAPI, APScheduler, SQLAlchemy, pytest, mypy (strict), ruff, uv.

**Spec:** `docs/superpowers/specs/2026-08-28-paper-only-public-build-design.md`

## Global Constraints

- Every task ends with `uv run pytest` green, `uv run mypy src/` clean, `uv run ruff check` clean.
- Never use the `Edit`/`Write` tools' equivalents to bypass tests — TDD order is mandatory: failing test first, then implementation.
- No file outside `src/investor/brokers/*` may import a broker SDK (existing ADR constraint — do not violate).
- `PAPER_ONLY = True` and the class name `LiveTradingBlocked` are fixed public names; later tasks import them verbatim.
- The multi-broker data model is frozen. Do NOT alter `models.py` columns, `broker_account`, `account_ref` partitioning, or `auto_trade_state`. Comment-only edits to `models.py` are permitted.
- `config/targets.yaml` ships unchanged. Do not sanitise it.
- Do NOT delete ADR-0018, ADR-0024, or Moomoo prose that explains *why* code is shaped as it is.
- Commit after every task. Do not push until Task 5.

---

### Task 1: The paper-only invariant

**Files:**
- Create: `src/investor/safety.py`
- Create: `tests/test_paper_only.py`
- Create: `tests/test_no_live_trading.py`
- Modify: `src/investor/brokers/alpaca.py:35-38` (add `self.paper`, add L0 guard)
- Modify: `src/investor/config.py:18` (`VALID_BROKERS`)
- Modify: `src/investor/brokers/__init__.py` (both factories — L2)
- Modify: `src/investor/services/auto_trade.py:600` (L3)

**Interfaces:**
- Consumes: nothing (first task).
- Produces:
  - `investor.safety.PAPER_ONLY: bool`
  - `investor.safety.LiveTradingBlocked(RuntimeError)`
  - `investor.safety.assert_paper_flag(paper: bool, *, source: str) -> None`
  - `investor.safety.assert_paper_only(adapter: object) -> None`
  - `AlpacaAdapter.paper: bool` — a public attribute L3 reads.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_paper_only.py`:

```python
"""The paper-only invariant: this build must never reach a live account.

Four independent layers are asserted here — see docs/adr/0036-paper-only-public-build.md.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from investor.brokers import make_account_adapter
from investor.brokers.alpaca import AlpacaAdapter
from investor.config import Settings
from investor.safety import PAPER_ONLY, LiveTradingBlocked, assert_paper_flag, assert_paper_only


def _settings_ns() -> SimpleNamespace:
    return SimpleNamespace(alpaca_api_key="k", alpaca_secret_key="s")


def test_paper_only_flag_is_on() -> None:
    assert PAPER_ONLY is True


# ── L1: config ────────────────────────────────────────────────────────────────

def test_broker_alpaca_live_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROKER", "alpaca_live")
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    with pytest.raises(Exception, match="broker must be one of"):
        Settings()


def test_broker_moomoo_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROKER", "moomoo")
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    with pytest.raises(Exception, match="broker must be one of"):
        Settings()


def test_broker_alpaca_paper_still_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROKER", "alpaca_paper")
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    assert Settings().broker == "alpaca_paper"


# ── L0: adapter constructor ───────────────────────────────────────────────────

def test_alpaca_adapter_refuses_live() -> None:
    with patch("investor.brokers.alpaca.TradingClient"):
        with pytest.raises(LiveTradingBlocked):
            AlpacaAdapter("k", "s", paper=False)


def test_alpaca_adapter_paper_exposes_paper_attribute() -> None:
    with patch("investor.brokers.alpaca.TradingClient"):
        adapter = AlpacaAdapter("k", "s", paper=True)
    assert adapter.paper is True


# ── L2: factory ignores a live connection_config ──────────────────────────────

def test_make_account_adapter_ignores_paper_false() -> None:
    with patch("investor.brokers.alpaca.TradingClient") as mock_tc:
        adapter = make_account_adapter(
            broker="alpaca", connection_config={"paper": False}, settings=_settings_ns()
        )
    assert mock_tc.call_args.kwargs["paper"] is True
    assert adapter.paper is True


def test_make_account_adapter_moomoo_is_gone() -> None:
    with pytest.raises(NotImplementedError):
        make_account_adapter(broker="moomoo", connection_config={}, settings=_settings_ns())


# ── assert helpers ────────────────────────────────────────────────────────────

def test_assert_paper_flag_raises_on_false() -> None:
    with pytest.raises(LiveTradingBlocked):
        assert_paper_flag(False, source="test")


def test_assert_paper_flag_passes_on_true() -> None:
    assert assert_paper_flag(True, source="test") is None


def test_assert_paper_only_raises_on_live_adapter() -> None:
    with pytest.raises(LiveTradingBlocked):
        assert_paper_only(SimpleNamespace(paper=False))


def test_assert_paper_only_raises_when_attribute_missing() -> None:
    """An adapter that cannot prove it is paper-mode is refused, not trusted."""
    with pytest.raises(LiveTradingBlocked):
        assert_paper_only(SimpleNamespace())


def test_assert_paper_only_passes_on_paper_adapter() -> None:
    assert assert_paper_only(SimpleNamespace(paper=True)) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_paper_only.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'investor.safety'`

- [ ] **Step 3: Create `src/investor/safety.py`**

```python
"""Hard constraint: this build can only ever reach an Alpaca *paper* account.

This is the public build of a private system that supports live brokers
(Alpaca live, Moomoo). Live trading is removed here on purpose: anyone can
clone this repository, and nobody should be able to lose money by running it.

Four independent layers enforce the constraint. Each one alone is sufficient;
all four are present so that defeating it has to be deliberate, never accidental.

  L0  AlpacaAdapter.__init__  — refuses a live flag at construction
  L1  config.VALID_BROKERS    — refuses BROKER=alpaca_live / moomoo at startup
  L2  the adapter factories   — ignore a live connection_config from the admin API
  L3  services/auto_trade.py  — refuses to submit an order via a non-paper adapter

See docs/adr/0036-paper-only-public-build.md.
"""

from __future__ import annotations

PAPER_ONLY = True


class LiveTradingBlocked(RuntimeError):
    """Raised when any code path attempts to reach a live brokerage account."""


def assert_paper_flag(paper: bool, *, source: str) -> None:
    """Raise if live mode is requested. `source` names the caller for the message."""
    if not paper:
        raise LiveTradingBlocked(
            f"{source}: live trading is disabled in this build. "
            "This is a paper-only public build; see src/investor/safety.py."
        )


def assert_paper_only(adapter: object) -> None:
    """Raise unless `adapter` proves it is paper-mode.

    A missing `paper` attribute is treated as failure, not as a pass — an adapter
    that cannot prove it is paper-mode must never reach an order-submission path.
    """
    if getattr(adapter, "paper", None) is not True:
        raise LiveTradingBlocked(
            f"{type(adapter).__name__} is not a verified paper adapter — refusing to "
            "submit an order. This is a paper-only public build; see src/investor/safety.py."
        )
```

- [ ] **Step 4: Add L0 to `AlpacaAdapter`**

In `src/investor/brokers/alpaca.py`, add to the relative-import block (ruff's isort rules order `..safety` before `..services.suggest`; run `uv run ruff check --fix` if unsure):

```python
from ..safety import assert_paper_flag
```

Replace the constructor at lines 35-38:

```python
class AlpacaAdapter:
    def __init__(self, api_key: str, secret_key: str, *, paper: bool = True) -> None:
        # L0 of the paper-only invariant. The `paper` parameter is kept rather than
        # removed so the adapter still reads as a general design that has been
        # deliberately narrowed. See src/investor/safety.py.
        assert_paper_flag(paper, source="AlpacaAdapter")
        self.paper = paper
        self._client = TradingClient(api_key, secret_key, paper=paper)
        logger.info("AlpacaAdapter initialised in paper mode")
```

Also update the module docstring's last line, from
`Paper/live routing is via paper=True only; URL override is not supported.` to
`This build is paper-only — see src/investor/safety.py.`

- [ ] **Step 5: Add L1 to `config.py`**

Replace line 18:

```python
# Paper-only public build: alpaca_live and moomoo are deliberately absent.
# See src/investor/safety.py and docs/adr/0036-paper-only-public-build.md.
VALID_BROKERS = {"alpaca_paper"}
```

- [ ] **Step 6: Add L2 to both factories**

In `src/investor/brokers/__init__.py`, replace the whole body of `make_adapter` after the docstring:

```python
def make_adapter(settings: Settings) -> BrokerAdapter:
    """Return the correct adapter for the configured broker.

    Paper-only build: `alpaca_paper` is the only accepted broker (enforced again
    in config.VALID_BROKERS). See src/investor/safety.py.
    """
    if settings.broker == "alpaca_paper":
        return AlpacaAdapter(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
            paper=True,
        )
    raise NotImplementedError(
        f"Broker {settings.broker!r} is not available in this paper-only build."
    )
```

Replace the `alpaca` branch of `make_account_adapter` (and delete its `moomoo` branch entirely):

```python
    if broker == "alpaca":
        # L2: `connection_config["paper"]` is deliberately ignored. The admin API
        # (POST /admin/broker-accounts) is a second door into adapter construction,
        # and it must not be able to request a live account. See src/investor/safety.py.
        api_key = os.environ.get(
            connection_config.get("api_key_env", ""), settings.alpaca_api_key
        )
        secret = os.environ.get(
            connection_config.get("secret_env", ""), settings.alpaca_secret_key
        )
        return AlpacaAdapter(api_key=api_key, secret_key=secret, paper=True)
    raise NotImplementedError(
        f"Broker {broker!r} is not available in this paper-only build."
    )
```

Update `make_account_adapter`'s docstring: replace the phrase
`the bare adapter family ("alpaca" / "moomoo")` with `the bare adapter family ("alpaca")`.

- [ ] **Step 7: Add L3 to the submit chokepoint**

In `src/investor/services/auto_trade.py`, add to the imports:

```python
from ..safety import assert_paper_only
```

At line 600, replace `conf = adapter.submit_order(req)` with:

```python
                # L3 of the paper-only invariant — the last gate before real money
                # could move. See src/investor/safety.py.
                assert_paper_only(adapter)
                conf = adapter.submit_order(req)
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_paper_only.py -v`
Expected: all PASS.

- [ ] **Step 9: Write the grep CI gate**

Create `tests/test_no_live_trading.py`:

```python
"""CI gate: no live-trading affordance may reappear in src/.

Mirrors the test_no_unauthorized_submit_order.py idiom. safety.py is excluded
because it is the module that *names* the forbidden states in order to block them.
"""

import subprocess

_ALLOWED = ("safety.py",)


def _grep(pattern: str) -> list[str]:
    result = subprocess.run(
        ["grep", "-rn", pattern, "src/", "--include=*.py"],
        capture_output=True,
        text=True,
    )
    return [
        line
        for line in result.stdout.splitlines()
        if not any(allowed in line.split(":")[0] for allowed in _ALLOWED)
    ]


def test_no_paper_false_in_src() -> None:
    offenders = _grep("paper=False")
    assert offenders == [], (
        "live-mode adapter construction found in src/:\n" + "\n".join(offenders)
    )


def test_no_alpaca_live_in_src() -> None:
    offenders = _grep("alpaca_live")
    assert offenders == [], (
        "alpaca_live reference found in src/:\n" + "\n".join(offenders)
    )


def test_no_moomoo_adapter_import_in_src() -> None:
    offenders = _grep("MoomooAdapter")
    assert offenders == [], (
        "MoomooAdapter reference found in src/:\n" + "\n".join(offenders)
    )
```

- [ ] **Step 10: Run the gate**

Run: `uv run pytest tests/test_no_live_trading.py -v`
Expected: `test_no_paper_false_in_src` PASSES; `test_no_alpaca_live_in_src` FAILS (`main.py` still has `alpaca_live` in `broker_scope` and `SOAK_WINDOWS`); `test_no_moomoo_adapter_import_in_src` FAILS (`brokers/__init__.py` and `moomoo.py`). These two are fixed in Task 2 — that is expected and correct at this point.

- [ ] **Step 11: Commit**

```bash
git add src/investor/safety.py src/investor/brokers/alpaca.py src/investor/config.py \
        src/investor/brokers/__init__.py src/investor/services/auto_trade.py \
        tests/test_paper_only.py tests/test_no_live_trading.py
git commit -m "feat(safety): four-layer paper-only invariant

Adds src/investor/safety.py and enforces it at the adapter constructor,
config validation, both adapter factories, and the submit_order chokepoint.
The grep gate still fails on Moomoo/alpaca_live wiring — removed next."
```

---

### Task 2: Excise Moomoo

**Files:**
- Delete: `src/investor/brokers/moomoo.py`, `src/investor/jobs/moomoo_parallel.py`, `tests/test_moomoo.py`
- Modify: `src/investor/config.py:83-88` (drop `opend_*`)
- Modify: `src/investor/scheduler.py:9,51,128-140,201`
- Modify: `src/investor/main.py:52,247,256,269,323,1248,1258`
- Modify: `src/investor/models.py:362,395`
- Modify: `tests/test_broker_factory.py`
- Modify: `pyproject.toml`, `.env.example`, `docker-compose.yml`

**Interfaces:**
- Consumes: `investor.safety.LiveTradingBlocked` (Task 1) — not directly, but the grep gate from Task 1 is the acceptance criterion here.
- Produces: `make_scheduler()` without the `moomoo_parallel_func` parameter. Task 3 does not depend on this signature.

- [ ] **Step 1: Delete the Moomoo modules**

```bash
git rm src/investor/brokers/moomoo.py src/investor/jobs/moomoo_parallel.py tests/test_moomoo.py
```

- [ ] **Step 2: Drop the OpenD settings from `config.py`**

Delete lines 83-88 in `src/investor/config.py` — the whole block:

```python
    # Moomoo/Futu OpenD daemon settings (used when broker == "moomoo")
    opend_host: str = ""
    opend_port: int = 11111
    opend_security_firm: str = "FUTUSECURITIES"
    opend_rsa_key_path: str = ""  # in-container path to OpenD's RSA key (empty = unencrypted)
    opend_currency: str = "USD"  # base currency for Moomoo account totals (accinfo_query)
```

Note: pydantic-settings ignores unknown env vars, so a stale `.env` still setting `OPEND_HOST` will not break startup. Verify this in Step 8.

- [ ] **Step 3: Remove the scheduler job**

In `src/investor/scheduler.py`:
- Delete line 9 from the module docstring: `  moomoo_parallel       — Mon–Fri 16:50 ET, grace 30 min`
- Delete line 51: `    moomoo_parallel_func: Callable[[], None] | None = None,`
- Delete the entire block at lines 128-140 (`if moomoo_parallel_func is not None:` through its closing `)`)
- In the log message at line 201, change `" Reconciliation Mon–Fri 16:45 ET; Moomoo parallel Mon–Fri 16:50 ET;"` to `" Reconciliation Mon–Fri 16:45 ET;"`

- [ ] **Step 4: Remove the `main.py` wiring**

In `src/investor/main.py`:
- Delete line 52: `from .jobs.moomoo_parallel import run_moomoo_parallel`
- Delete line 256: `    moomoo_parallel_fn = partial(run_moomoo_parallel, _settings, adapter)`
- Delete line 269: `        moomoo_parallel_fn,`
- At line 247, change the comment `weekly_review stays primary-scoped (4.9a), moomoo_parallel` / `is the soak comparison and is unchanged.` to `weekly_review stays primary-scoped (4.9a).`
- At line 323, change `    broker: str  # "alpaca" | "moomoo" | …` to `    broker: str  # "alpaca" (paper-only build; see safety.py)`
- At line 1248, change `broker_scope: Literal["alpaca_paper", "alpaca_live", "moomoo"]` to `broker_scope: Literal["alpaca_paper"]`
- In `SOAK_WINDOWS`, delete both `("alpaca_live", "LIVE"): 28,` and `("moomoo", "LIVE"): 28,`, and change the `alpaca_paper` comment to read `# paper has no real money; this build cannot promote beyond paper`

**Keep** the docstring at line 772 that explains the Alpaca-positions-under-Moomoo bug — that is design rationale, not wiring.

- [ ] **Step 5: Annotate the `models.py` comments**

- Line 362: change `# "alpaca" | "moomoo"` to `# "alpaca" | "dry_run" (multi-broker by design; only Alpaca ships in this build)`
- Line 395: change `# alpaca_paper|alpaca_live|moomoo` to `# alpaca_paper (paper-only build; the column stays broad by design)`

Do not change any column type, nullability, or name.

- [ ] **Step 6: Drop the dependency and the env/compose references**

In `pyproject.toml`, delete the line `    "futu-api>=9.3",`.

In `.env.example`, change line 2 to `# Options: alpaca_paper   (paper-only public build — live brokers are removed)` and delete lines 45-52 (the whole `Phase 4 — Moomoo/Futu OpenD` block).

In `docker-compose.yml`, the `extra_hosts` comment at line 17 references OpenD — reword it to explain `host.docker.internal` generically, or drop `extra_hosts` if nothing else needs it. Check first with `grep -n "host.docker.internal" docker-compose.yml`.

Regenerate the lockfile and inspect the diff for anything beyond the `futu-api` removal:

```bash
uv lock
git diff --stat uv.lock
```

- [ ] **Step 7: Fix `tests/test_broker_factory.py`**

Replace `test_make_account_adapter_alpaca_paper_vs_live` (it currently asserts `paper=False` is honoured — the invariant inverts it):

```python
def test_make_account_adapter_always_builds_paper() -> None:
    """Paper-only build: connection_config cannot request a live account."""
    with patch("investor.brokers.alpaca.TradingClient") as mock_tc:
        paper_adapter = make_account_adapter(
            broker="alpaca", connection_config={"paper": True}, settings=_fake_settings()
        )
        assert isinstance(paper_adapter, AlpacaAdapter)
        assert mock_tc.call_args.kwargs["paper"] is True

        make_account_adapter(
            broker="alpaca", connection_config={"paper": False}, settings=_fake_settings()
        )
        assert mock_tc.call_args.kwargs["paper"] is True  # the False is ignored
```

In `_fake_settings()`, delete the now-unused `opend_host`, `opend_port` and `opend_security_firm` keys.

- [ ] **Step 8: Verify a stale OPEND_ env var does not break startup**

```bash
OPEND_HOST=host.docker.internal BROKER=alpaca_paper ALPACA_API_KEY=k ALPACA_SECRET_KEY=s \
  uv run python -c "from investor.config import Settings; print(Settings().broker)"
```
Expected: prints `alpaca_paper` with no error. If it raises, add `extra="ignore"` to the `SettingsConfigDict` in `config.py` and re-run.

- [ ] **Step 9: Run the full suite**

Run: `uv run pytest -q && uv run mypy src/ && uv run ruff check`
Expected: all green. `tests/test_no_live_trading.py` now passes all three tests.

Note: `tests/test_auto_trade_routing.py`, `tests/test_multibroker_sync.py`, `tests/test_snapshot.py` and `tests/test_suggestion_review.py` reference Moomoo only as a *string label* on a fake adapter or in regression docstrings. The `broker` column is a free-form varchar and the data model stays multi-broker, so these keep working unchanged. Do not edit them.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "refactor: remove the Moomoo adapter from the public build

Deletes brokers/moomoo.py, jobs/moomoo_parallel.py, their scheduler and
main.py wiring, the OPEND_* settings, and the futu-api dependency.
ADR-0018 and the Moomoo design rationale in docstrings are kept."
```

---

### Task 3: Retire weekly-review section 9

**Files:**
- Modify: `src/investor/jobs/weekly_review.py:108,240-245,292,509`
- Modify: `templates/weekly_review.txt.j2:190-197`
- Modify: `templates/weekly_review.html.j2:447-462`
- Modify: `tests/test_weekly_review.py:95,118,177`

**Interfaces:**
- Consumes: nothing from Tasks 1-2.
- Produces: `WeeklyReview` dataclass without the `moomoo_status` field.

ADR-0019 already defines Moomoo-section sunset criteria, so this follows the documented path. Reference that ADR in the commit message.

- [ ] **Step 1: Update the test first**

In `tests/test_weekly_review.py`:
- Delete the `moomoo_status="unavailable",` argument at line 95
- Delete the assertion at line 118: `assert wr.moomoo_status == "unavailable"`
- Delete line 177: `settings.opend_host = None`

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_weekly_review.py -v`
Expected: FAIL — `WeeklyReview.__init__() missing 1 required positional argument: 'moomoo_status'`

- [ ] **Step 3: Remove the field and its computation**

In `src/investor/jobs/weekly_review.py`:
- Delete line 108: `    moomoo_status: str  # "parallel_running" | "primary" | "unavailable"`
- Delete lines 240-245 (the `# Moomoo status` block through `moomoo_status = "parallel_running"`)
- Delete line 292: `        moomoo_status=moomoo_status,`
- Delete line 509: `        moomoo_status=review.moomoo_status,`

**Keep** the docstring at line 353 that explains per-account catch-up filtering using Moomoo as the example — that is rationale.

- [ ] **Step 4: Remove the template sections**

In `templates/weekly_review.txt.j2`, delete lines 190-197 — the `=== MOOMOO STATUS ===` heading and its whole `{% if %}…{% endif %}` block.

In `templates/weekly_review.html.j2`, delete lines 447-462 — the `{# ---- Moomoo status ---- #}` comment, the `{{ ui.section("Moomoo Status") }}` call, and the whole `{% if %}…{% endif %}` block.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_weekly_review.py tests/test_email_templates.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q && uv run mypy src/ && uv run ruff check`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(email): sunset weekly-review Moomoo status section

Follows the sunset criteria already documented in ADR-0019."
```

---

### Task 4: Documentation, ADR, and licence

**Files:**
- Create: `LICENSE`
- Create: `docs/adr/0036-paper-only-public-build.md`
- Modify: `README.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: the final state of Tasks 1-3 (names: `safety.py`, `PAPER_ONLY`, `LiveTradingBlocked`).
- Produces: nothing code-facing.

- [ ] **Step 1: Write `LICENSE`**

MIT, copyright `2026 Jane Sun`, with a disclaimer appended after the standard MIT text:

```
ADDITIONAL NOTICE

This software is a personal project shared for educational and portfolio
purposes. It is not financial advice, investment advice, or a recommendation
to buy or sell any security. It is configured for paper trading only and is
not intended for use with a live brokerage account. You are solely
responsible for any investment decisions you make.
```

- [ ] **Step 2: Write ADR-0036**

Create `docs/adr/0036-paper-only-public-build.md`, matching the format of the existing ADRs (open one first, e.g. `docs/adr/0014-auto-trade-mode-discipline.md`, and copy its heading structure verbatim).

Content requirements:
- **Status:** Accepted, 2026-08-28. **Deciders:** Jane.
- **Context:** this repository is the public build of a private system; three paths reached real money (`BROKER=alpaca_live`, `BROKER=moomoo`, auto-trade `LIVE`), plus a fourth door via `POST /admin/broker-accounts` with `{"paper": false}`, which bypasses `BROKER` entirely.
- **Decision:** the four layers (L0-L3) as implemented in `src/investor/safety.py`, listed in a table.
- **Consequences:** the Moomoo adapter is gone from this build but ADR-0018 stays as a design record; the multi-broker data model is unchanged; the `OFF`/`DRY_RUN`/`LIVE` ladder is retained because ADR-0014's promotion discipline is worth showing, and `LIVE` can only reach paper money.
- **Explicitly state** that this constraint is specific to the public build, and that the private build at `sunjiajing0815/me_investing` is not so constrained — a reader must not assume otherwise.
- **Note** that the full pre-strip build is reachable at tag `v0.4.9a-full`.

- [ ] **Step 3: Update `README.md`**

- Insert as the very first content after the `# Investor Assistant` heading, before the description:

```markdown
> ⚠️ **Paper trading only.** This build cannot connect to a live brokerage
> account. A four-layer invariant in [`src/investor/safety.py`](src/investor/safety.py)
> blocks live trading at the adapter, the config, both factories, and the
> order-submission chokepoint. See [ADR-0036](docs/adr/0036-paper-only-public-build.md).
> Nothing here is financial advice.
```

- Line 1 heading: change `# Investor Assistant — Phase 4.9a (soak window)` to `# Investor Assistant — Phase 4.9a (paper-only public build)`
- Line 3: change `Pulls positions from Alpaca (or Moomoo)` to `Pulls positions from Alpaca`
- Line 7 (`**Current phase:**`): rewrite to say this is the paper-only public build of a private multi-broker system, and that the multi-broker data model is retained.
- Schedule table (~line 115): delete the `16:50 | Mon–Fri | Moomoo parallel-run` row.
- Env-var table (~lines 53-55): delete the `OPEND_HOST` / `OPEND_PORT` / `OPEND_SECURITY_FIRM` rows.
- `POST /admin/broker-accounts` docs (~line 164): change the example body from `{"broker": "moomoo", …}` to `{"broker": "alpaca", …}` and note that `connection_config.paper` is ignored.
- Auto-trade soak table (~line 296): delete the `moomoo | LIVE | 28` row.
- Repo-layout listing (~line 896, ~941): delete the `moomoo.py` and `moomoo_parallel.py` lines; add a `safety.py    paper-only invariant (L0–L3)` line.
- Test listing (~line 993): delete the `test_moomoo.py` line; add `test_paper_only.py` and `test_no_live_trading.py`.
- ADR listing (~line 1023): keep the ADR-0018 line, add an ADR-0036 line.
- Data-model tables (~lines 686, 706, 738): these describe the multi-broker model and mention Moomoo as the second broker — keep them, but add a parenthetical that Moomoo does not ship in this build.

- [ ] **Step 4: Update `CLAUDE.md`**

- Mission paragraph: change `(Alpaca + Moomoo as of Phase 4.9a; IBKR + Tiger are deferred follow-ons)` to note that this public build ships Alpaca paper only.
- Add to the top, under `## Current phase`, a short **Paper-only build** note pointing at `src/investor/safety.py` and ADR-0036.
- Repo layout block: delete the `moomoo.py` and `moomoo_parallel.py` lines; add `safety.py`.
- Gotchas 4, 16, 17, 18 are Moomoo-operational (OpenD host dependency, bind address, prefix stripping, `client_order_id`↔`remark`) — delete them, since the adapter they describe is gone. Renumber nothing else; leave the remaining numbers as they are to avoid churn.
- `## Things to never do`: add `**Never remove or weaken a layer of the paper-only invariant.** See src/investor/safety.py and ADR-0036.`
- Required env vars: delete the `OPEND_*` line from the Phase 4 section.

- [ ] **Step 5: Verify no dangling references**

```bash
grep -rn "moomoo_parallel\|MoomooAdapter\|OPEND_\|opend_\|alpaca_live" README.md CLAUDE.md src/ tests/ .env.example docker-compose.yml pyproject.toml
```
Expected: only hits inside `docs/adr/0018-*.md`, `docs/adr/0036-*.md`, `plans/`, and prose that deliberately explains history. Zero hits in `src/`, `.env.example`, `docker-compose.yml`, `pyproject.toml`.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "docs: paper-only build notice, ADR-0036, and MIT licence

Adds a LICENSE (MIT + not-financial-advice notice), ADR-0036 recording the
paper-only decision, and updates README/CLAUDE.md for the Alpaca-paper-only
build. ADR-0018 and the multi-broker design narrative are retained."
```

---

### Task 5: Final verification and push

**Files:** none modified.

**Interfaces:**
- Consumes: everything from Tasks 1-4.
- Produces: the published public build.

- [ ] **Step 1: Full green run**

```bash
uv run pytest -q
uv run mypy src/
uv run ruff check
```
Expected: all three clean. Record the pytest pass count.

- [ ] **Step 2: Verify the app still boots**

```bash
BROKER=alpaca_paper ALPACA_API_KEY=dummy ALPACA_SECRET_KEY=dummy \
SQLITE_PATH=./data/investor.db TARGETS_PATH=./config/targets.yaml \
  uv run python -c "import investor.main; print('import ok')"
```
Expected: `import ok` with no ImportError from the deleted modules.

- [ ] **Step 3: Prove the invariant end-to-end**

```bash
uv run python -c "
from unittest.mock import patch
from investor.safety import LiveTradingBlocked
from investor.brokers.alpaca import AlpacaAdapter
with patch('investor.brokers.alpaca.TradingClient'):
    try:
        AlpacaAdapter('k','s',paper=False)
        raise SystemExit('FAIL: live adapter was constructed')
    except LiveTradingBlocked as e:
        print('BLOCKED:', e)
"
```
Expected: prints `BLOCKED: AlpacaAdapter: live trading is disabled in this build. …`

- [ ] **Step 4: Confirm no secrets are staged**

```bash
git status --short
git ls-files | grep -iE '(^|/)\.env$|\.db$|\.duckdb$' || echo "clean"
```
Expected: `clean`, and a `.env` file must never appear.

- [ ] **Step 5: Push**

```bash
git push origin main
```

- [ ] **Step 6: Report**

State the final pytest count, confirm the three verification commands passed, and give the repository URL. Do not claim completion without the actual command output.
