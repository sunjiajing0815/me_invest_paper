"""Allocation donut PNG generation (Pillow)."""
from __future__ import annotations

from io import BytesIO

from investor.services.charts import (
    ALLOC_PALETTE,
    CASH_COLOR,
    OTHER_COLOR,
    build_allocation_pie,
)


def _decode(png: bytes):  # type: ignore[no-untyped-def]
    from PIL import Image
    return Image.open(BytesIO(png))


def test_returns_png_of_requested_size() -> None:
    png = build_allocation_pie([28.0, 22.0, 50.0], ["#2f5d8a", "#3a8fb7", CASH_COLOR], size=300)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic
    img = _decode(png)
    assert img.size == (300, 300)


def test_single_slice_full_circle() -> None:
    png = build_allocation_pie([100.0], ["#2f5d8a"], size=120)
    assert _decode(png).size == (120, 120)


def test_palette_has_distinct_colors() -> None:
    assert len(ALLOC_PALETTE) >= 8
    assert len(set(ALLOC_PALETTE)) == len(ALLOC_PALETTE)
    assert CASH_COLOR not in ALLOC_PALETTE
    assert OTHER_COLOR not in ALLOC_PALETTE


def test_zero_total_returns_empty_png_safely() -> None:
    # Defensive: all-zero values must not raise (no division by zero).
    png = build_allocation_pie([0.0, 0.0], ["#2f5d8a", "#3a8fb7"], size=100)
    assert _decode(png).size == (100, 100)
