"""Seven local start-date rows; nighttime lows are forecasts, not observed minima."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from weather_terminal.config import FreshnessSettings
from weather_terminal.freshness import assess_freshness, format_time
from weather_terminal.formatting import format_number
from weather_terminal.models import Component, ErrorKind, Provenance, as_utc, parse_instant
from weather_terminal.services.http import ServiceFailure
from weather_terminal.text import plain_text
from weather_terminal.units import precipitation, temperature


@dataclass(frozen=True)
class DailyPeriod:
    start: datetime
    end: datetime
    daytime: bool
    temperature_f: float | None
    precipitation_percent: float | None
    short: str
    detailed: str


@dataclass(frozen=True)
class DailyForecast:
    periods: tuple[DailyPeriod, ...]
    source_time: datetime | None


@dataclass(frozen=True)
class Day:
    date: date
    daytime: DailyPeriod | None
    nighttime: DailyPeriod | None


def normalize_daily(body: Any) -> DailyForecast:
    try:
        if not isinstance(body, dict) or not isinstance(body.get("properties"), dict):
            raise ValueError
        p = body["properties"]
        if not isinstance(p.get("periods"), list):
            raise ValueError
        periods = []
        for row in p["periods"]:
            if not isinstance(row, dict) or type(row.get("isDaytime")) is not bool:
                raise ValueError
            start, end = parse_instant(row["startTime"]), parse_instant(row["endTime"])
            if end <= start:
                raise ValueError
            for field in ("shortForecast", "detailedForecast"):
                if row.get(field) is not None and not isinstance(row[field], str):
                    raise ValueError
            periods.append(DailyPeriod(start, end, row["isDaytime"],
                                       temperature(row.get("temperature"), row.get("temperatureUnit")),
                                       precipitation(row.get("probabilityOfPrecipitation")),
                                       plain_text(row.get("shortForecast") or "Unavailable"),
                                       plain_text(row.get("detailedForecast") or "Unavailable")))
        ordered = tuple(sorted(periods, key=lambda row: row.start))
        if any(a.end > b.start for a, b in zip(ordered, ordered[1:])):
            raise ValueError
        source = p.get("updateTime") or p.get("generatedAt")
        return DailyForecast(ordered, parse_instant(source) if source else None)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise ServiceFailure(ErrorKind.INVALID_RESPONSE, "NWS daily forecast has invalid fields or intervals.") from None


def location_zone(name: str | None):
    try:
        return ZoneInfo(name) if name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return timezone.utc


def select_days(forecast: DailyForecast, now: datetime, zone: str | None) -> tuple[tuple[Day, ...], str]:
    tz = location_zone(zone)
    local_today = as_utc(now).astimezone(tz).date()
    grouped = {}
    for row in forecast.periods:
        key = (row.start.astimezone(tz).date(), row.daytime)
        if key in grouped:
            # Multiple periods for a date/type are not merged into fabricated daily extrema.
            raise ServiceFailure(ErrorKind.INVALID_RESPONSE, "NWS supplied duplicate day/night periods for a local date.")
        grouped[key] = row
    expired = bool(forecast.periods) and all(row.end <= as_utc(now) for row in forecast.periods)
    first = min((key[0] for key in grouped), default=local_today) if expired else local_today
    dates = [first + timedelta(days=i) for i in range(7)]
    days = tuple(Day(day, grouped.get((day, True)), grouped.get((day, False))) for day in dates)
    if expired:
        notice = "Forecast coverage expired — OUTDATED DATA; original forecast dates retained."
    elif not forecast.periods:
        notice = "Unavailable: NWS supplied no daily periods."
    elif not any(row.start <= as_utc(now) < row.end for row in forecast.periods):
        notice = "No supplied daily period covers now; missing periods remain Unavailable."
    else:
        notice = ""
    return days, notice


def render_daily(forecast: DailyForecast, provenance: Provenance, now: datetime, zone: str | None,
                 settings: FreshnessSettings, *, cached=False, attempted=None, loading=False, error=None) -> str:
    days, notice = select_days(forecast, now, zone)
    status = "Cached" if cached else "Recently checked"
    if loading:
        status += " | Loading"
    if error:
        status += " | Refresh failed"
    lines = [f"NEXT 7 DAYS — {status}", f"Retrieved: {format_time(provenance.retrieved_at, zone)}",
             f"Last checked: {format_time(provenance.last_checked_at, zone)}",
             f"Forecast updated: {format_time(forecast.source_time, zone)}",
             f"Last attempted: {format_time(attempted, zone) if attempted else 'never this session'}"]
    lines.extend(assess_freshness(Component.DAILY, provenance, now, settings).warnings)
    if error:
        lines.append(error)
    if notice:
        lines.append(notice)
    lines.append("Local date | Day high °F | Overnight low °F | Day / Night | Day / Night Precip %")
    for day in days:
        def value(period, field, suffix="", *, decimals=1):
            n = getattr(period, field) if period else None
            return format_number(n, suffix, decimals=decimals, missing="—")
        d, n = day.daytime, day.nighttime
        lines.append(f"{day.date:%a %Y-%m-%d} | {value(d, 'temperature_f')} | {value(n, 'temperature_f')} | "
                     f"{d.short if d else 'Unavailable'} / {n.short if n else 'Unavailable'} | "
                     f"{value(d, 'precipitation_percent', '%', decimals=0)} / {value(n, 'precipitation_percent', '%', decimals=0)}")
    lines.append("Low is the overnight forecast for each period's local start date; missing highs are not inferred.")
    return plain_text("\n".join(lines))
