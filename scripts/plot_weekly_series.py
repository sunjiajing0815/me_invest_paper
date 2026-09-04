#!/usr/bin/env python3
"""Render the weekly equity / cash-percentage series as an SVG for the README.

Standard library only — no matplotlib, no new runtime dependency. Emits a
self-contained SVG with its own cream background, so it stays legible in GitHub's
light and dark themes without needing a second dark-mode variant.

Figures are transcribed from the weekly stats cards in docs/weekly-outcome/.
To add a week: append one row to WEEKS and re-run.

    uv run python scripts/plot_weekly_series.py
"""

from __future__ import annotations

from pathlib import Path

# (week, label, equity_usd, cash_pct) — from docs/weekly-outcome/week_NN_stats_card.png
WEEKS: list[tuple[int, str, float, float]] = [
    (1,  "May 25", 101_900, 69),
    (2,  "Jun 1",  100_094, 57),
    (3,  "Jun 8",  100_262, 54),
    (4,  "Jun 15", 100_762, 54),
    (5,  "Jun 22",  99_071, 46),
    (6,  "Jun 29", 100_479, 46),
    (7,  "Jul 6",  100_901, 46),
    (8,  "Jul 13",  99_308, 43),
    (9,  "Jul 20",  98_172, 39),
    (10, "Jul 27", 100_575, 34),
    (11, "Aug 3",  103_395, 33),
    (12, "Aug 10", 103_269, 33),
    (13, "Aug 17", 102_809, 34),
    (14, "Aug 24", 103_521, 33),
]

# Palette sampled from the weekly stats cards so the chart matches them exactly.
BG = "#faf8f3"
INK = "#2c2c2c"
MUTED = "#888888"
EQUITY = "#3f72af"   # the cards' "invested" blue
CASH = "#adb5bd"     # the cards' "cash" grey
GRID = "#e6e2d8"
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"

W, H = 920, 480
L, R, T, B = 80, 78, 74, 96          # plot margins
PW, PH = W - L - R, H - T - B

EQ_LO, EQ_HI = 97_000, 104_000
CASH_LO, CASH_HI = 30, 72


def _x(i: int) -> float:
    return L + PW * i / (len(WEEKS) - 1)


def _y_eq(v: float) -> float:
    return T + PH * (1 - (v - EQ_LO) / (EQ_HI - EQ_LO))


def _y_cash(v: float) -> float:
    return T + PH * (1 - (v - CASH_LO) / (CASH_HI - CASH_LO))


def _text(x: float, y: float, s: str, *, size: int, fill: str = INK,
          anchor: str = "start", weight: str = "normal", style: str = "normal") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" '
        f'fill="{fill}" text-anchor="{anchor}" font-weight="{weight}" '
        f'font-style="{style}">{s}</text>'
    )


