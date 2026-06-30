"""Tests for _load() — idempotency and versioning."""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from investor.config import load_targets
from investor.db import override_engine_for_testing
from investor.models import (
    Base,
    OrderExecution,
    OrderSuggestion,
    TargetAllocation,
    TargetChangeEvent,
)
from investor.services.targets import compute_target_shifts, yaml_hash

_ACCT = 1  # account_ref for these single-account tests


def _load(session, targets, content_hash, adapter=None):
    """Wrapper: load_targets_into_db scoped to the single test account."""
    from investor.services.targets import load_targets_into_db as _f
    return _f(session, targets, content_hash, broker_account_id=_ACCT, adapter=adapter)

YAML_V1 = textwrap.dedent("""\
    watchlist: [VOO, QQQ, SCHD, AAPL, MSFT, AMZN]
    targets:
      VOO:  { pct: 30, band: [25, 35] }
      QQQ:  { pct: 20, band: [16, 24] }
      SCHD: { pct: 15, band: [12, 18] }
      AAPL: { pct: 15, band: [12, 18] }
      MSFT: { pct: 10, band: [7,  13] }
      AMZN: { pct: 5,  band: [3,   7] }
    cash_buffer_pct: 5
""")

YAML_V2 = textwrap.dedent("""\
    watchlist: [VOO, QQQ, SCHD, AAPL, MSFT, AMZN]
    targets:
      VOO:  { pct: 35, band: [30, 40] }
      QQQ:  { pct: 20, band: [16, 24] }
      SCHD: { pct: 15, band: [12, 18] }
      AAPL: { pct: 10, band: [7,  13] }
      MSFT: { pct: 10, band: [7,  13] }
      AMZN: { pct: 5,  band: [3,   7] }
    cash_buffer_pct: 5
""")


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    override_engine_for_testing(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


class TestLoadTargetsIntoDb:
    def test_first_load_returns_updated(self, db_session: Session, tmp_path: Path) -> None:
        f = tmp_path / "targets.yaml"
        f.write_text(YAML_V1)
        targets = load_targets(str(f))
        result = _load(db_session, targets, yaml_hash(str(f)))
        db_session.commit()
        assert result == "updated"

    def test_idempotent_same_hash_returns_unchanged(
        self, db_session: Session, tmp_path: Path
    ) -> None:
        f = tmp_path / "targets.yaml"
        f.write_text(YAML_V1)
        targets = load_targets(str(f))
        h = yaml_hash(str(f))
        _load(db_session, targets, h)
        db_session.commit()

        result2 = _load(db_session, targets, h)
        db_session.commit()
        result3 = _load(db_session, targets, h)
        db_session.commit()

        assert result2 == "unchanged"
        assert result3 == "unchanged"

    def test_idempotent_leaves_correct_open_row_count(
        self, db_session: Session, tmp_path: Path
    ) -> None:
        f = tmp_path / "targets.yaml"
        f.write_text(YAML_V1)
        targets = load_targets(str(f))
        h = yaml_hash(str(f))

        for _ in range(3):
            _load(db_session, targets, h)
            db_session.commit()

        open_rows = (
            db_session.query(TargetAllocation)
            .filter(TargetAllocation.effective_to.is_(None))
            .count()
        )
        assert open_rows == 6

    def test_versioning_closes_old_rows_on_change(
        self, db_session: Session, tmp_path: Path
    ) -> None:
        f1 = tmp_path / "v1.yaml"
        f1.write_text(YAML_V1)
        f2 = tmp_path / "v2.yaml"
        f2.write_text(YAML_V2)

        targets_v1 = load_targets(str(f1))
        _load(db_session, targets_v1, yaml_hash(str(f1)))
        db_session.commit()

        targets_v2 = load_targets(str(f2))
        _load(db_session, targets_v2, yaml_hash(str(f2)))
        db_session.commit()

        closed = (
            db_session.query(TargetAllocation)
            .filter(TargetAllocation.effective_to.isnot(None))
            .count()
        )
        open_rows = (
            db_session.query(TargetAllocation)
            .filter(TargetAllocation.effective_to.is_(None))
            .count()
        )
        assert closed == 6
        assert open_rows == 6

    def test_versioning_open_rows_match_v2_values(
        self, db_session: Session, tmp_path: Path
    ) -> None:
        f1 = tmp_path / "v1.yaml"
        f1.write_text(YAML_V1)
        f2 = tmp_path / "v2.yaml"
        f2.write_text(YAML_V2)

        _load(db_session, load_targets(str(f1)), yaml_hash(str(f1)))
        db_session.commit()
        _load(db_session, load_targets(str(f2)), yaml_hash(str(f2)))
        db_session.commit()

        open_rows = {
            r.ticker: r
            for r in db_session.query(TargetAllocation)
            .filter(TargetAllocation.effective_to.is_(None))
            .all()
        }
        assert open_rows["VOO"].target_pct == pytest.approx(35.0)
        assert open_rows["AAPL"].target_pct == pytest.approx(10.0)

    def test_target_change_expires_accepted_suggestion_for_removed_ticker(
        self, db_session: Session, tmp_path: Path
    ) -> None:
        # Load v1 with AAPL + TSLA
        yaml_v1 = textwrap.dedent("""\
            watchlist: [AAPL, TSLA]
            targets:
              AAPL: { pct: 50, band: [40, 60] }
              TSLA: { pct: 50, band: [40, 60] }
            cash_buffer_pct: 0
        """)
        f1 = tmp_path / "v1.yaml"
        f1.write_text(yaml_v1)
        targets_v1 = load_targets(str(f1))
        _load(db_session, targets_v1, yaml_hash(str(f1)))
        db_session.commit()

        # Create an accepted suggestion for AAPL for the current week
        today = datetime.now(UTC).date()
        current_week_monday = today - timedelta(days=today.weekday())
        sug = OrderSuggestion(broker_account_id=_ACCT, 
            week_of=current_week_monday,
            ticker="AAPL",
            side="buy",
            qty=1.0,
            limit_price=150.0,
            reason="test",
            status="accepted",
        )
        db_session.add(sug)
        db_session.commit()

        # Load v2 with only TSLA (AAPL removed)
        yaml_v2 = textwrap.dedent("""\
            watchlist: [TSLA]
            targets:
              TSLA: { pct: 100, band: [90, 100] }
            cash_buffer_pct: 0
        """)
        f2 = tmp_path / "v2.yaml"
        f2.write_text(yaml_v2)
        targets_v2 = load_targets(str(f2))
        _load(db_session, targets_v2, yaml_hash(str(f2)))
        db_session.commit()

        db_session.refresh(sug)
        assert sug.status == "expired"
        assert sug.acted_at is not None

    def test_target_change_keeps_suggestion_for_retained_ticker(
        self, db_session: Session, tmp_path: Path
    ) -> None:
        # Load v1 with TSLA only
        yaml_v1 = textwrap.dedent("""\
            watchlist: [TSLA]
            targets:
              TSLA: { pct: 100, band: [90, 100] }
            cash_buffer_pct: 0
        """)
        f1 = tmp_path / "v1.yaml"
        f1.write_text(yaml_v1)
        targets_v1 = load_targets(str(f1))
        _load(db_session, targets_v1, yaml_hash(str(f1)))
        db_session.commit()

        # Create an accepted suggestion for TSLA for the current week
        today = datetime.now(UTC).date()
        current_week_monday = today - timedelta(days=today.weekday())
        sug = OrderSuggestion(broker_account_id=_ACCT, 
            week_of=current_week_monday,
            ticker="TSLA",
            side="buy",
            qty=2.0,
            limit_price=200.0,
            reason="test",
            status="accepted",
        )
        db_session.add(sug)
        db_session.commit()

        # Load v2 with TSLA at an updated band (still present; pct unchanged to satisfy validation)
        yaml_v2 = textwrap.dedent("""\
            watchlist: [TSLA]
            targets:
              TSLA: { pct: 100, band: [80, 100] }
            cash_buffer_pct: 0
        """)
        f2 = tmp_path / "v2.yaml"
        f2.write_text(yaml_v2)
        targets_v2 = load_targets(str(f2))
        _load(db_session, targets_v2, yaml_hash(str(f2)))
        db_session.commit()

        db_session.refresh(sug)
        assert sug.status == "accepted"

    def test_target_change_cancels_live_order_for_removed_ticker(
        self, db_session: Session, tmp_path: Path
    ) -> None:
        # Load v1 with AAPL + TSLA
        yaml_v1 = textwrap.dedent("""\
            watchlist: [AAPL, TSLA]
            targets:
              AAPL: { pct: 50, band: [40, 60] }
              TSLA: { pct: 50, band: [40, 60] }
            cash_buffer_pct: 0
        """)
        f1 = tmp_path / "v1.yaml"
        f1.write_text(yaml_v1)
        targets_v1 = load_targets(str(f1))
        _load(db_session, targets_v1, yaml_hash(str(f1)))
        db_session.commit()

        # Create an accepted suggestion for AAPL for the current week
        today = datetime.now(UTC).date()
        current_week_monday = today - timedelta(days=today.weekday())
        sug = OrderSuggestion(broker_account_id=_ACCT, 
            week_of=current_week_monday,
            ticker="AAPL",
            side="buy",
            qty=1.0,
            limit_price=150.0,
            reason="test",
            status="accepted",
        )
        db_session.add(sug)
        db_session.flush()

        # Create a linked live execution row
        exe = OrderExecution(broker_account_id=_ACCT, 
            suggestion_id=sug.id,
            ticker="AAPL",
            side="buy",
            submitted_qty=1.0,
            filled_qty=0.0,
            limit_price=150.0,
            status="accepted_for_routing",
            dry_run=False,
            broker_order_id="ord-001",
            broker="alpaca",
            match_method="auto_trade_placed",
        )
        db_session.add(exe)
        db_session.commit()

        # Load v2 with only TSLA (AAPL removed), passing a mock adapter
        yaml_v2 = textwrap.dedent("""\
            watchlist: [TSLA]
            targets:
              TSLA: { pct: 100, band: [90, 100] }
            cash_buffer_pct: 0
        """)
        f2 = tmp_path / "v2.yaml"
        f2.write_text(yaml_v2)
        targets_v2 = load_targets(str(f2))
        mock_adapter = MagicMock()
        _load(db_session, targets_v2, yaml_hash(str(f2)), adapter=mock_adapter)
        db_session.commit()

        mock_adapter.cancel_order.assert_called_once_with("ord-001")
        db_session.refresh(exe)
        assert exe.status == "broker_cancelled"

    def test_target_change_no_adapter_does_not_crash(
        self, db_session: Session, tmp_path: Path
    ) -> None:
        # Load v1 with AAPL + TSLA
        yaml_v1 = textwrap.dedent("""\
            watchlist: [AAPL, TSLA]
            targets:
              AAPL: { pct: 50, band: [40, 60] }
              TSLA: { pct: 50, band: [40, 60] }
            cash_buffer_pct: 0
        """)
        f1 = tmp_path / "v1.yaml"
        f1.write_text(yaml_v1)
        targets_v1 = load_targets(str(f1))
        _load(db_session, targets_v1, yaml_hash(str(f1)))
        db_session.commit()

        # Create an accepted suggestion for AAPL for the current week
        today = datetime.now(UTC).date()
        current_week_monday = today - timedelta(days=today.weekday())
        sug = OrderSuggestion(broker_account_id=_ACCT, 
            week_of=current_week_monday,
            ticker="AAPL",
            side="buy",
            qty=1.0,
            limit_price=150.0,
            reason="test",
            status="accepted",
        )
        db_session.add(sug)
        db_session.flush()

        # Create a linked live execution row
        exe = OrderExecution(broker_account_id=_ACCT, 
            suggestion_id=sug.id,
            ticker="AAPL",
            side="buy",
            submitted_qty=1.0,
            filled_qty=0.0,
            limit_price=150.0,
            status="accepted_for_routing",
            dry_run=False,
            broker_order_id="ord-002",
            broker="alpaca",
            match_method="auto_trade_placed",
        )
        db_session.add(exe)
        db_session.commit()

        # Load v2 with only TSLA — no adapter passed
        yaml_v2 = textwrap.dedent("""\
            watchlist: [TSLA]
            targets:
              TSLA: { pct: 100, band: [90, 100] }
            cash_buffer_pct: 0
        """)
        f2 = tmp_path / "v2.yaml"
        f2.write_text(yaml_v2)
        targets_v2 = load_targets(str(f2))
        # Must not raise even though there's a live execution with no adapter
        _load(db_session, targets_v2, yaml_hash(str(f2)))
        db_session.commit()

        db_session.refresh(sug)
        db_session.refresh(exe)
        assert sug.status == "expired"
        assert exe.status == "accepted_for_routing"  # unchanged — no cancellation attempted


def test_targets_are_per_broker_account(db_session: Session, tmp_path: Path) -> None:
    """Targets for account A and B are independent; reloading A leaves B untouched."""
    from investor.services.targets import load_targets_into_db

    yaml_a = textwrap.dedent("""\
        watchlist: [VOO, QQQ]
        targets:
          VOO: { pct: 60, band: [50, 70] }
          QQQ: { pct: 40, band: [30, 50] }
        cash_buffer_pct: 0
    """)
    yaml_b = textwrap.dedent("""\
        watchlist: [TSLA]
        targets:
          TSLA: { pct: 100, band: [90, 100] }
        cash_buffer_pct: 0
    """)
    fa, fb = tmp_path / "a.yaml", tmp_path / "b.yaml"
    fa.write_text(yaml_a)
    fb.write_text(yaml_b)

    load_targets_into_db(db_session, load_targets(str(fa)), yaml_hash(str(fa)), broker_account_id=1)
    load_targets_into_db(db_session, load_targets(str(fb)), yaml_hash(str(fb)), broker_account_id=2)
    db_session.commit()

    def _active(bid: int) -> set[str]:
        return {
            r.ticker
            for r in db_session.query(TargetAllocation).filter(
                TargetAllocation.broker_account_id == bid,
                TargetAllocation.effective_to.is_(None),
            )
        }

    assert _active(1) == {"VOO", "QQQ"}
    assert _active(2) == {"TSLA"}

    # Reload account 1 with a changed allocation — account 2 must be untouched.
    yaml_a2 = textwrap.dedent("""\
        watchlist: [VOO]
        targets:
          VOO: { pct: 100, band: [90, 100] }
        cash_buffer_pct: 0
    """)
    fa.write_text(yaml_a2)
    load_targets_into_db(db_session, load_targets(str(fa)), yaml_hash(str(fa)), broker_account_id=1)
    db_session.commit()

    assert _active(1) == {"VOO"}
    assert _active(2) == {"TSLA"}  # B's open rows were not closed by A's reload


# ── P2.1: target_change_event audit ──────────────────────────────────────────

def test_compute_target_shifts_changed_added_removed() -> None:
    shifts = compute_target_shifts({"VOO": 30.0, "MU": 5.0}, {"VOO": 35.0, "TSLA": 5.0})
    assert shifts["VOO"] == pytest.approx(5.0)    # changed
    assert shifts["MU"] == pytest.approx(-5.0)    # removed → to 0
    assert shifts["TSLA"] == pytest.approx(5.0)   # added → from 0


def test_change_event_written_with_diff_and_max_shift(
    db_session: Session, tmp_path: Path
) -> None:
    import json
    f = tmp_path / "t.yaml"
    f.write_text(YAML_V1)
    _load(db_session, load_targets(str(f)), yaml_hash(str(f)), )
    f.write_text(YAML_V2)  # VOO 30 → 35, AAPL 15 → 10
    _load(db_session, load_targets(str(f)), yaml_hash(str(f)))
    db_session.commit()

    events = db_session.query(TargetChangeEvent).order_by(TargetChangeEvent.id).all()
    assert len(events) == 2                          # one per applied change
    assert events[0].source == "admin_endpoint"
    latest = events[1]
    assert latest.max_shift_pp == pytest.approx(5.0)  # VOO/AAPL both 5pp
    diff = json.loads(latest.diff_json)
    assert diff["old"]["VOO"] == pytest.approx(30.0)
    assert diff["new"]["VOO"] == pytest.approx(35.0)


def test_no_change_event_when_hash_unchanged(db_session: Session, tmp_path: Path) -> None:
    f = tmp_path / "t.yaml"
    f.write_text(YAML_V1)
    h = yaml_hash(str(f))
    _load(db_session, load_targets(str(f)), h)
    _load(db_session, load_targets(str(f)), h)  # unchanged → no-op
    db_session.commit()
    assert db_session.query(TargetChangeEvent).count() == 1


def test_change_event_source_threaded(db_session: Session, tmp_path: Path) -> None:
    from investor.services.targets import load_targets_into_db
    f = tmp_path / "t.yaml"
    f.write_text(YAML_V1)
    load_targets_into_db(
        db_session, load_targets(str(f)), yaml_hash(str(f)),
        broker_account_id=_ACCT, source="yaml_direct",
    )
    db_session.commit()
    assert db_session.query(TargetChangeEvent).one().source == "yaml_direct"
