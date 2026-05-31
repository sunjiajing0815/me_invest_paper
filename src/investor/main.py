"""FastAPI application — Phase 3a/4.

Endpoints:
  GET   /health                         — status, broker, last sync ts, target count
  GET   /positions                      — latest portfolio snapshot rows
  GET   /gap                            — allocation vs targets (%, USD, band_status)
  GET   /drift                          — only out-of-band gap rows
  GET   /indicators                     — technical indicators per ticker (SMA, RSI, MACD)
  GET   /suggestions                    — pending order suggestions for current week
  PATCH /suggestions/{sid}              — accept or reject a suggestion (admin token)
  GET   /suggestions/{sid}/{action}     — magic-link accept/reject from weekly email
  POST  /admin/run-sync                 — ad-hoc sync trigger (admin token)
  POST  /admin/run-daily-report         — manual daily report trigger (admin token)
  POST  /admin/run-weekly-suggestions   — manual weekly suggestions trigger (admin token)
  POST  /admin/reload-targets           — reload targets from targets.yaml (admin token)
  POST  /admin/run-movers               — trigger movers email job (admin token)
  POST  /admin/reconcile/{execution_id} — manually link an execution to a suggestion
  POST  /admin/run-auto-trade           — manual auto-trade trigger (admin token)
  POST  /admin/auto-trade/promote       — promote auto-trade mode (promotion token)
  POST  /admin/auto-trade/caps          — update spending caps (promotion token)
  POST  /admin/cancel-all-orders          — cancel all open broker orders (admin token)
  POST  /admin/reset-week-suggestions     — cancel orders + reset to pending (admin token)
  POST  /admin/resend-weekly-email        — re-send weekly email from DB rows, no LLM (admin token)
  POST  /admin/auto-trade/emergency-stop      — trigger kill switch immediately (admin token)
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select

from .brokers import build_account_adapters, make_account_adapter, make_adapter
from .config import Settings, load_targets
from .db import init_db, session_scope
from .jobs.auto_trade import run_auto_trade_job, run_auto_trade_job_all_brokers
from .jobs.daily_report import (
    run_daily_report_all_brokers,
    run_daily_report_for_account,
)
from .jobs.moomoo_parallel import run_moomoo_parallel
from .jobs.movers import run_movers_email
from .jobs.reconciliation import (
    run_daily_reconciliation_all_brokers,
)
from .jobs.suggestion_expiry import (
    sweep_expired_suggestions_all_brokers,
)
from .jobs.sync import run_sync_for_account
from .jobs.weekly_review import run_weekly_review
from .jobs.weekly_suggestions import (
    run_weekly_suggestions_all_brokers,
    run_weekly_suggestions_for_account,
)
from .models import (
    AutoTradeCaps,
    AutoTradePromotionLog,
    AutoTradeState,
    BrokerAccount,
    OrderExecution,
    OrderSuggestion,
)
from .queries import account_last_sync, positions_latest, targets_active_count
from .scheduler import make_scheduler
from .services.accounts import (
    AccountInfo,
    list_active_accounts,
    resolve_active_account_refs,
)
from .services.auto_trade import (
    _get_mode,
    _trigger_kill_switch,
    resolve_primary_account_ref,
    set_mode,
)
from .services.daily_report import AccountSnapshot
from .services.email import SMTPEmailer
from .services.gap import GapRow, compute_gap, get_untracked_positions
from .services.indicators import compute_indicators
from .services.levels import build_nearby_levels, compute_levels
from .services.magic_link import sign_action
from .services.render import render_template
from .services.suggest import _next_monday
from .services.targets import (
    load_targets_into_db,
    targets_path_for_account,
    yaml_hash,
)

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


def _resolve_scope(
    broker_account_id: int | None, *, default: Literal["primary", "all"]
) -> list[int]:
    """Resolve which broker account(s) an endpoint should act on (B-API).

    A given ``broker_account_id`` must be an active account (404 otherwise) and yields
    ``[id]``. When omitted, returns the primary account (``default="primary"``, for
    single-resource reads) or all active accounts (``default="all"``, for job triggers
    and bulk mutations). Active accounts come back primary-first.
    """
    with session_scope() as session:
        active = resolve_active_account_refs(session)
    if broker_account_id is not None:
        if broker_account_id not in active:
            raise HTTPException(
                status_code=404, detail=f"No active broker account {broker_account_id}"
            )
        return [broker_account_id]
    if default == "primary":
        # Primary = lowest active ref (stable across onboards), NOT active[0], which is
        # most-recently-synced order. Mirrors resolve_primary_account_ref.
        return [min(active)] if active else []
    return active


def _active_accounts() -> list[AccountInfo]:
    with session_scope() as session:
        return list_active_accounts(session)


def _require_account(broker_account_id: int) -> AccountInfo:
    """Return the AccountInfo for an active account, or 404."""
    for a in _active_accounts():
        if a.account_ref == broker_account_id:
            return a
    raise HTTPException(status_code=404, detail=f"No active broker account {broker_account_id}")


def admin_auth(
    x_admin_token: str = Header(default=""),
    settings: Settings = Depends(_get_settings),  # noqa: B008
) -> None:
    """Dependency: validates X-Admin-Token header against ADMIN_TOKEN setting."""
    if not settings.admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="invalid admin token")


def promotion_auth(
    x_promotion_token: str = Header(default=""),
    settings: Settings = Depends(_get_settings),  # noqa: B008
) -> None:
    """Dependency: validates X-Promotion-Token header against AUTO_TRADE_PROMOTION_TOKEN setting."""
    if (
        not settings.auto_trade_promotion_token
        or x_promotion_token != settings.auto_trade_promotion_token
    ):
        raise HTTPException(status_code=401, detail="invalid promotion token")


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

    # Per-broker adapters keyed by account_ref (B2). The primary adapter reuses the
    # dict's instance when an active account exists, else falls back to settings
    # (fresh DB, before the first snapshot creates a broker_account row).
    with session_scope() as _s:
        adapters = build_account_adapters(_s, _settings)
        primary_ref = resolve_primary_account_ref(_s)
    adapter = (
        adapters.get(primary_ref) if primary_ref is not None else None
    ) or make_adapter(_settings)
    app.state.adapters = adapters

    emailer = SMTPEmailer(
        host=_settings.smtp_host,
        port=_settings.smtp_port,
        user=_settings.smtp_user,
        password=_settings.smtp_app_password,
        from_addr=_settings.email_from,
    )

    from .services.llm import make_llm_client
    llm = make_llm_client(_settings)

    from .services.tavily import make_tavily_client
    tavily = make_tavily_client(_settings)

    from .services.earnings import make_earnings_client
    earnings = make_earnings_client(_settings)

    from .services.sentiment import make_sentiment_client
    sentiment = make_sentiment_client(_settings)

    app.state.settings = _settings
    app.state.adapter = adapter
    app.state.emailer = emailer
    app.state.llm = llm
    app.state.tavily = tavily
    app.state.earnings = earnings
    app.state.sentiment = sentiment

    # Per-broker cron loops fan out over app.state.adapters (B8). Movers stays global
    # (watchlist price moves), weekly_review stays primary-scoped (4.9a), moomoo_parallel
    # is the soak comparison and is unchanged.
    daily_fn = partial(run_daily_report_all_brokers, _settings, emailer, adapters)
    weekly_fn = partial(
        run_weekly_suggestions_all_brokers, _settings, emailer, llm, earnings, adapters
    )
    movers_fn = partial(run_movers_email, _settings, adapter, emailer, llm)
    expiry_fn = partial(sweep_expired_suggestions_all_brokers, adapters)
    recon_fn = partial(run_daily_reconciliation_all_brokers, _settings, adapters)
    moomoo_parallel_fn = partial(run_moomoo_parallel, _settings, adapter)
    weekly_review_fn = partial(
        run_weekly_review, _settings, adapter, emailer, llm, tavily, sentiment
    )
    auto_trade_fn = partial(run_auto_trade_job_all_brokers, _settings, emailer, adapters)
    scheduler = make_scheduler(
        daily_fn,
        weekly_fn,
        movers_fn,
        expiry_fn,
        recon_fn,
        moomoo_parallel_fn,
        weekly_review_fn,
        auto_trade_fn,
    )
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
    """Return service status and a per-broker-account summary.

    Each active account reports its nickname, broker, auto-trade mode, last sync
    timestamp, and active target count.
    """
    accounts_out: list[dict[str, Any]] = []
    try:
        with session_scope() as session:
            for acct in list_active_accounts(session):
                params = {"broker_account_id": acct.account_ref}
                ls_row = session.execute(account_last_sync, params).fetchone()
                tc_row = session.execute(targets_active_count, params).fetchone()
                accounts_out.append({
                    "broker_account_id": acct.account_ref,
                    "nickname": acct.nickname,
                    "broker": acct.broker,
                    "auto_trade_mode": _get_mode(session, acct.account_ref),
                    "last_sync_ts": str(ls_row[0]) if ls_row and ls_row[0] else None,
                    "target_count": int(tc_row[0]) if tc_row else 0,
                })
    except Exception as exc:
        logger.warning("Health check DB query failed: %s", exc)

    return {"status": "ok", "accounts": accounts_out}


class BrokerAccountCreateRequest(BaseModel):
    """Request body for POST /admin/broker-accounts."""

    broker: str  # "alpaca" | "moomoo" | …
    nickname: str
    connection_config: dict[str, Any] = {}  # creds/host refs; see make_account_adapter


@app.get(
    "/admin/broker-accounts",
    summary="List broker accounts",
    dependencies=[Depends(admin_auth)],
)
def admin_list_broker_accounts() -> list[dict[str, Any]]:
    """List every broker account (active + soft-deleted), latest open row per account."""
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    with session_scope() as session:
        rows = (
            session.query(BrokerAccount)
            .filter(BrokerAccount.effective_to.is_(None))
            .order_by(BrokerAccount.last_sync.desc())
            .all()
        )
        for r in rows:
            if r.account_ref is None or r.account_ref in seen:
                continue
            seen.add(r.account_ref)
            out.append({
                "broker_account_id": r.account_ref,
                "nickname": r.nickname,
                "broker": r.broker,
                "is_active": r.is_active,
                "auto_trade_mode": _get_mode(session, r.account_ref),
            })
    return out


@app.post(
    "/admin/broker-accounts",
    summary="Onboard a new broker account",
    dependencies=[Depends(admin_auth)],
)
def admin_create_broker_account(
    request: Request, body: BrokerAccountCreateRequest
) -> dict[str, Any]:
    """Create a broker-account identity row, seed its auto_trade_state at OFF, and
    register its adapter live (no restart needed).

    The adapter is built first to fail fast on a bad config. A fresh ``account_ref``
    is self-assigned (= the new row's id). New brokers always start at mode OFF — LIVE
    requires that broker's own soak ladder (ADR-0014/0024).
    """
    settings = _get_settings()
    try:
        adapter = make_account_adapter(
            broker=body.broker, connection_config=body.connection_config, settings=settings
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not build adapter: {exc}") from exc

    now = datetime.now(UTC)
    paper = bool(body.connection_config.get("paper", True))
    with session_scope() as session:
        row = BrokerAccount(
            broker=body.broker,
            mode="paper" if paper else "live",
            nickname=body.nickname,
            is_active=True,
            connection_config=json.dumps(body.connection_config),
            account_ref=0,  # placeholder; set to self id after flush
            cash_usd=0.0,
            equity_usd=0.0,
            last_sync=now,
            effective_from=now,
        )
        session.add(row)
        session.flush()
        row.account_ref = row.id  # stable self-reference for this new account
        account_ref = row.account_ref
        session.add(AutoTradeState(broker_account_id=account_ref, mode="OFF"))

    # Register the adapter so the new broker is usable immediately (cron loops +
    # endpoints read app.state.adapters).
    request.app.state.adapters[account_ref] = adapter
    logger.info(
        "broker-accounts: onboarded %s (%s) as account_ref=%s",
        body.nickname, body.broker, account_ref,
    )
    return {
        "status": "ok",
        "broker_account_id": account_ref,
        "nickname": body.nickname,
        "broker": body.broker,
        "auto_trade_mode": "OFF",
    }


@app.delete(
    "/admin/broker-accounts/{broker_account_id}",
    summary="Soft-delete (deactivate) a broker account",
    dependencies=[Depends(admin_auth)],
)
def admin_delete_broker_account(
    request: Request, broker_account_id: int
) -> dict[str, Any]:
    """Soft-delete a broker account: set is_active=False (never hard-delete — history
    stays queryable) and drop its adapter from the live dict so cron loops skip it."""
    with session_scope() as session:
        updated = (
            session.query(BrokerAccount)
            .filter(BrokerAccount.account_ref == broker_account_id)
            .update({"is_active": False})
        )
    if not updated:
        raise HTTPException(status_code=404, detail=f"No broker account {broker_account_id}")
    request.app.state.adapters.pop(broker_account_id, None)
    logger.info("broker-accounts: soft-deleted account_ref=%s", broker_account_id)
    return {"status": "ok", "broker_account_id": broker_account_id, "is_active": False}


@app.get("/positions", summary="Latest positions snapshot")
def positions(broker_account_id: int | None = None) -> list[dict[str, Any]]:
    """Return the most recent positions snapshot per ticker for one broker account
    (defaults to the primary account)."""
    ref = _resolve_scope(broker_account_id, default="primary")
    if not ref:
        return []
    try:
        with session_scope() as session:
            rows = session.execute(
                positions_latest, {"broker_account_id": ref[0]}
            ).fetchall()
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
def gap(broker_account_id: int | None = None) -> list[dict[str, Any]]:
    """Return gap between current allocation and targets for one broker account
    (defaults to the primary), sorted by abs(gap_pct) desc."""
    ref = _resolve_scope(broker_account_id, default="primary")
    if not ref:
        return []
    try:
        with session_scope() as session:
            rows: list[GapRow] = compute_gap(session, ref[0])
    except Exception as exc:
        logger.error("/gap query failed: %s", exc)
        raise HTTPException(status_code=500, detail="Gap computation failed") from exc

    return [_gap_row_to_dict(r) for r in rows]


@app.get("/drift", summary="Out-of-band tickers only")
def drift(broker_account_id: int | None = None) -> list[dict[str, Any]]:
    """Return only tickers whose current allocation is outside their rebalance band
    (one broker account; defaults to the primary)."""
    ref = _resolve_scope(broker_account_id, default="primary")
    if not ref:
        return []
    try:
        with session_scope() as session:
            rows: list[GapRow] = compute_gap(session, ref[0])
    except Exception as exc:
        logger.error("/drift query failed: %s", exc)
        raise HTTPException(status_code=500, detail="Gap computation failed") from exc

    return [_gap_row_to_dict(r) for r in rows if r.band_status != "in_band"]


@app.get("/indicators", summary="Technical indicators per ticker")
def indicators(broker_account_id: int | None = None) -> list[dict[str, Any]]:
    """Return latest SMA-20/50/200, EMA-21, RSI-14, MACD per watchlist ticker.

    Uses the watchlist of one broker account's targets file (defaults to the primary).
    """
    from .config import load_targets
    from .services.indicators import compute_indicators

    settings = _get_settings()
    ref = _resolve_scope(broker_account_id, default="primary")
    if not ref:
        return []
    accounts = _active_accounts()
    primary_ref = accounts[0].account_ref if accounts else None
    path = targets_path_for_account(settings, ref[0], is_primary=(ref[0] == primary_ref))
    if path is None:
        return []
    try:
        targets = load_targets(path)
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
def suggestions(broker_account_id: int | None = None) -> list[dict[str, Any]]:
    """Return pending order suggestions for one broker account (defaults to primary)."""
    from .models import OrderSuggestion

    ref = _resolve_scope(broker_account_id, default="primary")
    if not ref:
        return []
    try:
        with session_scope() as session:
            rows = (
                session.query(OrderSuggestion)
                .filter(
                    OrderSuggestion.broker_account_id == ref[0],
                    OrderSuggestion.status == "pending",
                )
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
def admin_run_sync(
    request: Request, broker_account_id: int | None = None
) -> dict[str, Any]:
    """Trigger an immediate sync. Defaults to ALL active brokers; ``?broker_account_id``
    targets one. Each account syncs through its OWN adapter (``app.state.adapters``) —
    never the primary adapter, which is what wrote Alpaca's positions under Moomoo."""
    settings = _get_settings()
    refs = _resolve_scope(broker_account_id, default="all")
    adapters = request.app.state.adapters
    synced: dict[int, int] = {}
    errors: dict[int, str] = {}
    for ref in refs:
        adapter = adapters.get(ref)
        if adapter is None:
            errors[ref] = "no adapter registered for this account"
            continue
        try:
            synced[ref] = run_sync_for_account(adapter, settings, ref)
        except Exception as exc:
            logger.exception("Ad-hoc sync failed for account_ref=%s", ref)
            errors[ref] = str(exc)
    if errors and not synced:
        raise HTTPException(status_code=502, detail=f"Sync failed: {errors}")
    logger.info("Ad-hoc sync via /admin/run-sync: synced=%s errors=%s", synced, errors)
    return {"status": "ok", "synced": synced, "errors": errors}


@app.post(
    "/admin/run-daily-report",
    summary="Manual daily report trigger",
    dependencies=[Depends(admin_auth)],
)
def admin_run_daily_report(broker_account_id: int | None = None) -> dict[str, str]:
    """Manually trigger the daily report — all active brokers, or one via broker_account_id."""
    settings = _get_settings()
    emailer = app.state.emailer
    adapters = app.state.adapters
    logger.info("Daily report triggered (account=%s)", broker_account_id)
    try:
        if broker_account_id is None:
            run_daily_report_all_brokers(settings, emailer, adapters)
            msg = "Daily report sent for all active brokers"
        else:
            acct = _require_account(broker_account_id)
            adapter = adapters.get(acct.account_ref)
            if adapter is None:
                raise HTTPException(
                    status_code=404, detail=f"No adapter for account {broker_account_id}"
                )
            run_daily_report_for_account(settings, adapter, emailer, acct)
            msg = f"Daily report sent for {acct.nickname}"
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Daily report failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Daily report failed: {exc}") from exc
    return {"status": "ok", "message": msg}


@app.post(
    "/admin/reload-targets",
    summary="Reload targets from targets.yaml",
    dependencies=[Depends(admin_auth)],
)
def admin_reload_targets(request: Request) -> dict[str, Any]:
    """Reload target allocations from targets.yaml and backfill bars for any new tickers.

    Bar backfill runs in a background thread (new tickers get 2 years of history;
    existing tickers get an incremental update). Check logs for completion.
    """
    import threading

    from .services.bars import update_bars

    settings = _get_settings()
    adapters = request.app.state.adapters
    results: dict[str, str] = {}
    all_tickers: set[str] = set()
    try:
        with session_scope() as sess:
            accounts = list_active_accounts(sess)
            primary_ref = resolve_primary_account_ref(sess)
        for acct in accounts:
            path = targets_path_for_account(
                settings, acct.account_ref, is_primary=(acct.account_ref == primary_ref)
            )
            if path is None:
                logger.info(
                    "reload-targets: no targets file for account_ref=%s (%s); skipping",
                    acct.account_ref, acct.nickname,
                )
                continue
            targets_cfg = load_targets(path)
            h = yaml_hash(path)
            with session_scope() as sess:
                results[str(acct.account_ref)] = load_targets_into_db(
                    sess, targets_cfg, h,
                    broker_account_id=acct.account_ref,
                    adapter=adapters.get(acct.account_ref),
                )
            all_tickers |= set(targets_cfg.watchlist)
    except Exception as exc:
        logger.error("reload-targets failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Reload failed: {exc}") from exc

    watchlist = sorted(all_tickers)

    def _backfill() -> None:
        try:
            update_bars(
                watchlist,
                settings.alpaca_api_key,
                settings.alpaca_secret_key,
                bars_dir=settings.bars_dir,
            )
            logger.info("reload-targets: bar backfill complete for %s", watchlist)
        except Exception as exc:
            logger.warning("reload-targets: bar backfill failed: %s", exc)

    threading.Thread(target=_backfill, daemon=True, name="reload-targets-bars").start()
    logger.info("reload-targets: bar backfill started in background")

    return {"status": "ok", "results": results, "bars_sync": "started in background"}


@app.post(
    "/admin/run-weekly-suggestions",
    summary="Manual weekly suggestions trigger",
    dependencies=[Depends(admin_auth)],
)
def admin_run_weekly_suggestions(
    request: Request, broker_account_id: int | None = None
) -> dict[str, str]:
    """Trigger weekly suggestions — all active brokers, or one via broker_account_id."""
    settings = _get_settings()
    emailer = request.app.state.emailer
    adapters = request.app.state.adapters
    llm = request.app.state.llm
    earnings = request.app.state.earnings
    logger.info("Weekly suggestions triggered (account=%s)", broker_account_id)
    try:
        if broker_account_id is None:
            run_weekly_suggestions_all_brokers(settings, emailer, llm, earnings, adapters)
            msg = "Weekly suggestions sent for all active brokers"
        else:
            acct = _require_account(broker_account_id)
            adapter = adapters.get(acct.account_ref)
            if adapter is None:
                raise HTTPException(
                    status_code=404, detail=f"No adapter for account {broker_account_id}"
                )
            run_weekly_suggestions_for_account(settings, adapter, emailer, llm, earnings, acct)
            msg = f"Weekly suggestions sent for {acct.nickname}"
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Weekly suggestions failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Weekly suggestions failed: {exc}") from exc
    return {"status": "ok", "message": msg}


@app.post(
    "/admin/resend-weekly-email",
    summary="Re-send weekly suggestions email from existing DB rows (no LLM)",
    dependencies=[Depends(admin_auth)],
)
def admin_resend_weekly_email(request: Request) -> dict[str, Any]:
    """Re-render and re-send the weekly suggestions email without running any LLM.

    Reads existing suggestions for the current week_of from the DB, recomputes
    indicators and nearby levels (fast, no LLM), then renders and emails.
    Useful for testing layout changes or resending after a template update.
    """
    from types import SimpleNamespace

    settings = _get_settings()
    emailer = request.app.state.emailer

    targets = load_targets(settings.targets_path)
    tickers = targets.watchlist
    week_of = _next_monday()

    indicators = compute_indicators(tickers, settings.bars_dir)
    sr_rows = compute_levels(tickers, indicators, settings.bars_dir)
    nearby = build_nearby_levels(tickers, sr_rows, indicators)

    with session_scope() as session:
        ref = resolve_primary_account_ref(session)
        if ref is None:
            raise HTTPException(status_code=404, detail="No active broker account found")
        orm_suggestions = session.scalars(
            select(OrderSuggestion)
            .where(
                OrderSuggestion.broker_account_id == ref,
                OrderSuggestion.week_of == week_of,
                OrderSuggestion.status.in_(["pending", "accepted"]),
            )
            .order_by(OrderSuggestion.id)
        ).all()

        suggestion_rows = [
            {
                "id": s.id,
                "ticker": s.ticker,
                "side": s.side,
                "qty": s.qty,
                "limit_price": s.limit_price,
                "reason": s.reason,
                "status": s.status,
                "size_factor": s.size_factor,
                "base_qty": s.base_qty,
                "context_note": s.context_note,
                "llm_rationale": s.llm_rationale,
            }
            for s in orm_suggestions
        ]

        orm_account = (
            session.query(BrokerAccount)
            .filter(
                BrokerAccount.account_ref == ref,
                BrokerAccount.effective_to.is_(None),
            )
            .order_by(BrokerAccount.last_sync.desc())
            .first()
        )
        account = (
            AccountSnapshot(
                broker=orm_account.broker,
                mode=orm_account.mode,
                cash_usd=orm_account.cash_usd,
                equity_usd=orm_account.equity_usd,
            )
            if orm_account is not None
            else AccountSnapshot(broker="unknown", mode="unknown", cash_usd=0.0, equity_usd=0.0)
        )

        untracked = get_untracked_positions(session, ref)

    if not suggestion_rows:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No suggestions found for week_of={week_of}"
                " — run /admin/run-weekly-suggestions first"
            ),
        )

    suggestion_items = []
    for row in suggestion_rows:
        sid = row["id"]
        sug_obj = SimpleNamespace(**{k: v for k, v in row.items() if k != "id"})
        suggestion_items.append({
            "suggestion": sug_obj,
            "sid": sid,
            "rationale": row["llm_rationale"] or row["reason"],
            "accept_token": sign_action(sid, "accept", settings.magic_link_secret),
            "reject_token": sign_action(sid, "reject", settings.magic_link_secret),
        })

    subject = f"[Resend] Orders for the week of {week_of:%b %d}"
    html = render_template(
        "weekly_suggestions.html.j2",
        week_of=week_of,
        account=account,
        suggestion_items=suggestion_items,
        base_url=settings.app_base_url,
        indicators=indicators,
        nearby=nearby,
        untracked=untracked,
        skipped=[],
        scoring_failures=[],
    )
    text_suggestions = [
        SimpleNamespace(**{k: v for k, v in row.items() if k != "id"})
        for row in suggestion_rows
    ]
    text = render_template(
        "weekly_suggestions.txt.j2",
        week_of=week_of,
        account=account,
        suggestions=text_suggestions,
        indicators=indicators,
        nearby=nearby,
        untracked=untracked,
        skipped=[],
        scoring_failures=[],
    )
    emailer.send(to=settings.email_to, subject=subject, html=html, text=text)
    logger.info(
        "resend-weekly-email: sent %d suggestions for %s to %s",
        len(suggestion_rows),
        week_of,
        settings.email_to,
    )
    return {
        "status": "ok",
        "week_of": str(week_of),
        "suggestions_sent": len(suggestion_rows),
        "message": f"Resent {len(suggestion_rows)} suggestions for {week_of}",
    }


