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


class LiveTradingBlocked(RuntimeError):  # noqa: N818 — fixed public name, later tasks import it verbatim
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
