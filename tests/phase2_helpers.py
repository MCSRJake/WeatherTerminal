"""Explicit synthetic fixtures; all writes use pytest temporary paths."""
from datetime import datetime, timezone
import json
from pathlib import Path

from weather_terminal.config import RuntimePaths
from weather_terminal.models import Component, EndpointDiscovery, Location, Provenance
from weather_terminal.offline import alerts_endpoint
from weather_terminal.storage import CacheStore, CachedPayload, SavedSelection

NOW = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)
LOCATION = Location("Oxford", "MS", 34.366, -89.519, timezone_name="America/Chicago")
DISCOVERY = EndpointDiscovery(
    "https://api.weather.gov/points/34.366,-89.519",
    "https://api.weather.gov/gridpoints/MEG/1,1/forecast/hourly",
    "https://api.weather.gov/gridpoints/MEG/1,1/forecast",
    "https://api.weather.gov/gridpoints/MEG/1,1/stations", "America/Chicago",
)
SELECTION = SavedSelection(LOCATION, DISCOVERY)
ENDPOINTS = {
    Component.ALERTS: alerts_endpoint(SELECTION),
    Component.CONDITIONS: "https://api.weather.gov/stations/KTUP/observations/latest",
    Component.HOURLY: DISCOVERY.hourly_url, Component.DAILY: DISCOVERY.daily_url,
}


def fixture_bodies():
    return json.loads((Path(__file__).parent / "fixtures" / "phase2-source.json").read_text(encoding="utf-8"))


def entry(component=Component.HOURLY, *, location=LOCATION, provenance=None, body=None, endpoint=None):
    return CachedPayload(location, component, endpoint or ENDPOINTS[component],
                         body if body is not None else fixture_bodies()[component.value],
                         provenance or Provenance(NOW, NOW, source_time=NOW))


def seed_cache(root):
    store = CacheStore(RuntimePaths(root))
    assert store.write_selection(SELECTION).saved
    for component in ENDPOINTS:
        source = datetime(2026, 1, 10, 11 if component == Component.CONDITIONS else 10, tzinfo=timezone.utc)
        assert store.write_component(entry(component, provenance=Provenance(NOW, NOW, source_time=source))).saved
    return store