@app.post(
    "/admin/run-weekly-review",
    summary="Manual weekly review trigger",
    dependencies=[Depends(admin_auth)],
)
def admin_run_weekly_review(request: Request) -> dict[str, str]:
    """Trigger the weekly review job — builds review data and emails it."""
    settings = _get_settings()
    adapter = request.app.state.adapter
    emailer = request.app.state.emailer
    logger.info("Weekly review triggered via POST /admin/run-weekly-review")
    try:
        run_weekly_review(
            settings, adapter, emailer,
            request.app.state.llm, request.app.state.tavily,
            request.app.state.sentiment,
        )
    except Exception as exc:
        logger.error("Weekly review failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Weekly review failed: {exc}") from exc
    return {"status": "ok", "message": "Weekly review email sent"}


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


class ReconcileManualRequest(BaseModel):
    """Request body for POST /admin/reconcile/{execution_id}."""

    suggestion_id: int


@app.post(
    "/admin/reconcile/{execution_id}",
    summary="Manually link an execution to a suggestion",
    dependencies=[Depends(admin_auth)],
)
def admin_reconcile_manual(
    execution_id: int,
    body: ReconcileManualRequest,
) -> dict[str, Any]:
    """Manually set suggestion_id on an order_execution row (match_method='manual_matched')."""
    from .models import OrderExecution, OrderSuggestion

    with session_scope() as session:
        execution = session.get(OrderExecution, execution_id)
        if execution is None:
            raise HTTPException(status_code=404, detail=f"OrderExecution {execution_id} not found")
        suggestion = session.get(OrderSuggestion, body.suggestion_id)
        if suggestion is None:
            raise HTTPException(
                status_code=422,
                detail=f"OrderSuggestion {body.suggestion_id} not found",
            )
        execution.suggestion_id = body.suggestion_id
        execution.match_method = "manual_matched"
    return {
        "id": execution_id,
        "suggestion_id": body.suggestion_id,
        "match_method": "manual_matched",
    }


