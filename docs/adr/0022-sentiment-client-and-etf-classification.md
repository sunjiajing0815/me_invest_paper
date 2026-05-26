# ADR-0022 — SentimentClient Protocol and ETF Classification in targets.yaml

**Status:** Accepted  
**Date:** 2026-05-26

## Context

Phase 4.7 post-deploy review surfaced three related issues in `weekly_context.py` and `context_adjust_node`:

1. VIX and Fear & Greed index were fetched by bare private functions inside `weekly_context.py`. There was no dependency-injection seam, so those HTTP calls could not be mocked in unit tests.

2. The CNN Fear & Greed endpoint used in production is a public URL with no API key. Its URL is fragile (CNN controls it) and there was no explicit fallback strategy documented.

3. The `context_size_v2.txt` prompt embedded a hardcoded list of ETF tickers (VOO, SPY, IVV, ...) to distinguish index ETFs from equities. Any change to the watchlist would silently break the classification or require a prompt edit, neither of which is easily audited.

## Decisions

### 1. SentimentClient Protocol — injectable VIX + Fear & Greed dependency

VIX and Fear & Greed are extracted into a `SentimentClient` Protocol in `services/sentiment.py`, following the same pattern as `EarningsClient` (`services/earnings.py`) and `TavilyClient` (`services/tavily.py`).

```
Protocol: SentimentClient
  .get_vix() -> float | None
  .get_fear_greed() -> tuple[int, str] | None  # (score, label)

Concrete: FinnhubCNNSentimentClient
  - VIX: Finnhub market-news endpoint (FINNHUB_API_KEY required)
  - Fear & Greed: CNN public endpoint (no API key)

Fake: FakeSentimentClient(vix, score, label)
  - Returns canned values; used in all tests

Factory: make_sentiment_client(settings) -> SentimentClient
  - Returns FinnhubCNNSentimentClient if FINNHUB_API_KEY is set
  - Returns FakeSentimentClient(_canned_empty) with a WARNING log otherwise
```

Call sites in `weekly_context.py` replace bare function calls with `sentiment_client.get_vix()` and `sentiment_client.get_fear_greed()`. The client is injected via `build_weekly_market_context(sentiment_client=..., ...)`.

### 2. CNN Fear & Greed endpoint — no key, explicit fragility contract

The CNN endpoint (`https://production.dataviz.cnn.io/index/fearandgreed/graphdata`) requires no API key. This is an undocumented public endpoint that CNN can change without notice.

Explicit contract:
- Any `urllib.request` failure (network, 4xx, 5xx, JSON parse error) is caught, logged as WARNING, and returns `None`.
- `None` from `get_fear_greed()` is treated as "sentiment unknown" by `build_weekly_market_context()`: the `WeeklyMarketContext` fields `fear_greed_score` and `fear_greed_label` are set to `None` and the Sonnet synthesis prompt notes "sentiment data unavailable".
- `FakeSentimentClient` is the offline fallback; it is always available regardless of network or CNN changes.
- Do not raise on fetch failure. Silent degradation is correct — the weekly email is not blocked by a CNN endpoint change.

### 3. ETF classification in targets.yaml — YAML is authoritative, not prompt heuristics

`asset_class` is added as an optional field per target in `config/targets.yaml`:

```yaml
targets:
  - ticker: VOO
    target_pct: 20.0
    asset_class: index_etf   # "index_etf" | "leveraged_etf" | "equity" (default: "equity")
  - ticker: TQQQ
    target_pct: 5.0
    asset_class: leveraged_etf
  - ticker: AAPL
    target_pct: 10.0
    # asset_class omitted → defaults to "equity"
```

`load_targets()` validates `asset_class` against `{"index_etf", "leveraged_etf", "equity"}`. Unrecognised values are logged as a WARNING and coerced to `"equity"` (safe degradation — the app keeps running with equity-level sizing for the offending ticker). Absent → `"equity"`.

`gather_context_node` reads `target_asset_classes: dict[str, str]` from the loaded targets and stores it on `ReviewContext`. `context_adjust_node` passes it to Sonnet as `asset_classes[ticker]` in the user payload.

The `context_size_v2.txt` prompt is updated to reference `asset_classes[ticker]` symbolically rather than hardcoded ticker names. This means prompt behaviour is correct even as the watchlist evolves, without prompt edits.

## Consequences

- `services/sentiment.py` is a new file; `make_sentiment_client()` is called once at app startup alongside `make_earnings_client()` and `make_tavily_client()`.
- `weekly_context.py` gains a `sentiment_client` parameter; no call-site changes outside `main.py` and tests.
- `tests/test_weekly_context.py` can now inject `FakeSentimentClient` for deterministic VIX/F&G values without HTTP mocking.
- `config/targets.yaml` gains an `asset_class` column; existing rows without it default to `"equity"` with no migration required.
- `ReviewContext.target_asset_classes` and `OrderSuggestionRow` (no change needed there) carry the classification forward; no schema migration required.
- If a ticker is added to the watchlist without an `asset_class` entry in YAML, it silently defaults to `"equity"` — this is the safe default (equity-level sizing caps, not ETF-level).
