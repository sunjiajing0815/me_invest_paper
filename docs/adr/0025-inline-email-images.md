# ADR-0025 — Inline (Content-ID) email images and the allocation donut

**Status:** Accepted
**Date:** 2026-06-17

## Context

The daily report email needed a graphical pie/donut of the current allocation
(including cash), styled to match the email design system. Email rendering is hostile
to charts:

- Gmail (the primary recipient) strips `<svg>` and ignores CSS `conic-gradient`, so a
  pure-HTML/CSS pie cannot be drawn reliably.
- Remote `<img src="https://…">` charts are blocked until the reader clicks "display
  images" and require the app to be internet-reachable from wherever mail is read.

The existing `SMTPEmailer` only built a `multipart/alternative` message (plain + HTML),
with no path for binary attachments, and the project had no image-rendering dependency.

## Decisions

### 1. Charts ship as inline Content-ID (CID) images, not SVG/CSS/remote URLs

`SMTPEmailer` now builds a `multipart/related` message when `inline_images` are supplied:
the `multipart/alternative` (plain+HTML) part plus one inline `image/png` per Content-ID.
The HTML references each chart as `<img src="cid:alloc_pie">`. CID images are part of the
message body, so Gmail renders them inline by default (no "display images" prompt) and they
survive offline. When no images are supplied the message stays a plain
`multipart/alternative` — existing emails (weekly, movers, etc.) are unchanged.

`EmailSender.send()` gains an optional `inline_images: dict[str, bytes] | None = None`
parameter (Protocol, `SMTPEmailer`, `FakeEmailer`). MIME construction is extracted into
`SMTPEmailer._build_message()` so the related/alternative structure is unit-testable
without an SMTP server.

### 2. Server-side PNG rendering via Pillow

`services/charts.py::build_allocation_pie()` draws an anti-aliased donut (4× supersample
→ LANCZOS downscale) and returns PNG bytes. Pillow is imported lazily inside the function
so the module's palette constants can be imported by pure data code
(`daily_report.compose`) without pulling in the imaging stack. Pillow (`pillow>=10.4`) is a
new runtime dependency — it is small, ubiquitous, and ships manylinux wheels, so it adds no
build burden to the slim app image.

### 3. One palette, no colour drift between donut and legend

`charts.ALLOC_PALETTE` (+ `OTHER_COLOR`, `CASH_COLOR`) is the single source of truth for
slice colours. `compose_daily_report` assigns each `AllocationSlice` its colour from this
palette; both the PNG and the HTML legend read `slice.color`, so they can never disagree.
The legend swatches use the `bgcolor` table attribute (universally email-safe), not CSS
backgrounds.

### 4. Slice composition

Distribution is over `equity = Σ position market_value + cash`. Positions are sorted
largest-first; beyond the top 8 they fold into an "Other" slice; cash (when positive) is
always the final slice. Position values are summed as USD (the active accounts are
USD-denominated); a future multi-currency account would need conversion before summing.

## Consequences

- True circular donut renders inline in Gmail with the email palette, no remote hosting.
- The emailer can now carry any inline image; future charts (e.g. a weekly drift sparkline)
  reuse the same `inline_images` seam.
- New runtime dependency (Pillow). If chart rendering ever raises, the daily job logs a
  warning and sends the email without the image — the HTML legend still renders, and there
  is no broken-image icon (the `<img>` is gated on an `alloc_chart` flag set only when the
  PNG was produced).
- S/R "Levels at a Glance" was already removed from the daily email (weekly-only); the
  allocation donut now anchors the daily Allocation section.
