"""Cache-first raw snapshots, restoration seam, and conservative legacy previews.

Semantic normalizers/coordinator own production weather interpretation; no HTTP
or weather normalization happens in this disk-loading module. Source previews
remain for explicitly networking-disabled historical tests only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Any

from weather_terminal.config import FreshnessSettings
from weather_terminal.freshness import (
    alert_expiry, assess_freshness, forecast_coverage, format_time, source_intervals,
)
from weather_terminal.models import Component, ComponentState
from weather_terminal.storage import CacheStore, CachedPayload, SavedSelection
from weather_terminal.text import plain_text

DISPLAY_COMPONENTS = (Component.ALERTS, Component.CONDITIONS, Component.HOURLY, Component.DAILY)
TITLES = {
    Component.ALERTS: "ALERTS", Component.CONDITIONS: "CURRENT CONDITIONS",
    Component.HOURLY: "NEXT 12 HOURS", Component.DAILY: "NEXT 7 DAYS",
}


def alerts_endpoint(selection: SavedSelection) -> str:
    point = selection.location
    return f"https://api.weather.gov/alerts/active?point={point.latitude},{point.longitude}"


@dataclass
class OfflineSnapshot:
    selection: SavedSelection | None = None
    entries: dict[Component, CachedPayload] = field(default_factory=dict)
    issues: tuple[str, ...] = ()

    @property
    def timezone_name(self) -> str | None:
        if self.selection is None:
            return None
        discovery = self.selection.discovery
        return (discovery.timezone_name if discovery is not None else None) or self.selection.location.timezone_name


def load_offline(store: CacheStore) -> OfflineSnapshot:
    selected = store.read_selection()
    snapshot = OfflineSnapshot(selection=selected.value, issues=selected.issues)
    if selected.value is None:
        return snapshot
    return load_selection(store, selected.value, issues=selected.issues)


def load_selection(store: CacheStore, selection: SavedSelection, *, issues: tuple[str, ...] = ()) -> OfflineSnapshot:
    """Load a known selection without rereading or changing remembered state."""
    snapshot = OfflineSnapshot(selection=selection, issues=issues)
    discovery = selection.discovery
    endpoints: dict[Component, str] = {Component.ALERTS: alerts_endpoint(selection)}
    if discovery is not None:
        endpoints.update({Component.POINT: discovery.point_url, Component.HOURLY: discovery.hourly_url,
                          Component.DAILY: discovery.daily_url, Component.STATIONS: discovery.stations_url})
    issues = list(snapshot.issues)
    for component in Component:
        result = store.read_component(selection.location, component, endpoint=endpoints.get(component))
        issues.extend(result.issues)
        if result.value is not None:
            snapshot.entries[component] = result.value
    snapshot.issues = tuple(issues)
    return snapshot


def restore_component(entry: CachedPayload, *, generation: int,
                      normalize: Callable[[dict[str, Any]], Any]) -> ComponentState[Any]:
    """Normalize before creating usable state; never change timestamps on a read."""
    data = normalize(entry.raw_body)
    if data is None:
        raise ValueError("Cached source body could not be normalized")
    return ComponentState(
        component=entry.component, location_key=entry.location.key, generation=generation,
        endpoint=entry.endpoint, data=data, raw_body=entry.raw_body,
        provenance=entry.provenance, loaded_from_disk=True,
        # Attempts, errors, in-flight work, and monotonic eligibility are session-only.
    )


def render_cached_component(entry: CachedPayload, now: datetime, settings: FreshnessSettings,
                            timezone_name: str | None) -> str:
    component, provenance = entry.component, entry.provenance
    lines = [f"{TITLES.get(component, component.value.upper())} — cached source preview",
             "No live check this session; weather interpretation/details not implemented.",
             f"Retrieved: {format_time(provenance.retrieved_at, timezone_name)}",
             f"Last checked: {format_time(provenance.last_checked_at, timezone_name)}",
             f"Source time: {format_time(provenance.source_time, timezone_name)}",
             "Last attempted: never this session"]
    lines.extend(assess_freshness(component, provenance, now, settings).warnings)
    if component in (Component.HOURLY, Component.DAILY):
        coverage = forecast_coverage(source_intervals(entry.raw_body), now, limit=12 if component == Component.HOURLY else 14)
        if coverage.status == "expired":
            lines.append("Forecast coverage expired — original dates retained (OUTDATED DATA).")
        elif coverage.status == "gap":
            lines.append("No source interval covers now; showing next supplied intervals.")
        elif coverage.status == "unavailable":
            lines.append("Forecast source coverage unavailable.")
        lines.append("Retained source intervals (not yet the final weather table):")
        for period in coverage.selected:
            label = period.body.get("shortForecast", period.body.get("name", ""))
            lines.append(f"{format_time(period.start, timezone_name)} → {format_time(period.end, timezone_name)} | {label}")
    elif component == Component.ALERTS:
        features = entry.raw_body["features"]
        actual = [f for f in features if f["properties"].get("status") == "Actual"]
        lines.append("Previously retrieved alerts — current status unverified.")
        if not features:
            lines.append("No alerts at last check — NOT a current all-clear.")
        else:
            # Do not display Test/Exercise messages as operational alerts.
            lines.append(f"Retained Actual records: {len(actual)} (not a current active-alert count).")
            for feature in actual:
                properties = feature["properties"]
                lines.append(f"{properties.get('event', 'Event unavailable')} | {alert_expiry(properties, now)}")
                if properties.get("expires"):
                    lines.append(f"Source expiration: {properties['expires']}")
    else:
        lines.append("Source payload retained; condition values/station selection are not yet implemented.")
    return plain_text("\n".join(lines))