# --- Auto-trade endpoints ---

@app.post(
    "/admin/run-auto-trade",
    summary="Manual auto-trade trigger",
    dependencies=[Depends(admin_auth)],
)
def admin_run_auto_trade(
    request: Request, broker_account_id: int | None = None
) -> dict[str, Any]:
    """Manually trigger an auto-trade pass — all active brokers, or one via broker_account_id."""
    settings = request.app.state.settings
    emailer = request.app.state.emailer
    adapters = request.app.state.adapters
    try:
        if broker_account_id is None:
            run_auto_trade_job_all_brokers(settings, emailer, adapters)
            msg = "Auto-trade pass completed for all active brokers"
        else:
            acct = _require_account(broker_account_id)
            adapter = adapters.get(acct.account_ref)
            if adapter is None:
                raise HTTPException(
                    status_code=404, detail=f"No adapter for account {broker_account_id}"
                )
            run_auto_trade_job(settings, adapter, emailer)
            msg = f"Auto-trade pass completed for {acct.nickname}"
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Manual auto-trade pass failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Auto-trade pass failed: {exc}") from exc
    return {"status": "ok", "message": msg}


class AutoTradePromoteRequest(BaseModel):
    to_mode: Literal["OFF", "DRY_RUN", "LIVE"]
    broker_scope: Literal["alpaca_paper", "alpaca_live", "moomoo"]
    reason: str | None = None
    broker_account_id: int | None = None  # None → the primary active broker account


