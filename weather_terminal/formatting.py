"""Consistent display precision for normalized weather numbers; no data mutation."""
from __future__ import annotations


def format_number(value: float | None, suffix: str = "", *, decimals: int = 1,
                  missing: str = "Unavailable") -> str:
    """Format a finite normalized value, keeping missing distinct from real zero.

    Temperatures and speeds use one decimal; percentages and directions use
    zero decimals at their call sites. Suppress negative zero after rounding.
    """
    if value is None:
        return missing
    rendered = f"{value:.{decimals}f}"
    if rendered.startswith("-") and float(rendered) == 0:
        rendered = rendered[1:]
    return rendered + suffix
