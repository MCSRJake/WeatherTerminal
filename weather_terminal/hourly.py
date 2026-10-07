"""Validated hourly forecast values and current-interval selection/rendering."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
import re
from typing import Any

from weather_terminal.config import FreshnessSettings
from weather_terminal.freshness import assess_freshness, format_time
from weather_terminal.formatting import format_number
from weather_terminal.models import Component, ErrorKind, Provenance, as_utc, parse_instant
from weather_terminal.services.http import ServiceFailure
from weather_terminal.text import plain_text
from weather_terminal.units import number as _number, temperature as temperature_f, precipitation


@dataclass(frozen=True)
class Hour:
    start: datetime
    end: datetime
    temperature_f: float | None
    precipitation_percent: float | None
    wind: str
    description: str


@dataclass(frozen=True)
class HourlyForecast:
    periods: tuple[Hour, ...]
    source_time: datetime | None




def _wind(speed: Any, direction: Any) -> str:
    if direction is not None and not isinstance(direction, str):
        raise ValueError("Invalid wind direction")
    if isinstance(speed, dict):
        value = _number(speed.get("value"))
        unit = speed.get("unitCode")
        factors = {"wmoUnit:km_h-1": 1 / 1.609344, "wmoUnit:m_s-1": 2.236936292,
                   "wmoUnit:mi_h-1": 1, "wmoUnit:kt": 1.150779448}
        if value is None:
            rendered = "Unavailable"
        elif not isinstance(unit, str) or unit not in factors:
            raise ValueError("Unsupported wind unit")
        elif value < 0:
            raise ValueError("Invalid wind speed")
        else:
            rendered = format_number(value * factors[unit], " mph")
    elif speed is None:
        rendered = "Unavailable"
    elif isinstance(speed, str):
        match = re.fullmatch(r"(\d+(?:\.\d+)?)(?: to (\d+(?:\.\d+)?))? (mph|km/h|m/s|kt|knots)", speed.strip(), re.IGNORECASE)
        if match:
            low, high, unit = match.groups()
            factor = {"mph": 1, "km/h": 1 / 1.609344, "m/s": 2.236936292, "kt": 1.150779448, "knots": 1.150779448}[unit.lower()]
            values = [float(low) * factor] + ([] if high is None else [float(high) * factor])
            if any(not math.isfinite(number) for number in values):
                raise ValueError("Invalid scalar wind")
            rendered = " to ".join(format_number(number) for number in values) + " mph"
        elif speed.casefold() in ("calm", "light and variable"):
            rendered = speed
        else:
            raise ValueError("Unsupported scalar wind representation")
    else:
        raise ValueError("Invalid wind speed")
    return plain_text(f"{direction or ''} {rendered}".strip())


def normalize_hourly(body: Any) -> HourlyForecast:
    try:
        if not isinstance(body, dict) or not isinstance(body.get("properties"), dict):
            raise ValueError
        properties = body["properties"]
        periods = properties.get("periods")
        if not isinstance(periods, list):
            raise ValueError
        result = []
        seen = set()
        for period in periods:
            if not isinstance(period, dict):
                raise ValueError
            start, end = parse_instant(period["startTime"]), parse_instant(period["endTime"])
            if end <= start or (start, end) in seen:
                raise ValueError
            seen.add((start, end))
            precip = precipitation(period.get("probabilityOfPrecipitation"))
            description = period.get("shortForecast")
            if description is not None and not isinstance(description, str):
                raise ValueError
            result.append(Hour(start, end, temperature_f(period.get("temperature"), period.get("temperatureUnit")),
                               precip, _wind(period.get("windSpeed"), period.get("windDirection")),
                               plain_text(description or "Unavailable")))
        ordered = tuple(sorted(result, key=lambda hour: hour.start))
        if any(a.end > b.start for a, b in zip(ordered, ordered[1:])):
            raise ValueError
        source = properties.get("updateTime") or properties.get("generatedAt")
        return HourlyForecast(ordered, parse_instant(source) if source else None)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise ServiceFailure(ErrorKind.INVALID_RESPONSE, "NWS hourly forecast has invalid fields or intervals.") from None


def select_hours(forecast: HourlyForecast, now: datetime) -> tuple[tuple[Hour, ...], str]:
    now = as_utc(now)
    future = tuple(hour for hour in forecast.periods if hour.end > now)
    if not forecast.periods:
        return (), "Unavailable: NWS supplied no hourly periods."
    if not future:
        return forecast.periods[-12:], "Forecast coverage expired — OUTDATED DATA; original dates retained."
    notice = []
    if not any(hour.start <= now < hour.end for hour in future):
        notice.append("No period covers now; showing next supplied periods.")
    selected = future[:12]
    if len(selected) < 12:
        notice.append(f"Only {len(selected)} hourly periods supplied for the remaining coverage.")
    if any(a.end < b.start for a, b in zip(selected, selected[1:])):
        notice.append("Source coverage contains gaps; missing hours are not interpolated.")
    return selected, " ".join(notice)


def render_hourly(forecast: HourlyForecast, provenance: Provenance, now: datetime,
                  timezone_name: str | None, settings: FreshnessSettings, *, cached: bool,
                  attempted: datetime | None = None, loading: bool = False,
                  error: str | None = None) -> str:
    status = "Cached — no successful network check this session" if cached else "Recently checked"
    if loading:
        status += " | Loading"
    if error:
        status += " | Refresh failed"
    lines = [f"NEXT 12 HOURS — {status}", f"Retrieved: {format_time(provenance.retrieved_at, timezone_name)}",
             f"Last checked: {format_time(provenance.last_checked_at, timezone_name)}",
             f"Forecast updated: {format_time(forecast.source_time, timezone_name)}",
             f"Last attempted: {format_time(attempted, timezone_name) if attempted else 'never this session'}"]
    if error:
        lines.append(error)
    lines.extend(assess_freshness(Component.HOURLY, provenance, now, settings).warnings)
    hours, notice = select_hours(forecast, now)
    if notice:
        lines.append(notice)
    lines.append("Time (local date/zone/offset) | Temp °F | Precip % | Wind | Conditions")
    for hour in hours:
        temp = format_number(hour.temperature_f, missing="—")
        precip = format_number(hour.precipitation_percent, "%", decimals=0, missing="—")
        lines.append(f"{format_time(hour.start, timezone_name)} | {temp} | {precip} | {hour.wind} | {hour.description}")
    lines.append("Twelve supplied hourly slots, including a partly elapsed current slot. Precip % is probability, not amount.")
    return plain_text("\n".join(lines))