SOAK_WINDOWS: dict[tuple[str, str], int] = {
    # (broker_scope, to_mode) → minimum days in previous mode
    ("alpaca_paper", "DRY_RUN"): 0,
    ("alpaca_paper", "LIVE"): 0,    # paper has no real money; soak only required before alpaca_live
    ("alpaca_live", "LIVE"): 28,
    ("moomoo", "LIVE"): 28,
}


@app.post(
    "/admin/auto-trade/promote",
    summary="Promote auto-trade mode (requires promotion token)",
    dependencies=[Depends(promotion_auth)],
)
def admin_auto_trade_promote(body: AutoTradePromoteRequest) -> dict[str, Any]:
    """Promote or demote auto-trade mode. Enforces soak-window requirements."""
    to_mode = body.to_mode  # already validated by Pydantic Literal

    with session_scope() as session:
        account_ref = body.broker_account_id or resolve_primary_account_ref(session)
        if account_ref is None:
            raise HTTPException(status_code=404, detail="No active broker account found")
        current_mode = _get_mode(session, account_ref)

        # Demote to OFF is always allowed immediately
        if to_mode != "OFF":
            soak_days = SOAK_WINDOWS.get((body.broker_scope, to_mode), 0)
            if soak_days > 0:
                # Clock starts when we last entered the current mode (per broker scope)
                last_entry = (
                    session.query(AutoTradePromotionLog)
                    .filter(
                        AutoTradePromotionLog.to_mode == current_mode,
                        AutoTradePromotionLog.broker_scope == body.broker_scope,
                    )
                    .order_by(AutoTradePromotionLog.ts.desc())
                    .first()
                )
                entry_ts = last_entry.ts.replace(tzinfo=UTC) if last_entry else None
                if entry_ts is None or (
                    datetime.now(UTC) - entry_ts
                ) < timedelta(days=soak_days):
                    days_elapsed = (
                        (datetime.now(UTC) - entry_ts).days if entry_ts else 0
                    )
                    days_remaining = soak_days - days_elapsed
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Soak window not met: need {soak_days} days,"
                            f" {days_remaining} remaining"
                        ),
                    )

        # Apply the promotion to this broker's auto_trade_state row
        set_mode(session, account_ref, to_mode)
        state = session.get(AutoTradeState, account_ref)
        if state is not None:
            state.promoted_at = datetime.now(UTC)

        session.add(AutoTradePromotionLog(
            ts=datetime.now(UTC),
            from_mode=current_mode,
            to_mode=to_mode,
            broker_scope=body.broker_scope,
            reason=body.reason,
            actor="admin",
        ))

    return {"mode": to_mode, "broker_scope": body.broker_scope, "broker_account_id": account_ref}


