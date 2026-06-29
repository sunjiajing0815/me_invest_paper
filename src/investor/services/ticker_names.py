"""Curated ticker → trading-name map for email annotation (soak-window P1.1).

Trading-name strings only — no prospectus text, CIK, or SEC linkage (soak-scope rule). The
motivating case (ADR-0029): the ``BTC`` ticker is the *Grayscale Bitcoin Mini Trust ETF*, not
crypto-spot — a cognitive mismatch a one-line name prevents. Covers both current watchlists;
unknown tickers return ``None`` and are simply omitted from the email glossary.
"""

from __future__ import annotations

TICKER_NAMES: dict[str, str] = {
    # ETFs / funds (note the crypto-proxy ETFs — the BTC/ETH confusion ADR-0029 surfaced)
    "VOO": "Vanguard S&P 500 ETF",
    "QQQ": "Invesco QQQ Trust (Nasdaq-100 ETF)",
    "TQQQ": "ProShares UltraPro QQQ (3× Nasdaq-100)",
    "BTC": "Grayscale Bitcoin Mini Trust ETF",
    "ETH": "Grayscale Ethereum Mini Trust ETF",
    # Equities
    "ISRG": "Intuitive Surgical",
    "BRK.B": "Berkshire Hathaway (Class B)",
    "AMZN": "Amazon.com",
    "GOOG": "Alphabet (Class C)",
    "MSFT": "Microsoft",
    "MU": "Micron Technology",
    "NFLX": "Netflix",
    "CEG": "Constellation Energy",
    "PANW": "Palo Alto Networks",
    "NVDA": "NVIDIA",
    "NEE": "NextEra Energy",
    "TSLA": "Tesla",
}


def name_for(ticker: str) -> str | None:
    """Trading name for a ticker, or ``None`` if not in the curated map."""
    return TICKER_NAMES.get(ticker)


def names_for(tickers: list[str]) -> dict[str, str]:
    """{ticker: name} for the subset of ``tickers`` that have a curated name (others dropped)."""
    return {t: TICKER_NAMES[t] for t in tickers if t in TICKER_NAMES}