def build() -> str:
    p: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" role="img" '
        f'aria-label="Equity and cash percentage across 14 weeks on a paper account">',
        f'<rect width="{W}" height="{H}" fill="{BG}"/>',
        _text(W / 2, 34, f"Equity and Cash — {len(WEEKS)} Weeks",
              size=19, anchor="middle", weight="bold"),
        _text(W / 2, 55,
              f"Paper account · cash fell {WEEKS[0][3]:.0f}% → {WEEKS[-1][3]:.0f}%"
              " as positions were built toward target",
              size=12, fill=MUTED, anchor="middle", style="italic"),
    ]

    # ── horizontal grid + left axis (equity) ─────────────────────────────────
    for v in range(97_000, 104_001, 1_000):
        y = _y_eq(v)
        p.append(f'<line x1="{L}" y1="{y:.1f}" x2="{L + PW}" y2="{y:.1f}" '
                 f'stroke="{GRID}" stroke-width="1"/>')
        p.append(_text(L - 10, y + 4, f"${v // 1000}k", size=11, fill=MUTED, anchor="end"))

    # ── right axis (cash %) ──────────────────────────────────────────────────
    for v in range(30, 73, 10):
        p.append(_text(L + PW + 10, _y_cash(v) + 4, f"{v}%", size=11, fill=MUTED))

    # ── x labels ─────────────────────────────────────────────────────────────
    for i, (wk, label, _, _) in enumerate(WEEKS):
        x = _x(i)
        p.append(_text(x, H - B + 22, f"W{wk}", size=10, fill=MUTED, anchor="middle"))
        if i % 2 == 0:
            p.append(_text(x, H - B + 38, label, size=9, fill=MUTED, anchor="middle"))

    # ── cash % line (drawn first, sits behind) ───────────────────────────────
    cash_pts = " ".join(f"{_x(i):.1f},{_y_cash(c):.1f}" for i, (_, _, _, c) in enumerate(WEEKS))
    p.append(f'<polyline points="{cash_pts}" fill="none" stroke="{CASH}" '
             f'stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>')
    for i, (_, _, _, c) in enumerate(WEEKS):
        p.append(f'<circle cx="{_x(i):.1f}" cy="{_y_cash(c):.1f}" r="3" fill="{CASH}"/>')

    # ── equity line ──────────────────────────────────────────────────────────
    eq_pts = " ".join(f"{_x(i):.1f},{_y_eq(e):.1f}" for i, (_, _, e, _) in enumerate(WEEKS))
    p.append(f'<polyline points="{eq_pts}" fill="none" stroke="{EQUITY}" '
             f'stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>')
    for i, (_, _, e, _) in enumerate(WEEKS):
        p.append(f'<circle cx="{_x(i):.1f}" cy="{_y_eq(e):.1f}" r="3" fill="{EQUITY}"/>')

    # ── $100k reference line ─────────────────────────────────────────────────
    y100 = _y_eq(100_000)
    p.append(f'<line x1="{L}" y1="{y100:.1f}" x2="{L + PW}" y2="{y100:.1f}" '
             f'stroke="{MUTED}" stroke-width="1" stroke-dasharray="4 4" opacity="0.55"/>')
    p.append(_text(L + PW - 4, y100 - 7, "$100k start", size=9, fill=MUTED,
               anchor="end", style="italic"))

    # ── annotate the low and the high ────────────────────────────────────────
    i_lo = min(range(len(WEEKS)), key=lambda i: WEEKS[i][2])
    i_hi = max(range(len(WEEKS)), key=lambda i: WEEKS[i][2])
    p.append(_text(_x(i_lo), _y_eq(WEEKS[i_lo][2]) + 20, f"low ${WEEKS[i_lo][2]:,.0f}",
                   size=9, fill=EQUITY, anchor="middle"))
    # anchored end-ward so it does not collide with the right-hand % axis labels
    p.append(_text(_x(i_hi) - 7, _y_eq(WEEKS[i_hi][2]) - 11,
                   f"high ${WEEKS[i_hi][2]:,.0f}",
                   size=9, fill=EQUITY, anchor="end", weight="bold"))

    # ── legend ───────────────────────────────────────────────────────────────
    ly = H - 22
    p.append(f'<line x1="{L}" y1="{ly - 4}" x2="{L + 22}" y2="{ly - 4}" '
             f'stroke="{EQUITY}" stroke-width="2.5"/>')
    p.append(_text(L + 29, ly, "Equity (left)", size=11, fill=INK))
    p.append(f'<line x1="{L + 132}" y1="{ly - 4}" x2="{L + 154}" y2="{ly - 4}" '
             f'stroke="{CASH}" stroke-width="2.5"/>')
    p.append(_text(L + 161, ly, "Cash % of equity (right)", size=11, fill=INK))
    p.append(_text(W - R, ly, "paper account · not real money",
                   size=10, fill=MUTED, anchor="end", style="italic"))

    p.append("</svg>")
    return "\n".join(p)


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "docs" / "weekly-outcome" / "equity-and-cash.svg"
    out.write_text(build(), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size:,} bytes, {len(WEEKS)} weeks)")


if __name__ == "__main__":
    main()