class AutoTradeCapsRequest(BaseModel):
    per_order_max_usd: float
    per_day_max_usd: float
    per_week_max_usd_per_ticker: float
    per_day_max_orders: int


@app.post(
    "/admin/auto-trade/caps",
    summary="Update auto-trade spending caps",
    dependencies=[Depends(promotion_auth)],
)
def admin_auto_trade_caps(body: AutoTradeCapsRequest) -> dict[str, Any]:
    """Update auto-trade caps: closes old row, inserts new."""
    with session_scope() as session:
        # Close existing active caps row
        old = session.scalar(
            select(AutoTradeCaps).where(AutoTradeCaps.effective_to.is_(None))
        )
        if old is not None:
            old.effective_to = datetime.now(UTC)

        session.add(AutoTradeCaps(
            per_order_max_usd=body.per_order_max_usd,
            per_day_max_usd=body.per_day_max_usd,
            per_week_max_usd_per_ticker=body.per_week_max_usd_per_ticker,
            per_day_max_orders=body.per_day_max_orders,
            effective_from=datetime.now(UTC),
            effective_to=None,
        ))

    return {
        "per_order_max_usd": body.per_order_max_usd,
        "per_day_max_usd": body.per_day_max_usd,
        "per_week_max_usd_per_ticker": body.per_week_max_usd_per_ticker,
        "per_day_max_orders": body.per_day_max_orders,
    }


