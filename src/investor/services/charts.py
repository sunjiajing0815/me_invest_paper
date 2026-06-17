"""Email chart rendering — allocation donut PNG via Pillow.

Pillow is imported lazily inside the render function so this module's constants
(palettes) can be imported by pure data code (e.g. daily_report.compose) without
pulling in the imaging stack.
"""

from __future__ import annotations

from io import BytesIO

# Categorical palette for allocation slices — harmonised with the email design
# system (navy/blue base, AA-friendly accents). Assigned to tickers by order.
ALLOC_PALETTE: list[str] = [
    "#2f5d8a",  # blue
    "#3a8fb7",  # cyan-blue
    "#5cab7d",  # green-teal
    "#c8851b",  # amber (WARN accent)
    "#8a6fb0",  # purple
    "#d98c5f",  # warm orange
    "#4f9d9d",  # teal
    "#b8627d",  # rose
    "#6b8e5a",  # olive
    "#5a7da3",  # steel blue
]
OTHER_COLOR = "#aeb4c0"  # grouped small slices
CASH_COLOR = "#cdd2db"   # cash — neutral, distinct from "Other"

_SS = 4  # supersample factor for anti-aliasing


def build_allocation_pie(
    values: list[float],
    colors: list[str],
    *,
    size: int = 300,
    hole_ratio: float = 0.58,
) -> bytes:
    """Render a donut chart of ``values`` (any positive scale) to PNG bytes.

    ``colors`` must align 1:1 with ``values``. White slice separators + a white
    centre hole give the donut look on the email's white background. Returns PNG
    bytes suitable for an inline (Content-ID) email attachment.
    """
    from PIL import Image, ImageDraw

    s = size * _SS
    img = Image.new("RGB", (s, s), "#ffffff")
    draw = ImageDraw.Draw(img)
    pad = int(s * 0.04)
    bbox = (pad, pad, s - pad, s - pad)

    total = sum(v for v in values if v > 0)
    if total > 0:
        # PIL angles: 0deg at 3 o'clock, increasing clockwise; start at top (-90).
        start = -90.0
        for value, color in zip(values, colors, strict=False):
            if value <= 0:
                continue
            sweep = 360.0 * value / total
            draw.pieslice(
                bbox, start, start + sweep,
                fill=color, outline="#ffffff", width=max(1, _SS * 2),
            )
            start += sweep

    # Punch the centre hole (donut) in the email background colour.
    cx = cy = s / 2
    hole_r = (s - 2 * pad) * hole_ratio / 2
    draw.ellipse((cx - hole_r, cy - hole_r, cx + hole_r, cy + hole_r), fill="#ffffff")

    img = img.resize((size, size), Image.Resampling.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
