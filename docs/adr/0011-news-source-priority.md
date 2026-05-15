# ADR-0011 — News Source Priority

**Date:** 2026-05-12
**Status:** Accepted
**Deciders:** Jane

---

## Context

Phase 3b introduces a "movers" email that summarises material news for tickers that moved significantly. Two news sources are available:

- **Alpaca News** — already integrated via `alpaca-py`; covers US equities well and requires no additional API key.
- **Finnhub** — free tier, 60 req/min; provides broader coverage and acts as a fallback.

Both sources serve many of the same underlying Benzinga articles, distinguished only by query parameters (e.g., `?utm_source=alpaca` vs `?utm_source=finnhub`). Without deduplication the same article would be stored twice, inflating the LLM triage cost and polluting the retrospective corpus.

A deduplication strategy is needed before any article reaches the `llm_material` classification step.

## Decision

**Alpaca News is the primary source. Finnhub is the fallback.**

For each fetch cycle the pipeline:

1. Fetches from Alpaca News first.
2. Fetches from Finnhub only for tickers or time windows not covered by the Alpaca response.
3. Normalises every article URL with `_normalise_url()` — strips query parameters and lowercases the host — then computes a SHA-256 hash truncated to 16 hex characters.
4. Uses the resulting `url_hash` as the deduplication key before inserting into the `news_article` table. Duplicate hashes are silently skipped (`INSERT OR IGNORE`).

### Retention policy

- Articles classified `llm_material=false` are pruned after 6 months.
- Articles classified `llm_material=true` are retained indefinitely for retrospective review.

## Consequences

- Alpaca provides good coverage for US equities and is already integrated; no new credentials are required for the primary path.
- Finnhub fills gaps when Alpaca returns no results for a ticker or time window; the free tier (60 req/min) is sufficient at current single-user scale.
- URL normalisation (`_normalise_url()`) is the single point of defence against double-insertion of the same Benzinga article served by both sources with differing `?utm_source=` params. If this function is modified, re-verify against known duplicates.
- The `url_hash` column must be indexed and carry a `UNIQUE` constraint in the schema migration. An `INSERT OR IGNORE` without the unique constraint would silently allow duplicates.
- Pruning `llm_material=false` rows after 6 months keeps the SQLite file size bounded; `llm_material=true` rows accumulate indefinitely and will need archival in a later phase if the corpus grows large.
- Adding a third news source in a future phase requires only: (a) a new adapter, (b) the same `_normalise_url()` + `url_hash` dedup path. No changes to the triage graph are needed.
