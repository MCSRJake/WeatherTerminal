"""Pure age/coverage calculations and unambiguous location-time formatting."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from weather_terminal.config import FreshnessSettings
from weather_terminal.models import Component, ComponentState, Provenance, as_utc, parse_instant


def format_time(instant: datetime | None, timezone_name: str | None = None) -> str:
    if instant is None:
        return "Unavailable"
    value = as_utc(instant)
    if timezone_name:
        try:
            value = value.astimezone(ZoneInfo(timezone_name))
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            return value.strftime("%Y-%m-%d %H:%M:%S UTC +0000 (timezone unavailable)")
    return value.strftime("%Y-%m-%d %H:%M:%S %Z %z")


@dataclass(frozen=True)
class Freshness:
    verification_stale: bool
    observation_stale: bool | None
    warnings: tuple[str, ...]


def assess_freshness(component: Component, provenance: Provenance, now: datetime,
                     settings: FreshnessSettings) -> Freshness:
    now = as_utc(now)
    threshold = {
        Component.ALERTS: settings.alerts_check_seconds,
        Component.CONDITIONS: settings.conditions_check_seconds,
        Component.HOURLY: settings.forecast_check_seconds,
        Component.DAILY: settings.forecast_check_seconds,
    }.get(component)
    warnings: list[str] = []
    check_age = (now - provenance.last_checked_at).total_seconds()
    if provenance.retrieved_at > now or provenance.last_checked_at > now:
        warnings.append("Successful timestamp is in the future; check the system clock.")
    verification_stale = check_age < 0 or (threshold is not None and check_age > threshold)
    if verification_stale:
        warnings.append("Last successful check is stale or cannot be verified.")
    observation_stale: bool | None = None
    if provenance.source_time is not None and provenance.source_time > now:
        warnings.append("Source timestamp is in the future; source age is unverified.")
    if component == Component.CONDITIONS:
        if provenance.source_time is None:
            warnings.append("Observation source time unavailable; observation age is unverified.")
        elif provenance.source_time <= now:
            observation_stale = (now - provenance.source_time).total_seconds() > settings.observation_seconds
            if observation_stale:
                warnings.append("Observation stale (source time, not download time).")
    return Freshness(verification_stale, observation_stale, tuple(warnings))


@dataclass(frozen=True)
class SourceInterval:
    start: datetime
    end: datetime
    body: dict[str, Any]


def source_intervals(body: dict[str, Any]) -> tuple[SourceInterval, ...]:
    """Dates are source validity intervals, never display labels rolled forward."""
    periods = body.get("properties", {}).get("periods", [])
    result: list[SourceInterval] = []
    for period in periods:
        start, end = parse_instant(period["startTime"]), parse_instant(period["endTime"])
        if end <= start:
            raise ValueError("Invalid source interval")
        result.append(SourceInterval(start, end, period))
    return tuple(sorted(result, key=lambda p: (p.start, p.end)))


@dataclass(frozen=True)
class Coverage:
    status: str  # available, gap, expired, unavailable
    selected: tuple[SourceInterval, ...]


def forecast_coverage(intervals: tuple[SourceInterval, ...], now: datetime,
                      *, limit: int = 12) -> Coverage:
    now = as_utc(now)
    if limit < 1:
        raise ValueError("Coverage limit must be positive")
    if not intervals:
        return Coverage("unavailable", ())
    applicable = tuple(p for p in intervals if p.end > now)
    if not applicable:
        return Coverage("expired", intervals[-limit:])
    status = "available" if any(p.start <= now < p.end for p in applicable) else "gap"
    return Coverage(status, applicable[:limit])


def alert_expiry(properties: dict[str, Any], now: datetime) -> str:
    """No assertion of current alert status follows from an unexpired cached date."""
    expires = properties.get("expires")
    if expires is None:
        return "Expiration unavailable; current status unverified"
    try:
        instant = parse_instant(expires)
    except (ValueError, TypeError, AttributeError):
        return "Expiration invalid; current status unverified"
    return "Expired" if instant <= as_utc(now) else "Not known expired; current status unverified"


def state_labels(state: ComponentState[Any], now: datetime, settings: FreshnessSettings) -> tuple[str, ...]:
    labels: list[str] = []
    if state.loaded_from_disk:
        labels.append("Cached")
    if state.in_flight:
        labels.append("Loading")
    if state.error is not None:
        labels.append("Refresh failed")
    if state.provenance is None:
        labels.append("Unavailable")
    else:
        labels.extend(assess_freshness(state.component, state.provenance, now, settings).warnings)
    return tuple(labels)