@app.post(
    "/admin/cancel-all-orders",
    summary="Cancel all open broker orders",
    dependencies=[Depends(admin_auth)],
)
def admin_cancel_all_orders(
    request: Request, broker_account_id: int | None = None
) -> dict[str, Any]:
    """Cancel every open (accepted_for_routing, dry_run=False) broker order — all active
    brokers, or one via broker_account_id. Each order is cancelled via its own account's
    adapter. Does NOT change auto-trade mode (use emergency-stop for mode → OFF).
    """
    from .models import OrderExecution

    adapters = request.app.state.adapters
    scope_refs = _resolve_scope(broker_account_id, default="all")
    with session_scope() as session:
        open_execs = session.scalars(
            select(OrderExecution).where(
                OrderExecution.broker_account_id.in_(scope_refs),
                OrderExecution.status.in_(["accepted_for_routing", "broker_cancelled"]),
                OrderExecution.dry_run.is_(False),
                OrderExecution.broker_order_id.is_not(None),
            )
        ).all()

        cancelled: list[str] = []
        failed: list[str] = []
        for exe in open_execs:
            oid = exe.broker_order_id
            assert oid is not None  # guaranteed by IS NOT NULL filter above
            adapter = adapters.get(exe.broker_account_id)
            if adapter is None:
                logger.warning(
                    "cancel-all-orders: no adapter for account_ref=%s", exe.broker_account_id
                )
                failed.append(oid)
                continue
            try:
                adapter.cancel_order(oid)
                exe.status = "broker_cancelled"
                cancelled.append(oid)
            except Exception as exc:
                logger.warning("cancel-all-orders: cancel_order(%s) failed: %s", oid, exc)
                failed.append(oid)

    return {
        "cancelled": cancelled,
        "failed": failed,
        "total_cancelled": len(cancelled),
        "total_failed": len(failed),
    }


