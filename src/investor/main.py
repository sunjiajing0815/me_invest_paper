"""FastAPI application — Phase 3a.

Endpoints:
  GET   /health                        — status, broker, last sync ts, target count
  GET   /positions                     — latest portfolio snapshot rows
  GET   /gap                           — current allocation vs targets (% and USD, band_status)
  GET   /drift                         — only out-of-band gap rows
  GET   /indicators                    — technical indicators per ticker (SMA, RSI, MACD)
  GET   /suggestions                   — pending order suggestions for current week
  PATCH /suggestions/{sid}             — accept or reject a suggestion (requires X-Admin-Token)
  GET   /suggestions/{sid}/{action}    — magic-link accept/reject from weekly email
  POST  /admin/run-sync                — ad-hoc sync trigger (requires X-Admin-Token)
  POST  /admin/run-daily-report        — manual trigger for daily report job (requires X-Admin-Token)
  POST  /admin/run-weekly-suggestions  — manual weekly suggestions trigger (requires X-Admin-Token)
  POST  /admin/reload-targets          — reload targets from targets.yaml (requires X-Admin-Token)
  POST  /admin/run-movers             — trigger movers email job (requires X-Admin-Token)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .brokers import make_adapter
from .config import Settings, load_targets
from .db import init_db, session_scope
from .jobs.daily_report import run_daily_report
from .jobs.movers import run_movers_email
from .jobs.sync import run_sync_job
from .jobs.weekly_suggestions import run_weekly_suggestions
from .queries import account_last_sync, positions_latest, targets_active_count
from .scheduler import make_scheduler
from .services.email import SMTPEmailer
from .services.gap import GapRow, compute_gap
from .services.targets import load_targets_into_db, yaml_hash

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SuggestionActionRequest(BaseModel):
    """Request body for PATCH /suggestions/{sid}."""

    action: str  # "accept" | "reject"
    note: str | None = None

_settings: Settings | None = None


def _get_settings() -> Settings:
    if _settings is None:
        raise RuntimeError("Settings not initialised")
    return _settings


def admin_auth(
    x_admin_token: str = Header(default=""),
    settings: Settings = Depends(_get_settings),  # noqa: B008
) -> None:
    """Dependency: validates X-Admin-Token header against ADMIN_TOKEN setting."""
    if not settings.admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="invalid admin token")


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

    from .services.bars import update_bars as _update_bars
    try:
        _update_bars(
            targets.watchlist,
            _settings.alpaca_api_key,
            _settings.alpaca_secret_key,
            bars_dir=_settings.bars_dir,
        )
    except Exception as exc:
        logger.warning("Startup bar sync failed; continuing with existing bars: %s", exc)

    adapter = make_adapter(_settings)
    emailer = SMTPEmailer(
        host=_settings.smtp_host,
        port=_settings.smtp_port,
        user=_settings.smtp_user,
        password=_settings.smtp_app_password,
        from_addr=_settings.email_from,
    )

    from .services.llm import make_llm_client
    llm = make_llm_client(_settings)

    app.state.settings = _settings
    app.state.adapter = adapter
    app.state.emailer = emailer
    app.state.llm = llm

    daily_fn = partial(run_daily_report, _settings, adapter, emailer)
    weekly_fn = partial(run_weekly_suggestions, _settings, adapter, emailer, llm)
    movers_fn = partial(run_movers_email, _settings, adapter, emailer, llm)
    scheduler = make_scheduler(daily_fn, weekly_fn, movers_fn)
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
        "last_sync_ts": str(last_sync) if last_sync else None,
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
            "ts": str(r.ts) if r.ts else None,
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


@app.get("/indicators", summary="Technical indicators per ticker")
def indicators() -> list[dict[str, Any]]:
    """Return latest SMA-20/50/200, EMA-21, RSI-14, MACD per watchlist ticker."""
    from .config import load_targets
    from .services.indicators import compute_indicators

    settings = _get_settings()
    try:
        targets = load_targets(settings.targets_path)
        rows = compute_indicators(targets.watchlist, settings.bars_dir)
    except Exception as exc:
        logger.error("/indicators failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Indicator computation failed: {exc}") from exc

    return [
        {
            "ticker": r.ticker,
            "as_of": r.as_of.isoformat(),
            "close": r.close,
            "sma_20": r.sma_20,
            "sma_50": r.sma_50,
            "sma_200": r.sma_200,
            "ema_21": r.ema_21,
            "rsi_14": r.rsi_14,
            "macd": r.macd,
            "macd_signal": r.macd_signal,
            "pct_from_sma_50": r.pct_from_sma_50,
            "pct_from_sma_200": r.pct_from_sma_200,
        }
        for r in rows
    ]


@app.get("/suggestions", summary="Pending order suggestions for current week")
def suggestions() -> list[dict[str, Any]]:
    """Return pending order suggestions for the current week."""
    from .models import OrderSuggestion

    try:
        with session_scope() as session:
            rows = (
                session.query(OrderSuggestion)
                .filter(OrderSuggestion.status == "pending")
                .order_by(OrderSuggestion.week_of.desc(), OrderSuggestion.id)
                .all()
            )
            result = [
                {
                    "id": r.id,
                    "week_of": r.week_of.isoformat(),
                    "ticker": r.ticker,
                    "side": r.side,
                    "qty": r.qty,
                    "limit_price": r.limit_price,
                    "reason": r.reason,
                    "status": r.status,
                    "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                }
                for r in rows
            ]
    except Exception as exc:
        logger.error("/suggestions query failed: %s", exc)
        raise HTTPException(status_code=500, detail="Suggestions query failed") from exc

    return result


@app.patch(
    "/suggestions/{sid}",
    summary="Accept or reject a suggestion",
    dependencies=[Depends(admin_auth)],
)
def patch_suggestion(sid: int, body: SuggestionActionRequest) -> dict[str, Any]:
    """Accept or reject a pending order suggestion by ID."""
    from .models import OrderSuggestion

    if body.action not in ("accept", "reject"):
        raise HTTPException(status_code=400, detail="action must be 'accept' or 'reject'")

    new_status: str
    with session_scope() as session:
        row = session.get(OrderSuggestion, sid)
        if row is None:
            raise HTTPException(status_code=404, detail="suggestion not found")
        if row.status != "pending":
            raise HTTPException(status_code=409, detail=f"suggestion already {row.status}")
        row.status = body.action + "ed"  # "accepted" | "rejected"
        row.acted_at = datetime.now(UTC)
        if body.note:
            row.note = body.note
        session.flush()
        new_status = row.status

    return {"status": "ok", "id": sid, "new_status": new_status}


@app.get(
    "/suggestions/{sid}/{action}",
    summary="Magic-link accept/reject",
    response_class=HTMLResponse,
)
def suggestion_magic_link(
    sid: int,
    action: str,
    token: str,
    request: Request,
) -> HTMLResponse:
    """Handle magic-link accept/reject from the weekly email."""
    from .models import OrderSuggestion
    from .services.magic_link import verify_action

    settings = request.app.state.settings

    if action not in ("accept", "reject"):
        raise HTTPException(status_code=400, detail="invalid action")

    if not verify_action(sid, action, token, settings.magic_link_secret):
        raise HTTPException(status_code=400, detail="invalid or expired token")

    new_status: str
    with session_scope() as session:
        row = session.get(OrderSuggestion, sid)
        if row is None:
            raise HTTPException(status_code=404, detail="suggestion not found")
        if row.status != "pending":
            raise HTTPException(status_code=409, detail=f"suggestion already {row.status}")
        row.status = action + "ed"
        row.acted_at = datetime.now(UTC)
        session.flush()
        new_status = row.status

    return HTMLResponse(
        content=f"<h2>Suggestion #{sid} {new_status}.</h2>",
        status_code=200,
    )


@app.post("/admin/run-sync", summary="Ad-hoc sync trigger", dependencies=[Depends(admin_auth)])
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


@app.post(
    "/admin/run-daily-report",
    summary="Manual daily report trigger",
    dependencies=[Depends(admin_auth)],
)
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


@app.post(
    "/admin/reload-targets",
    summary="Reload targets from targets.yaml",
    dependencies=[Depends(admin_auth)],
)
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


@app.post(
    "/admin/run-weekly-suggestions",
    summary="Manual weekly suggestions trigger",
    dependencies=[Depends(admin_auth)],
)
def admin_run_weekly_suggestions(request: Request) -> dict[str, str]:
    """Trigger the weekly suggestions job — computes indicators, levels, suggestions, and emails."""
    settings = _get_settings()
    adapter = request.app.state.adapter
    emailer = request.app.state.emailer
    logger.info("Weekly suggestions triggered via POST /admin/run-weekly-suggestions")
    try:
        run_weekly_suggestions(settings, adapter, emailer, request.app.state.llm)
    except Exception as exc:
        logger.error("Weekly suggestions failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Weekly suggestions failed: {exc}") from exc
    return {"status": "ok", "message": "Weekly suggestions email sent"}


@app.post(
    "/admin/run-movers",
    summary="Manual movers email trigger",
    dependencies=[Depends(admin_auth)],
)
def admin_run_movers(request: Request) -> dict[str, str]:
    """Trigger the movers email job — fetches top movers, triages news, and emails."""
    run_movers_email(
        request.app.state.settings,
        request.app.state.adapter,
        request.app.state.emailer,
        request.app.state.llm,
    )
    return {"status": "ok"}
