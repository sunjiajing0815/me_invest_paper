# ADR-0031 — Shared Email Components and the Jinja Autoescape Trap

**Status:** Accepted
**Date:** 2026-06-07
**Commit:** `1aa36cd`

## Context

Through Phases 4.5, 4.7, and 4.8 the four email templates (daily report, weekly suggestions,
weekly review, movers) grew organically, each with its own markup, colour choices, and
typography. By 06-07 they had visibly drifted: the levels-table gating differed, untracked-
positions banners used different shades of red, and accessibility was inconsistent.

A specific bug exposed the structural problem: HTML entities (`&mdash;`, `&hellip;`) placed
inside Jinja `{{ }}` expressions silently autoescape to literal `&amp;mdash;`, rendering as the
visible text `&mdash;` in the email body. The per-occurrence fix is trivial, but any future
template author can re-introduce it.

## Decision

Two shared component files become the single source of truth:

- `templates/_components.html.j2` — one token palette (WCAG-AA on white), one type scale, and
  macros (`header / footer / preheader / section / subsection / untracked_box / levels_table /
  responsive_style`).
- `templates/_sentiment.html.j2` — the Market Sentiment widget (two metric cards, navy
  numerals, value-derived semantic colour, 5-band Fear & Greed strip, single-card fallback).

Rules:

- All four email templates MUST import from `_components.html.j2`.
- Any HTML entity inside a Jinja `{{ }}` expression must be replaced with the actual Unicode
  character (`—`, `…`, `©`).
- Preview workflow before deploying any visual change: render to HTML **and** PNG (Chrome
  headless), get visual approval, then deploy.

## Consequences

- Emails are visually consistent and accessibility-correct; mobile rendering is correct across
  all four.
- The SMA200 ETF-only gate is enforced via the shared `levels_table` macro, killing
  per-template drift.
- Future email changes have higher coordination cost — touching a shared macro affects all
  four templates. Worth it: the previous independence was buying drift, not flexibility.
- Outlook (desktop Windows) is the client most likely to silently break new CSS additions;
  verify after any change to `_components.html.j2`.

## References

- `templates/_components.html.j2`, `templates/_sentiment.html.j2`.
- Tests: `tests/test_email_templates.py` (autoescape-leak guard, ETF-only MA200, sentiment
  colour bands), `tests/test_email_indicators.py`.