@app.post(
    "/admin/reset-week-suggestions",
    summary="Cancel open orders and reset suggestions to pending",
    dependencies=[Depends(admin_auth)],
)
@app.post(
    "/admin/reset-week-buy-suggestions",
    summary="Cancel open orders and reset suggestions to pending",
    dependencies=[Depends(admin_auth)],
)
def admin_reset_week_buy_suggestions(
    request: Request,
    side: Literal["buy", "sell", "all"] = "buy",
    broker_account_id: int | None = None,
) -> dict[str, Any]:
    """Cancel all live broker orders for the current week and reset
    those suggestions to pending so they can be re-evaluated.

    Scoped to all active brokers, or one via broker_account_id. Each order is
    cancelled via its own account's adapter. For each accepted suggestion matching
    the requested side:
    - Finds every execution row with a broker_order_id (accepted_for_routing OR
      broker_cancelled — the latter may still be live at the broker due to GTC
      cancel propagation lag) and attempts cancel_order() for each.
    - Cancel failure is logged but does not block the suggestion reset.
    - Resets OrderSuggestion.status → "pending", clears acted_at.
    - Pass side="buy" (default) to reset only buys, "sell" for sells, "all" for both.
    """
    adapters = request.app.state.adapters
    scope_refs = _resolve_scope(broker_account_id, default="all")
    week_of = _next_monday()

    cancelled: list[str] = []
    cancel_failed: list[str] = []
    reset_sids: list[int] = []

    side_clauses = [OrderSuggestion.side == side] if side != "all" else []

    with session_scope() as session:
        accepted_suggestions = session.scalars(
            select(OrderSuggestion).where(
                OrderSuggestion.broker_account_id.in_(scope_refs),
                OrderSuggestion.week_of == week_of,
                OrderSuggestion.status == "accepted",
                *side_clauses,
            )
        ).all()

        for sug in accepted_suggestions:
            # Skip suggestions whose order already filled — there is nothing to cancel
            # and resetting a filled suggestion to pending would allow re-buying.
            already_filled = session.scalar(
                select(OrderExecution).where(
                    OrderExecution.suggestion_id == sug.id,
                    OrderExecution.dry_run.is_(False),
                    OrderExecution.status == "filled",
                )
            )
            if already_filled is not None:
                logger.info(
                    "reset-week-buy-suggestions: sug-%d already filled — skipping", sug.id
                )
                continue

            # Cancel every execution row that has a broker_order_id, regardless
            # of current status. broker_cancelled rows may still be open at the
            # broker due to GTC cancel propagation lag.
            execs_with_order = session.scalars(
                select(OrderExecution).where(
                    OrderExecution.suggestion_id == sug.id,
                    OrderExecution.dry_run.is_(False),
                    OrderExecution.broker_order_id.is_not(None),
                    OrderExecution.status.in_(["accepted_for_routing", "broker_cancelled"]),
                )
            ).all()

            adapter = adapters.get(sug.broker_account_id)
            for exe in execs_with_order:
                oid = exe.broker_order_id
                assert oid is not None
                if adapter is None:
                    cancel_failed.append(oid)
                    continue
                try:
                    adapter.cancel_order(oid)
                    exe.status = "broker_cancelled"
                    cancelled.append(oid)
                except Exception as exc:
                    logger.warning(
                        "reset-week-buy-suggestions: cancel_order(%s) failed: %s", oid, exc
                    )
                    cancel_failed.append(oid)

            sug.status = "pending"
            sug.acted_at = None
            reset_sids.append(sug.id)

    logger.info(
        "reset-week-buy-suggestions: reset %d suggestions, cancelled %d orders",
        len(reset_sids),
        len(cancelled),
    )
    return {
        "week_of": str(week_of),
        "suggestions_reset": reset_sids,
        "orders_cancelled": cancelled,
        "cancel_failed": cancel_failed,
    }


