"""Tests for load_targets_into_db() — idempotency and versioning."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from investor.config import load_targets
from investor.db import override_engine_for_testing
from investor.models import Base, TargetAllocation
from investor.services.targets import load_targets_into_db, yaml_hash

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
    engine = create_engine("duckdb:///:memory:", poolclass=StaticPool, future=True)
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
        result = load_targets_into_db(db_session, targets, yaml_hash(str(f)))
        db_session.commit()
        assert result == "updated"

    def test_idempotent_same_hash_returns_unchanged(
        self, db_session: Session, tmp_path: Path
    ) -> None:
        f = tmp_path / "targets.yaml"
        f.write_text(YAML_V1)
        targets = load_targets(str(f))
        h = yaml_hash(str(f))
        load_targets_into_db(db_session, targets, h)
        db_session.commit()

        result2 = load_targets_into_db(db_session, targets, h)
        db_session.commit()
        result3 = load_targets_into_db(db_session, targets, h)
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
            load_targets_into_db(db_session, targets, h)
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
        load_targets_into_db(db_session, targets_v1, yaml_hash(str(f1)))
        db_session.commit()

        targets_v2 = load_targets(str(f2))
        load_targets_into_db(db_session, targets_v2, yaml_hash(str(f2)))
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

        load_targets_into_db(db_session, load_targets(str(f1)), yaml_hash(str(f1)))
        db_session.commit()
        load_targets_into_db(db_session, load_targets(str(f2)), yaml_hash(str(f2)))
        db_session.commit()

        open_rows = {
            r.ticker: r
            for r in db_session.query(TargetAllocation)
            .filter(TargetAllocation.effective_to.is_(None))
            .all()
        }
        assert open_rows["VOO"].target_pct == pytest.approx(35.0)
        assert open_rows["AAPL"].target_pct == pytest.approx(10.0)
