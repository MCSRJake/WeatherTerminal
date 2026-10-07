"""NWS numeric units; missing values are not zero and no units are guessed."""
from __future__ import annotations

import math
from typing import Any


def number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Invalid numeric value")
    return float(value)


def temperature(value: Any, unit: str | None = None) -> float | None:
    if isinstance(value, dict):
        unit, value = value.get("unitCode"), value.get("value")
    n = number(value)
    if n is None:
        return None
    if unit in ("F", "wmoUnit:degF", "unit:degF"):
        return n
    if unit in ("C", "wmoUnit:degC", "unit:degC"):
        return n * 9 / 5 + 32
    if unit in ("K", "wmoUnit:K"):
        return (n - 273.15) * 9 / 5 + 32
    raise ValueError("Unknown temperature unit")


def quantity(value: Any, kind: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Expected quantitative value")
    n = number(value.get("value"))
    if n is None:
        return None
    unit = value.get("unitCode")
    factors = {
        "speed": {"wmoUnit:m_s-1": 2.236936292, "wmoUnit:km_h-1": 1 / 1.609344,
                  "wmoUnit:mi_h-1": 1, "wmoUnit:kt": 1.150779448},
        "percent": {"wmoUnit:percent": 1, "unit:percent": 1},
        "direction": {"wmoUnit:degree_(angle)": 1, "unit:degree_(angle)": 1},
    }
    if not isinstance(unit, str) or unit not in factors[kind]:
        raise ValueError("Unknown quantity unit")
    n *= factors[kind][unit]
    if kind == "percent" and not 0 <= n <= 100:
        raise ValueError("Invalid percentage")
    if kind == "direction" and not 0 <= n <= 360:
        raise ValueError("Invalid direction")
    if kind == "speed" and n < 0:
        raise ValueError("Invalid speed")
    return n


def precipitation(value: Any) -> float | None:
    if isinstance(value, dict):
        if value.get("unitCode") not in (None, "wmoUnit:percent", "unit:percent"):
            raise ValueError("Unknown percentage unit")
        value = value.get("value")
    n = number(value)
    if n is not None and not 0 <= n <= 100:
        raise ValueError("Invalid percentage")
    return n
