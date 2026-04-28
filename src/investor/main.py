"""FastAPI application — Phase 0 MVP.

Endpoints:
  GET  /health                  — status, broker, last sync ts, target count
  GET  /positions               — latest portfolio snapshot rows
  GET  /gap                     — current allocation vs targets (% and USD)
  POST /admin/run-sync          — ad-hoc sync trigger
  POST /admin/reload-targets    — reload targets from targets.yaml
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from functools import partial
from typing import Any

from fastapi import FastAPI, HTTPException

from .config import Settings, load_targets
from .queries import account_last_sync, positions_latest, targets_active_count
from .db import init_db, session_scope
from .jobs.sync import run_sync_job
from .scheduler import make_scheduler
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
    """Startup: init DB, load targets, start scheduler. Shutdown: stop scheduler."""
    global _settings

    _settings = Settings()
    logging.getLogger().setLevel(_settings.log_level.upper())

    logger.info("Starting Investor Assistant (broker=%s)", _settings.broker)
    init_db(_settings.sqlite_path)

    targets = load_targets(_settings.targets_path)
    logger.info("Targets validated: %d tickers", len(targets.targets))

    sync_fn = partial(run_sync_job, _settings)
    scheduler = make_scheduler(sync_fn)
    scheduler.start()
    app.state.scheduler = scheduler

    yield

    logger.info("Shutting down scheduler")
    scheduler.shutdown(wait=False)


app = FastAPI(
    title="Investor Assistant",
    description="Phase 0 MVP — portfolio gap analysis",
    version="0.0.1",
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
        raise HTTPException(status_code=500, detail="Database query failed")

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


@app.get("/gap", summary="Allocation gap vs targets")
def gap() -> list[dict[str, Any]]:
    """Return gap between current allocation and targets, sorted by abs(gap_pct) desc."""
    try:
        with session_scope() as session:
            rows: list[GapRow] = compute_gap(session)
    except Exception as exc:
        logger.error("/gap query failed: %s", exc)
        raise HTTPException(status_code=500, detail="Gap computation failed")

    return [
        {
            "ticker": r.ticker,
            "current_pct": round(r.current_pct, 4),
            "target_pct": round(r.target_pct, 4),
            "gap_pct": round(r.gap_pct, 4),
            "gap_usd": round(r.gap_usd, 2),
        }
        for r in rows
    ]


@app.post("/admin/run-sync", summary="Ad-hoc sync trigger")
def admin_run_sync() -> dict[str, str]:
    """Trigger an immediate sync from the broker. Runs synchronously."""
    settings = _get_settings()
    logger.info("Ad-hoc sync triggered via POST /admin/run-sync")
    try:
        run_sync_job(settings)
    except Exception as exc:
        logger.error("Ad-hoc sync failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Sync failed: {exc}")
    return {"status": "ok", "message": "Sync completed"}


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
        raise HTTPException(status_code=500, detail=f"Reload failed: {exc}")
    return {"status": "ok", "result": result}
