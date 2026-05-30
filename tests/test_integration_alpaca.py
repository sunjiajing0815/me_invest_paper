"""Integration test: full chain against live Alpaca paper account.

Skipped automatically when ALPACA_API_KEY is not set.
Run with: uv run pytest -m integration -v
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from investor.brokers.alpaca import AlpacaAdapter
from investor.config import Settings, load_targets
from investor.db import override_engine_for_testing
from investor.models import Base
from investor.services.daily_report import compose_daily_report
from investor.services.email import FakeEmailer
from investor.services.render import render_template
from investor.services.snapshot import take_snapshot


@pytest.fixture()
def _settings() -> Settings:
    return Settings()


@pytest.fixture()
def _tmp_session(tmp_path):  # type: ignore[no-untyped-def]
    db_url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = create_engine(db_url, poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    override_engine_for_testing(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.mark.integration
def test_alpaca_paper_full_chain(_settings: Settings, _tmp_session: Session) -> None:
    if not os.getenv("ALPACA_API_KEY"):
        pytest.skip("ALPACA_API_KEY not set — skipping integration test")

    _acct = 1  # single-account integration run
    # Pull live positions from paper account first so the broker_account row exists
    adapter = AlpacaAdapter(
        api_key=_settings.alpaca_api_key,
        secret_key=_settings.alpaca_secret_key,
        paper=True,
    )
    take_snapshot(adapter, _tmp_session, _settings, _acct)
    _tmp_session.commit()

    # Wire targets into DB so gap query has targets to compare against
    from investor.services.targets import load_targets_into_db, yaml_hash
    h = yaml_hash(_settings.targets_path)
    targets_cfg = load_targets(_settings.targets_path)
    load_targets_into_db(_tmp_session, targets_cfg, h, broker_account_id=_acct)
    _tmp_session.commit()

    # Compose report and render both templates (no bars_dir in CI — indicators will be empty)
    report = compose_daily_report(_tmp_session, broker_account_id=_acct)
    html = render_template("daily_report.html.j2", report=report)
    text = render_template("daily_report.txt.j2", report=report)

    # Send via FakeEmailer and verify
    emailer = FakeEmailer()
    subject = f"Portfolio — {report.date:%Y-%m-%d}"
    emailer.send(to="test@example.com", subject=subject, html=html, text=text)

    assert len(emailer.sent) == 1
    sent = emailer.sent[0]
    assert sent["subject"].startswith("Portfolio —")
    assert "Portfolio" in sent["html"]
    assert "Portfolio" in sent["text"]

    # At least one watchlist ticker should appear in the email
    watchlist = targets_cfg.watchlist
    assert any(ticker in sent["html"] for ticker in watchlist), (
        f"None of {watchlist} found in HTML email body"
    )
