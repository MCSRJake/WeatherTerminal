"""Station ranking, usable observations, and honest station-condition display."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from weather_terminal.config import FreshnessSettings
from weather_terminal.freshness import assess_freshness, format_time
from weather_terminal.formatting import format_number
from weather_terminal.models import Component, ErrorKind, Location, Provenance, as_utc, parse_instant
from weather_terminal.services.http import ServiceFailure
from weather_terminal.text import plain_text
from weather_terminal.units import number, quantity, temperature

ID_PATTERN = r"[A-Za-z0-9_-]{1,64}"


def station_id(url: str) -> str:
    try:
        parsed = urlsplit(url)
        match = re.fullmatch(r"/stations/(" + ID_PATTERN + r")", parsed.path)
        if (parsed.scheme != "https" or parsed.hostname != "api.weather.gov" or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or not match):
            raise ValueError
        return match[1]
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid station identity") from None


def observation_endpoint(identifier: str) -> str:
    if not isinstance(identifier, str) or not re.fullmatch(ID_PATTERN, identifier):
        raise ValueError("Invalid station identifier")
    return f"https://api.weather.gov/stations/{identifier}/observations/latest"


def observation_station(endpoint: str) -> str:
    if not isinstance(endpoint, str) or not endpoint.endswith("/observations/latest"):
        raise ValueError("Invalid observation endpoint")
    return station_id(endpoint.removesuffix("/observations/latest"))


def coordinates(geometry: Any) -> tuple[float, float]:
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        raise ValueError("Missing station coordinates")
    c = geometry.get("coordinates")
    if not isinstance(c, list) or len(c) < 2:
        raise ValueError("Invalid station coordinates")
    lon, lat = number(c[0]), number(c[1])
    if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("Invalid station coordinates")
    return lat, lon


def distance_miles(location: Location, latitude: float, longitude: float) -> float:
    lat1, lat2 = math.radians(location.latitude), math.radians(latitude)
    dlat, dlon = lat2 - lat1, math.radians(longitude - location.longitude)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 3958.7613 * 2 * math.asin(math.sqrt(min(1, max(0, a))))


@dataclass(frozen=True)
class Station:
    identifier: str
    name: str
    latitude: float
    longitude: float
    miles: float


@dataclass(frozen=True)
class StationList:
    stations: tuple[Station, ...]
    notes: tuple[str, ...] = ()


def _validate_station_pagination(body: dict) -> None:
    """Allow NWS's redundant next link only when its ID filter is exhausted.

    The gridpoint station endpoint can return every station yet advertise a
    /stations?id[0]=...&cursor=... continuation. An exact match between that
    explicit filter and the returned identities leaves no unseen station IDs.
    Generic or partially covered pagination remains unsupported. Nothing here
    follows URLs or interprets the provider's opaque cursor.
    """
    pagination = body.get("pagination")
    if pagination is None:
        return
    if not isinstance(pagination, dict):
        raise ValueError
    next_url = pagination.get("next")
    if next_url is None or next_url == "":
        return
    if not isinstance(next_url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in next_url):
        raise ValueError
    parsed = urlsplit(next_url)
    if (parsed.scheme != "https" or parsed.hostname != "api.weather.gov"
            or parsed.port not in (None, 443) or parsed.username is not None
            or parsed.password is not None or parsed.path != "/stations" or parsed.fragment):
        raise ValueError
    filters: dict[int, str] = {}
    cursor_seen = False
    for key, value in parse_qsl(parsed.query, keep_blank_values=True,
                                strict_parsing=True, max_num_fields=4096):
        if key == "cursor":
            if cursor_seen or not value:
                raise ValueError
            cursor_seen = True
            continue
        match = re.fullmatch(r"id\[(0|[1-9][0-9]*)\]", key)
        if match is None or not re.fullmatch(ID_PATTERN, value):
            raise ValueError
        index = int(match[1])
        if index in filters:
            raise ValueError
        filters[index] = value
    expected = set(filters.values())
    if (not cursor_seen or not filters or set(filters) != set(range(len(filters)))
            or len(expected) != len(filters)):
        raise ValueError
    returned = set()
    for feature in body["features"]:
        if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
            raise ValueError
        identifier = feature["properties"].get("stationIdentifier")
        if not isinstance(identifier, str) or not re.fullmatch(ID_PATTERN, identifier):
            raise ValueError
        returned.add(identifier)
    if returned != expected:
        raise ValueError


def normalize_stations(body: Any, location: Location) -> StationList:
    try:
        if not isinstance(body, dict) or not isinstance(body.get("features"), list):
            raise ValueError
        _validate_station_pagination(body)
        stations = {}
        skipped = 0
        for feature in body["features"]:
            if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
                raise ValueError
            p = feature["properties"]
            try:
                identifier = p.get("stationIdentifier")
                if not isinstance(identifier, str) or not re.fullmatch(ID_PATTERN, identifier):
                    raise ValueError
                if p.get("@id") is not None and station_id(p["@id"]) != identifier:
                    raise ValueError
                lat, lon = coordinates(feature.get("geometry"))
                name = p.get("name")
                if name is not None and not isinstance(name, str):
                    raise ValueError
                station = Station(identifier, plain_text(name or identifier), lat, lon, distance_miles(location, lat, lon))
                old = stations.get(identifier)
                if old is not None and (old.latitude, old.longitude) != (lat, lon):
                    raise ValueError
                stations[identifier] = station
            except (ValueError, TypeError, AttributeError):
                skipped += 1
        ordered = tuple(sorted(stations.values(), key=lambda s: (s.miles, s.identifier)))
        notes = (f"{skipped} station records lacked usable identity/coordinates and were skipped.",) if skipped else ()
        return StationList(ordered, notes)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise ServiceFailure(ErrorKind.INVALID_RESPONSE, "NWS station list is invalid.") from None


@dataclass(frozen=True)
class Observation:
    identifier: str
    timestamp: datetime
    temperature_f: float
    description: str
    feels_like_f: float | None
    feels_like_source: str | None
    wind_mph: float | None
    gust_mph: float | None
    direction_degrees: float | None
    humidity_percent: float | None
    dewpoint_f: float | None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Conditions:
    observation: Observation
    station: Station | None
    selection_note: str = ""


def normalize_observation(body: Any, expected_identifier: str | None = None) -> Observation:
    try:
        if not isinstance(body, dict) or not isinstance(body.get("properties"), dict):
            raise ValueError
        p = body["properties"]
        identifier = station_id(p["station"])
        if expected_identifier is not None and identifier != expected_identifier:
            raise ValueError
        stamp = parse_instant(p["timestamp"])
        temp = temperature(p.get("temperature"))
        if temp is None:
            raise ServiceFailure(ErrorKind.MISSING_DATA, "Station has no usable temperature observation.")
        notes = []
        def optional(name, convert):
            try:
                return convert(p.get(name))
            except (ValueError, TypeError, AttributeError, OverflowError):
                notes.append(f"{name}: invalid optional value shown unavailable.")
                return None
        chill = optional("windChill", temperature)
        heat = optional("heatIndex", temperature)
        description = p.get("textDescription")
        if description is not None and not isinstance(description, str):
            description = None
            notes.append("Invalid optional conditions description shown unavailable.")
        return Observation(identifier, stamp, temp, plain_text(description or "Unavailable"),
                           chill if chill is not None else heat,
                           "NWS wind chill" if chill is not None else ("NWS heat index" if heat is not None else None),
                           optional("windSpeed", lambda v: quantity(v, "speed")),
                           optional("windGust", lambda v: quantity(v, "speed")),
                           optional("windDirection", lambda v: quantity(v, "direction")),
                           optional("relativeHumidity", lambda v: quantity(v, "percent")),
                           optional("dewpoint", temperature), tuple(notes))
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise ServiceFailure(ErrorKind.INVALID_RESPONSE, "NWS observation has invalid identity, temperature, or timestamp.") from None


def restore_conditions(body: dict, location: Location, stations: StationList | None = None,
                       endpoint: str | None = None) -> Conditions:
    try:
        expected = observation_station(endpoint) if endpoint is not None else None
        observation = normalize_observation(body, expected)
        station = next((s for s in stations.stations if s.identifier == observation.identifier), None) if stations else None
        if station is None:
            try:
                lat, lon = coordinates(body.get("geometry"))
                station = Station(observation.identifier, observation.identifier, lat, lon, distance_miles(location, lat, lon))
            except ValueError:
                pass
        return Conditions(observation, station, "Previously selected station; current station ranking is not verified.")
    except ValueError:
        raise ServiceFailure(ErrorKind.INVALID_RESPONSE, "Cached observation station identity is invalid.") from None


def fresh_observation(observation: Observation, now: datetime, threshold: float) -> bool:
    return 0 <= (as_utc(now) - observation.timestamp).total_seconds() <= threshold


def choose_observation(candidates: list[tuple[Station, Any]], now: datetime, threshold: float):
    """Result objects expose .observation; nearest fresh, otherwise newest usable."""
    if not candidates:
        return None
    fresh = [pair for pair in candidates if fresh_observation(pair[1].observation, now, threshold)]
    if fresh:
        return min(fresh, key=lambda pair: (pair[0].miles, pair[0].identifier))
    # Future instants cannot win the 'most recent timestamp' fallback and mask valid past data.
    past = [pair for pair in candidates if pair[1].observation.timestamp <= as_utc(now)]
    return min(past, key=lambda pair: (-pair[1].observation.timestamp.timestamp(), pair[0].miles, pair[0].identifier)) if past else None


def render_conditions(data: Conditions, provenance: Provenance, now: datetime, zone: str | None,
                      settings: FreshnessSettings, *, cached=False, attempted=None, loading=False, error=None) -> str:
    o, s = data.observation, data.station

    status = "Cached" if cached else "Recently checked"
    if loading:
        status += " | Loading"
    if error:
        status += " | Refresh failed"
    lines = [f"CURRENT CONDITIONS — {status}", f"Retrieved: {format_time(provenance.retrieved_at, zone)}",
             f"Last checked: {format_time(provenance.last_checked_at, zone)}",
             f"Observed: {format_time(o.timestamp, zone)}",
             f"Last attempted: {format_time(attempted, zone) if attempted else 'never this session'}",
             f"Station: {s.name if s else o.identifier} ({o.identifier}) | " + (f"{s.miles:.1f} miles from forecast point" if s else "Distance unavailable"),
             "Reporting-station observation, not a measurement at the exact city point.",
             f"{o.description} | Temperature: {format_number(o.temperature_f, ' °F')}",
             f"Feels like: {format_number(o.feels_like_f, ' °F')}" + (f" ({o.feels_like_source})" if o.feels_like_source else " (not supplied by NWS)"),
             f"Wind: {format_number(o.direction_degrees, '°', decimals=0)} | Sustained: {format_number(o.wind_mph, ' mph')} | Gusts: {format_number(o.gust_mph, ' mph')}",
             f"Humidity: {format_number(o.humidity_percent, '%', decimals=0)} | Dew point: {format_number(o.dewpoint_f, ' °F')}"]
    if data.selection_note:
        lines.append(data.selection_note)
    lines.extend(o.notes)
    lines.extend(assess_freshness(Component.CONDITIONS, provenance, now, settings).warnings)
    if error:
        lines.append(error)
    return plain_text("\n".join(lines))
