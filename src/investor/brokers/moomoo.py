"""Moomoo broker adapter via futu-api + OpenD running on host.

OpenD must be bound to 0.0.0.0:11111 on the host so Docker can reach it
via host.docker.internal:11111.

Ticker symbols use the 'US.' prefix in Moomoo's API; we strip it at the
adapter boundary — no 'US.AAPL' should ever appear outside this file.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal

from .base import Account, Activity, OrderConfirmation, OrderRequest, Position

logger = logging.getLogger(__name__)


def _strip_market_prefix(symbol: str) -> str:
    """'US.AAPL' → 'AAPL', 'HK.0700' → '0700'. Strip at adapter boundary."""
    if "." in symbol:
        return symbol.split(".", 1)[1]
    return symbol


_MARKET_CURRENCY = {
    "US": "USD", "AU": "AUD", "HK": "HKD", "SG": "SGD", "JP": "JPY", "CN": "CNH",
}


def _currency_for_code(code: str, default: str = "USD") -> str:
    """Map a market-prefixed Moomoo code ('US.AAPL', 'AU.CSL') to its native currency."""
    market = code.split(".", 1)[0].upper() if "." in code else ""
    return _MARKET_CURRENCY.get(market, default)


class MoomooAdapter:
    """Broker adapter for Moomoo/Futu via futu-api + local OpenD daemon.

    Requires OpenD running on the host (or accessible at opend_host:opend_port).
    Docker containers should use host.docker.internal:11111 as the default.
    """

    def __init__(
        self,
        host: str = "host.docker.internal",
        port: int = 11111,
        *,
        paper: bool = True,
        security_firm: str = "FUTUSECURITIES",
        rsa_key_path: str | None = None,
        currency: str = "USD",
    ) -> None:
        # Import futu lazily so the module remains importable without OpenD running
        import futu as ft

        self._ft = ft
        # When OpenD requires an encrypted connection, the SDK must use the SAME RSA
        # private key file OpenD was configured with. SysConfig is process-global and
        # MUST be set before any context is created. No key path → unencrypted (OpenD
        # must then also have encryption off, or InitConnect fails: "check sha error").
        if rsa_key_path:
            ft.SysConfig.enable_proto_encrypt(True)
            ft.SysConfig.set_init_rsa_file(rsa_key_path)
            logger.info("MoomooAdapter: protocol encryption enabled (rsa key=%s)", rsa_key_path)
        self._trd_env = ft.TrdEnv.SIMULATE if paper else ft.TrdEnv.REAL
        self._security_firm = security_firm
        # Base currency for account totals. accinfo_query defaults to HKD, so without
        # this an AUD/USD account's total_assets/cash come back converted to HKD.
        self._currency = currency
        self._host = host
        self._port = port
        self._trade_ctx = ft.OpenSecTradeContext(
            host=host,
            port=port,
            security_firm=getattr(
                ft.SecurityFirm,
                security_firm,
                ft.SecurityFirm.FUTUSECURITIES,
            ),
        )
        self._quote_ctx = ft.OpenQuoteContext(host=host, port=port)
        logger.info(
            "MoomooAdapter initialised: host=%s port=%d paper=%s currency=%s",
            host,
            port,
            paper,
            currency,
        )

    def close(self) -> None:
        """Close both OpenD contexts."""
        self._trade_ctx.close()
        self._quote_ctx.close()

    def get_account(self) -> Account:
        """Return account summary from Moomoo accinfo_query."""
        ft = self._ft
        # Pin the currency: accinfo_query defaults to Currency.HKD, which silently
        # converts an AUD/USD account's totals into HKD. Request the account's base
        # currency so total_assets/cash match what the broker shows.
        ret, data = self._trade_ctx.accinfo_query(
            trd_env=self._trd_env,
            currency=getattr(ft.Currency, self._currency, ft.Currency.USD),
        )
        if ret != ft.RET_OK:
            raise RuntimeError(f"Moomoo accinfo_query failed: {data}")
        row = data.iloc[0]
        return Account(
            account_id=str(row.get("acc_id", "moomoo")),
            cash_usd=float(row["cash"]),
            equity_usd=float(row["total_assets"]),
            buying_power_usd=float(row["power"]),
            as_of=datetime.now(UTC),
            currency=self._currency,
        )

    def get_positions(self) -> list[Position]:
        """Return current positions from Moomoo position_list_query."""
        ft = self._ft
        ret, data = self._trade_ctx.position_list_query(trd_env=self._trd_env)
        if ret != ft.RET_OK:
            raise RuntimeError(f"Moomoo position_list_query failed: {data}")
        positions = []
        for _, row in data.iterrows():
            code = str(row["code"])
            ticker = _strip_market_prefix(code)
            positions.append(
                Position(
                    ticker=ticker,
                    qty=float(row["qty"]),
                    avg_cost=float(row["cost_price"]),
                    market_value=float(row["market_val"]),
                    as_of=datetime.now(UTC),
                    currency=_currency_for_code(code, self._currency),
                )
            )
        return positions

    def get_bars(self, ticker: str, start: datetime, end: datetime) -> list[object]:
        """Not implemented — per ADR-0001 bars always come from Alpaca."""
        raise NotImplementedError(
            "MoomooAdapter does not provide bar data — use AlpacaAdapter"
        )

    def get_activities(
        self, since: datetime, until: datetime | None = None
    ) -> list[Activity]:
        """Return fills from deal_list_query (actual fills, not order statuses)."""
        ft = self._ft
        # Moomoo deal_list_query does not support date filter — filter in Python
        ret, data = self._trade_ctx.deal_list_query(trd_env=self._trd_env)
        if ret != ft.RET_OK:
            raise RuntimeError(f"Moomoo deal_list_query failed: {data}")
        activities = []
        for _, row in data.iterrows():
            # Parse fill timestamp — Moomoo returns strings like "2024-01-15 10:30:00"
            try:
                filled_at: datetime | None = datetime.fromisoformat(
                    str(row["create_time"])
                ).replace(tzinfo=UTC)
            except (ValueError, TypeError):
                filled_at = None
            if filled_at is not None and filled_at < since:
                continue
            if until is not None and filled_at is not None and filled_at > until:
                continue
            # Moomoo side: TrdSide.BUY / TrdSide.SELL
            side_val = str(row["trd_side"]).upper()
            if "BUY" in side_val:
                side: Literal["buy", "sell"] = "buy"
            elif "SELL" in side_val:
                side = "sell"
            else:
                continue  # skip unknown side
            ticker = _strip_market_prefix(str(row["code"]))
            broker_order_id = str(row["order_id"])
            # client_order_id stored in remark field (per ADR-0018)
            remark = str(row.get("remark", "")) if "remark" in row else ""
            client_order_id: str | None = remark if remark else None
            activities.append(
                Activity(
                    broker_order_id=broker_order_id,
                    client_order_id=client_order_id,
                    ticker=ticker,
                    side=side,
                    filled_qty=float(row["qty"]),
                    filled_price=float(row["price"]),
                    filled_at=filled_at,
                    status="filled",
                )
            )
        return activities

    def submit_order(self, req: OrderRequest) -> OrderConfirmation:
        """Place a limit order via Moomoo place_order.

        Stores req.client_order_id in the remark field so it round-trips
        through get_order() and get_activities() (per ADR-0018).
        """
        ft = self._ft
        trd_side = ft.TrdSide.BUY if req.side == "buy" else ft.TrdSide.SELL
        # US. prefix required for Moomoo
        code = f"US.{req.ticker}"
        ret, data = self._trade_ctx.place_order(
            price=req.limit_price or 0,
            qty=req.qty,
            code=code,
            trd_side=trd_side,
            order_type=ft.OrderType.NORMAL,
            trd_env=self._trd_env,
            remark=req.client_order_id,  # client_order_id stored in remark per ADR-0018
        )
        if ret != ft.RET_OK:
            raise RuntimeError(f"Moomoo place_order failed: {data}")
        row = data.iloc[0]
        return OrderConfirmation(
            broker_order_id=str(row["order_id"]),
            client_order_id=req.client_order_id,
            status=str(row.get("order_status", "submitted")),
            submitted_at=datetime.now(UTC),
        )

    def get_order(self, broker_order_id: str) -> OrderConfirmation:
        """Fetch a single order by broker_order_id and return its confirmation."""
        ft = self._ft
        ret, data = self._trade_ctx.order_list_query(
            order_id=broker_order_id, trd_env=self._trd_env
        )
        if ret != ft.RET_OK:
            raise RuntimeError(f"Moomoo order_list_query failed: {data}")
        if data.empty:
            raise RuntimeError(f"Moomoo order {broker_order_id} not found")
        row = data.iloc[0]
        remark = str(row.get("remark", "")) if "remark" in row else ""
        return OrderConfirmation(
            broker_order_id=broker_order_id,
            client_order_id=remark if remark else None,
            status=str(row.get("order_status", "unknown")),
            submitted_at=datetime.now(UTC),
        )

    def cancel_order(self, broker_order_id: str) -> None:
        """Cancel an open order via Moomoo modify_order with CANCEL op."""
        ft = self._ft
        ret, data = self._trade_ctx.modify_order(
            modify_order_op=ft.ModifyOrderOp.CANCEL,
            order_id=broker_order_id,
            qty=0,
            price=0,
            trd_env=self._trd_env,
        )
        if ret != ft.RET_OK:
            raise RuntimeError(f"Moomoo cancel_order failed: {data}")

    def list_orders(self, status: str) -> list[OrderConfirmation]:
        """Return all orders with the given status ('open' or 'closed').

        Maps 'closed' → FILLED_ALL, CANCELLED_ALL, FAILED, EXPIRED via order_list_query.
        Maps 'open'   → WAITING_SUBMIT, SUBMITTING, SUBMITTED.
        """
        ft = self._ft
        import futu as ft_module  # noqa: PLC0415

        if status == "open":
            status_filter = [
                ft_module.OrderStatus.WAITING_SUBMIT,
                ft_module.OrderStatus.SUBMITTING,
                ft_module.OrderStatus.SUBMITTED,
            ]
        else:  # "closed"
            status_filter = [
                ft_module.OrderStatus.FILLED_ALL,
                ft_module.OrderStatus.CANCELLED_ALL,
                ft_module.OrderStatus.FAILED,
                ft_module.OrderStatus.EXPIRED,
            ]

        ret, data = self._trade_ctx.order_list_query(
            status_filter_list=status_filter,
            trd_env=self._trd_env,
        )
        if ret != ft.RET_OK:
            raise RuntimeError(f"Moomoo order_list_query failed: {data}")

        results = []
        for _, row in data.iterrows():
            broker_order_id = str(row.get("order_id", ""))
            remark = str(row.get("remark", "")) if "remark" in row else ""
            results.append(
                OrderConfirmation(
                    broker_order_id=broker_order_id,
                    client_order_id=remark if remark else None,
                    status=str(row.get("order_status", "unknown")),
                    submitted_at=datetime.now(UTC),
                )
            )
        return results
