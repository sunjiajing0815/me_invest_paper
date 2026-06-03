"""Application configuration: pydantic-settings + YAML target loader."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .services.llm import HAIKU, SONNET

logger = logging.getLogger(__name__)

VALID_BROKERS = {"alpaca_paper", "alpaca_live", "moomoo"}
_VALID_ASSET_CLASSES = {"index_etf", "leveraged_etf", "equity"}


@dataclass(frozen=True)
class TickerTarget:
    ticker: str
    pct: float
    band_low: float
    band_high: float
    asset_class: str = "equity"  # "index_etf" | "leveraged_etf" | "equity"


@dataclass(frozen=True)
class TargetsConfig:
    watchlist: list[str]
    targets: list[TickerTarget]
    cash_buffer_pct: float


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    broker: str = "alpaca_paper"
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""

    sqlite_path: str = "./data/investor.db"
    targets_path: str = "./config/targets.yaml"

    log_level: str = "INFO"

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_app_password: str = ""
    email_from: str = ""
    email_to: str = ""

    admin_token: str = ""
    bars_dir: str = "data/bars"

    anthropic_api_key: str = ""
    finnhub_api_key: str = ""
    magic_link_secret: str = ""
    app_base_url: str = "http://localhost:8000"
    level_prompt_version: str = "2"
    llm_daily_cost_cap_usd: float = 3.0
    llm_backend: str = "anthropic_api"
    llm_cli_path: str = ""  # override claude CLI for agent_sdk; empty = use bundled

    # Per-node models for the movers news-triage graph. The critic defaults to a DIFFERENT
    # class than the classifier (Sonnet judging Haiku) so the graph doesn't depend on
    # same-class self-judgment; all three are env-overridable.
    news_classify_model: str = HAIKU
    news_critic_model: str = SONNET
    news_arbitrate_model: str = SONNET

    auto_trade_promotion_token: str = ""  # separate from admin_token; required for promotions

    # Moomoo/Futu OpenD daemon settings (used when broker == "moomoo")
    opend_host: str = ""
    opend_port: int = 11111
    opend_security_firm: str = "FUTUSECURITIES"
    opend_rsa_key_path: str = ""  # in-container path to OpenD's RSA key (empty = unencrypted)
    opend_currency: str = "USD"  # base currency for Moomoo account totals (accinfo_query)

    # Tavily search API (Phase 4.5 weekly market context)
    tavily_api_key: str = ""
    tavily_monthly_cap: int = 200
    weekly_context_prompt_version: str = "1"

    # Context-aware order sizing (Phase 4.7)
    earnings_size_factor: float = 0.5
    earnings_reanchor: bool = True
    earnings_lookahead_days: int = 7
    context_size_min: float = 0.25
    context_size_max: float = 1.5
    context_max_age_days: int = 4
    context_adjust_prompt_version: str = "1"
    critic_prompt_version: str = "2"

    # Phase 4.8: Weekly order activity metrics
    weekly_review_trend_weeks: int = 4
    weekly_review_breakdown_top_n: int = 20

    @field_validator(
        "level_prompt_version", "weekly_context_prompt_version",
        "context_adjust_prompt_version", "critic_prompt_version",
    )
    @classmethod
    def strip_v_prefix(cls, v: str) -> str:
        return v.removeprefix("v")

    @field_validator("broker")
    @classmethod
    def validate_broker(cls, v: str) -> str:
        if v not in VALID_BROKERS:
            raise ValueError(f"broker must be one of {VALID_BROKERS}, got {v!r}")
        return v

    @model_validator(mode="after")
    def validate_alpaca_keys(self) -> Settings:
        if self.broker.startswith("alpaca"):
            if not self.alpaca_api_key:
                raise ValueError("ALPACA_API_KEY is required when BROKER starts with 'alpaca'")
            if not self.alpaca_secret_key:
                raise ValueError("ALPACA_SECRET_KEY is required when BROKER starts with 'alpaca'")
        return self


def load_targets(targets_path: str) -> TargetsConfig:
    """Load and validate targets.yaml. Raises ValueError if pct sum is wrong."""
    path = Path(targets_path)
    if not path.exists():
        raise FileNotFoundError(f"targets.yaml not found at {path.resolve()}")

    raw: dict[str, Any] = yaml.safe_load(path.read_text())

    watchlist: list[str] = raw.get("watchlist", [])
    cash_buffer_pct: float = float(raw.get("cash_buffer_pct", 0.0))
    raw_targets: dict[str, Any] = raw.get("targets", {})

    targets: list[TickerTarget] = []
    for ticker, spec in raw_targets.items():
        pct = float(spec["pct"])
        band_low = float(spec["band"][0])
        band_high = float(spec["band"][1])
        asset_class = str(spec.get("asset_class", "equity"))
        if asset_class not in _VALID_ASSET_CLASSES:
            logger.warning(
                "targets.yaml: unknown asset_class %r for ticker %s, defaulting to 'equity'",
                asset_class,
                ticker,
            )
            asset_class = "equity"
        targets.append(
            TickerTarget(
                ticker=ticker,
                pct=pct,
                band_low=band_low,
                band_high=band_high,
                asset_class=asset_class,
            )
        )

    total_pct = sum(t.pct for t in targets)
    expected = 100.0 - cash_buffer_pct
    if abs(total_pct - expected) > 0.5:
        raise ValueError(
            f"Target pct sum {total_pct:.2f} must equal 100 - cash_buffer_pct "
            f"({expected:.2f}) ± 0.5"
        )

    logger.info(
        "Loaded %d targets from %s (sum=%.2f%%, cash_buffer=%.2f%%)",
        len(targets), path, total_pct, cash_buffer_pct,
    )
    return TargetsConfig(watchlist=watchlist, targets=targets, cash_buffer_pct=cash_buffer_pct)
