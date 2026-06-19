# ADR-0030 — CNN Sentiment Endpoint with Browser-Shaped Headers; Phase 5 Pre-Launch Removal

**Status:** Accepted (extends ADR-0022); **flagged as Phase 5 mandatory pre-launch removal #3**
**Date:** 2026-06-07
**Commit:** `acd9a5c`
**Extends:** ADR-0022 (`SentimentClient` Protocol)

## Context

ADR-0022 introduced the `SentimentClient` Protocol with `FinnhubCNNSentimentClient` and a
"never raise on failure" contract. Between Phase 4.7 ship and 2026-06-07 the implementation
**never populated non-NULL values** — every `weekly_market_context` row landed with NULL `vix`
and NULL `fear_greed_score`. Two pre-existing structural causes:

1. **Finnhub free tier does not serve `^VIX`** — `quote("^VIX")` returns `c=0`. The premium
   tier serves it; the free tier silently does not.
2. **CNN's `production.dataviz.cnn.io/index/fearandgreed/graphdata` returns HTTP 418
   ("I'm a teapot") to bot User-Agents** — an undocumented endpoint with active anti-bot
   detection keyed on the User-Agent string.

Side-effect: the CNN `graphdata` JSON also carries the latest VIX under `market_volatility_vix`,
so one CNN call services both metrics.

## Decision

`services/sentiment.py::_fetch_cnn` sends browser-shaped headers (a recent-Chrome `User-Agent`,
`Accept`, and `Origin`/`Referer` → cnn.com), which bypasses the 418 in normal operation, and
returns `(score, label, vix)`. Finnhub is kept only as a VIX fallback (invoked if CNN fails and
a Finnhub premium key is configured). ADR-0022's "never raise on failure" contract is
preserved: any fetch failure → WARNING log + `None` return → the Market Sentiment widget
silently hides.

### Operational fragility contract

The CNN endpoint is undocumented, unsupported, and subject to anti-bot countermeasures at any
time. Expected response matrix: (a) tweak headers when CNN updates anti-bot; (b) accept silent
NULLs until a paid feed is wired; (c) escalate to a paid feed. Watch for `_fetch_cnn` WARNINGs
in the logs. The `User-Agent` string is now load-bearing config — refresh it periodically
(annually).

### Phase 5 Mandatory Pre-Launch Removal #3

The CNN scrape is **single-user-only tolerated grey area**, structurally identical to
ADR-0016's `LLM_CLI_PATH` consumer-OAuth decision. CNN's ToS almost certainly prohibits
automated scraping; what's grey-area-tolerated for solo Jane becomes unambiguous abuse at
multi-tenant scale. Before any second user signs up in Phase 5, replace with one of:

1. A paid feed (Polygon, Tradier, Finnhub Premium for VIX; Fear & Greed is harder to license
   cleanly — Alternative.me's crypto-focused F&G is non-equivalent, or derive in-house from
   put/call + breadth + momentum).
2. Graceful absence of the Market Sentiment widget for Phase 5 users (preserves ADR-0022's
   degradation contract; lowest-effort path).
3. User-supplied API key (BYO; higher onboarding friction).

## Consequences

- VIX and Fear & Greed populate; the Market Sentiment widget renders.
- Fragility is flagged operationally; failures degrade gracefully to a hidden widget.
- The Phase 5 pre-launch budget gains an item with either real licensing cost or a UX
  downgrade.

## References

- Extends [ADR-0022](0022-sentiment-client-and-etf-classification.md) with implementation
  specifics. `services/sentiment.py::_fetch_cnn`.
- Phase 5 pre-launch removal alongside [ADR-0016](0016-llm-backend-abstraction.md) and the
  existing auto-trade-LIVE multi-tenant decision.
