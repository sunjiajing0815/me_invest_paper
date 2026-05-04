"""FastAPI application — Phase 1.

Endpoints:
  GET  /health                    — status, broker, last sync ts, target count
  GET  /positions                 — latest portfolio snapshot rows
  GET  /gap                       — current allocation vs targets (% and USD, band_status)
  GET  /drift                     — only out-of-band gap rows
  POST /admin/run-sync            — ad-hoc sync trigger
  POST /admin/run-daily-report    — manual trigger for daily report job
  POST /admin/reload-targets      — reload targets from targets.yaml
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from functools import partial
from typing import Any

from fastapi import FastAPI, HTTPException

from .brokers import make_adapter
from .config import Settings, load_targets
from .db import init_db, session_scope
from .jobs.daily_report import run_daily_report
from .jobs.sync import run_sync_job
from .queries import account_last_sync, positions_latest, targets_active_count
from .scheduler import make_scheduler
from .services.email import SMTPEmailer
from .services.gap import GapRow, compute_gap
from .services.targets import load_targets_into_db, yaml_hash

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_settings: Settings | None = None


def _get_settings() -> Settings:
    if _settings is None:
        raise RuntimeError("Settings not initialised")
    return _settings


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    """Startup: init DB, load targets, build adapter+emailer, start scheduler."""
    global _settings

    _settings = Settings()
    logging.getLogger().setLevel(_settings.log_level.upper())

    logger.info("Starting Investor Assistant (broker=%s)", _settings.broker)
    init_db(_settings.sqlite_path)

    targets = load_targets(_settings.targets_path)
    logger.info("Targets validated: %d tickers", len(targets.targets))

    adapter = make_adapter(_settings)
    emailer = SMTPEmailer(
        host=_settings.smtp_host,
        port=_settings.smtp_port,
        user=_settings.smtp_user,
        password=_settings.smtp_app_password,
        from_addr=_settings.email_from,
    )

    app.state.settings = _settings
    app.state.adapter = adapter
    app.state.emailer = emailer

    daily_fn = partial(run_daily_report, _settings, adapter, emailer)
    scheduler = make_scheduler(daily_fn)
    scheduler.start()
    app.state.scheduler = scheduler

    yield

    logger.info("Shutting down scheduler")
    scheduler.shutdown(wait=False)


app = FastAPI(
    title="Investor Assistant",
    description="Phase 1 — daily portfolio email + drift detection",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health", summary="Health check")
def health() -> dict[str, Any]:
    """Return service status, broker, last sync timestamp, and target count."""
    settings = _get_settings()
    last_sync: datetime | None = None
    target_count = 0

    try:
        with session_scope() as session:
            row = session.execute(account_last_sync).fetchone()
            if row:
                last_sync = row[0]
            count_row = session.execute(targets_active_count).fetchone()
            if count_row:
                target_count = int(count_row[0])
    except Exception as exc:
        logger.warning("Health check DB query failed: %s", exc)

    return {
        "status": "ok",
        "broker": settings.broker,
        "last_sync_ts": last_sync.isoformat() if last_sync else None,
        "target_count": target_count,
    }


@app.get("/positions", summary="Latest positions snapshot")
def positions() -> list[dict[str, Any]]:
    """Return the most recent positions snapshot per ticker."""
    try:
        with session_scope() as session:
            rows = session.execute(positions_latest).fetchall()
    except Exception as exc:
        logger.error("/positions query failed: %s", exc)
        raise HTTPException(status_code=500, detail="Database query failed") from exc

    return [
        {
            "ticker": r.ticker,
            "ts": r.ts.isoformat() if r.ts else None,
            "qty": r.qty,
            "avg_cost": r.avg_cost,
            "market_value": r.market_value,
            "weight_pct": r.weight_pct,
        }
        for r in rows
    ]


def _gap_row_to_dict(r: GapRow) -> dict[str, Any]:
    return {
        "ticker": r.ticker,
        "current_pct": round(r.current_pct, 4),
        "target_pct": round(r.target_pct, 4),
        "gap_pct": round(r.gap_pct, 4),
        "gap_usd": round(r.gap_usd, 2),
        "band_status": r.band_status,
    }


@app.get("/gap", summary="Allocation gap vs targets")
def gap() -> list[dict[str, Any]]:
    """Return gap between current allocation and targets, sorted by abs(gap_pct) desc."""
    try:
        with session_scope() as session:
            rows: list[GapRow] = compute_gap(session)
    except Exception as exc:
        logger.error("/gap query failed: %s", exc)
        raise HTTPException(status_code=500, detail="Gap computation failed") from exc

    return [_gap_row_to_dict(r) for r in rows]


@app.get("/drift", summary="Out-of-band tickers only")
def drift() -> list[dict[str, Any]]:
    """Return only tickers whose current allocation is outside their rebalance band."""
    try:
        with session_scope() as session:
            rows: list[GapRow] = compute_gap(session)
    except Exception as exc:
        logger.error("/drift query failed: %s", exc)
        raise HTTPException(status_code=500, detail="Gap computation failed") from exc

    return [_gap_row_to_dict(r) for r in rows if r.band_status != "in_band"]


@app.post("/admin/run-sync", summary="Ad-hoc sync trigger")
def admin_run_sync() -> dict[str, str]:
    """Trigger an immediate sync from the broker. Runs synchronously."""
    settings = _get_settings()
    logger.info("Ad-hoc sync triggered via POST /admin/run-sync")
    try:
        run_sync_job(settings)
    except Exception as exc:
        logger.error("Ad-hoc sync failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Sync failed: {exc}") from exc
    return {"status": "ok", "message": "Sync completed"}


@app.post("/admin/run-daily-report", summary="Manual daily report trigger")
def admin_run_daily_report() -> dict[str, str]:
    """Manually trigger the daily report job — syncs, composes, and emails."""
    settings = _get_settings()
    adapter = app.state.adapter
    emailer = app.state.emailer
    logger.info("Daily report triggered via POST /admin/run-daily-report")
    try:
        run_daily_report(settings, adapter, emailer)
    except Exception as exc:
        logger.error("Daily report failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Daily report failed: {exc}") from exc
    return {"status": "ok", "message": "Daily report sent"}


@app.post("/admin/reload-targets", summary="Reload targets from targets.yaml")
def admin_reload_targets() -> dict[str, str]:
    """Reload target allocations from targets.yaml. No-op if file content is unchanged."""
    settings = _get_settings()
    try:
        h = yaml_hash(settings.targets_path)
        targets_cfg = load_targets(settings.targets_path)
        with session_scope() as sess:
            result = load_targets_into_db(sess, targets_cfg, h)
    except Exception as exc:
        logger.error("reload-targets failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Reload failed: {exc}") from exc
    return {"status": "ok", "result": result}
