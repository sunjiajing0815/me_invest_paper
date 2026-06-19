# ADR-0027 — Tavily as Third-Fallback in the Movers News Pipeline

**Status:** Accepted (supersedes `phase_4_5_guide.md` §8 deferral)
**Date:** 2026-06-04
**Commit:** `c05d581`
**Extends:** ADR-0020 (Tavily as weekly-context provider)

## Context

`phase_4_5_guide.md` §8 explicitly deferred *"Tavily-as-third-fallback in the Phase 3b
movers pipeline"*, citing three reasons:

1. Tavily's general-web crawl cadence is hours-to-day; Alpaca News (Benzinga-backed) indexes
   financial headlines within minutes — disqualifying Tavily for same-day mover explanations.
2. Alpaca/Finnhub are purpose-built for ticker-tagged financial news with much higher
   signal-to-noise than general web search (which returns blogs, opinion, listicles).
3. The two use cases differ: daily movers ask "what specific same-day company news moved this
   ticker?"; weekly context asks "what macro/sector themes should I be aware of?".

The deferred condition — *"revisit if 'no news explanation' annotations turn out to be
common"* — turned out true during the 4.9a soak for thinly-covered tickers (notably
crypto-related ETFs and small-cap names), where Alpaca + Finnhub frequently returned nothing
for a genuine ≥5% mover.

## Decision

When Alpaca News + Finnhub combined return **fewer than 3 articles** for a mover, the movers
job invokes Tavily as a third fallback via `fetch_tavily_news` in `services/news.py`.

Hard boundaries that keep this consistent with ADR-0020:

- Tavily results are **display-only** in the movers email and are **never persisted to
  `news_event`** — no path to the suggestion engine, the LLM classifier inputs, or any
  persistent state.
- Tavily-sourced articles are visually labelled (`source="tavily"`) so the reader sees the
  lower-confidence provenance.
- Mondays use the existing 48h lookback.
- The Tavily monthly query cap applies — operationally ~25 extra calls/month, well within the
  1000-call free-tier ceiling.

## Consequences

- The "no news explanation" annotation rate drops materially on thinly-covered tickers.
- ADR-0020's architectural separation holds — Tavily content remains informational, never
  advisory.
- The hours-to-day crawl lag remains real for major same-day events; the engine surfaces what
  Tavily has at query time and trusts the reader to weigh the labelled provenance.
- Phase 4.5 §8's "deliberately NOT used in movers" guidance is superseded.

## References

- Supersedes `phase_4_5_guide.md` §8; extends [ADR-0020](0020-tavily-weekly-context.md).
- `services/news.py::fetch_tavily_news`, `_CRYPTO_NEWS_SYMBOLS`, `_alpaca_news_symbol`.
- Related correctness fix in the same commit: `BTC` → `BTC/USD` Alpaca symbol-format handling
  (`_alpaca_news_symbol`).