@app.post(
    "/admin/auto-trade/emergency-stop",
    summary="Immediately trigger kill switch",
    dependencies=[Depends(admin_auth)],
)
def admin_auto_trade_emergency_stop(
    request: Request, broker_account_id: int | None = None
) -> dict[str, Any]:
    """Trigger the kill switch immediately (mode → OFF, cancel open auto-trade orders).

    Default: every active broker account (safest). Pass broker_account_id to stop one.
    Each account's orders are cancelled via its own adapter.
    """
    emailer = request.app.state.emailer
    adapters = request.app.state.adapters
    email_to = request.app.state.settings.email_to
    scope_refs = _resolve_scope(broker_account_id, default="all")
    if not scope_refs:
        raise HTTPException(status_code=404, detail="No active broker account found")
    stopped: list[int] = []
    for ref in scope_refs:
        adapter = adapters.get(ref)
        if adapter is None:
            continue
        with session_scope() as session:
            _trigger_kill_switch(
                session=session,
                emailer=emailer,
                adapter=adapter,
                trigger="manual",
                detail="Emergency stop via POST /admin/auto-trade/emergency-stop",
                settings_email_to=email_to,
                broker_account_id=ref,
            )
        stopped.append(ref)

    return {
        "status": "ok",
        "message": "Kill switch activated; auto-trade mode set to OFF.",
        "broker_account_ids": stopped,
    }
