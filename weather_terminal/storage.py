"""Versioned last-good source storage. No I/O on import or construction.

Provider adapters must finish semantic validation before calling write_component.
This layer additionally checks JSON safety, cache identity, and basic source shape.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Generic, TypeVar
import unicodedata
from urllib.parse import urlsplit

from weather_terminal.config import RuntimePaths
from weather_terminal.models import Component, EndpointDiscovery, Location, Provenance, as_utc, parse_instant

SCHEMA_VERSION = 1
MAX_BYTES = 8 * 1024 * 1024
T = TypeVar("T")
BodyValidator = Callable[[Component, dict[str, Any]], None]


@dataclass(frozen=True)
class ReadResult(Generic[T]):
    value: T | None = None
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class WriteResult:
    saved: bool
    issue: str | None = None


@dataclass(frozen=True)
class SavedSelection:
    location: Location
    discovery: EndpointDiscovery | None = None


@dataclass(frozen=True)
class CachedPayload:
    location: Location
    component: Component
    endpoint: str
    raw_body: dict[str, Any]
    provenance: Provenance


@dataclass(frozen=True)
class CachedSearch:
    provider: str
    query: str
    retrieved_at: datetime
    raw_body: list[dict[str, Any]]
    results: tuple[Location, ...]


def _url(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ValueError("Invalid endpoint identity")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid endpoint identity") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)):
        raise ValueError("Invalid endpoint identity")
    # This checks identity syntax, NOT permission to make requests to this host.
    # Phase 3 adapters own destination/redirect allowlists and TLS policy.
    return value


def endpoint_key(endpoint: str) -> str:
    return hashlib.sha256(_url(endpoint).encode("utf-8")).hexdigest()


def normalize_query(query: str) -> str:
    if not isinstance(query, str) or any(unicodedata.category(c) in {"Cc", "Cf"} for c in query):
        raise ValueError("Query must be ordinary text")
    normalized = " ".join(unicodedata.normalize("NFKC", query).split()).casefold()
    if not normalized or len(normalized) > 200:
        raise ValueError("Query must be between 1 and 200 characters")
    return normalized


def search_key(provider: str, query: str) -> str:
    identity = [_url(provider), normalize_query(query)]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()


def _text(value: Any, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError("Invalid text field")
    return value


def _location(value: Any) -> Location:
    if not isinstance(value, dict):
        raise ValueError("Invalid location")
    return Location(
        name=_text(value.get("name")), state=_text(value.get("state")),
        latitude=value.get("latitude"), longitude=value.get("longitude"),
        county=_text(value.get("county"), optional=True),
        timezone_name=_text(value.get("timezone_name"), optional=True),
    )


def _discovery(value: Any) -> EndpointDiscovery | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Invalid endpoint discovery")
    return EndpointDiscovery(
        point_url=_url(value.get("point_url")), hourly_url=_url(value.get("hourly_url")),
        daily_url=_url(value.get("daily_url")), stations_url=_url(value.get("stations_url")),
        timezone_name=_text(value.get("timezone_name"), optional=True),
    )


def _instant(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Invalid timestamp")
    return parse_instant(value)


def _validator_header(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 2048 or any(not 32 <= ord(c) <= 126 for c in value):
        raise ValueError("Invalid cached HTTP validator")
    return value


def _provenance(value: Any) -> Provenance:
    if not isinstance(value, dict):
        raise ValueError("Invalid provenance")
    def optional_time(name: str) -> datetime | None:
        return None if value.get(name) is None else _instant(value[name])
    return Provenance(
        retrieved_at=_instant(value.get("retrieved_at")),
        last_checked_at=_instant(value.get("last_checked_at")),
        source_time=optional_time("source_time"), fresh_until=optional_time("fresh_until"),
        etag=_validator_header(value.get("etag")),
        last_modified=_validator_header(value.get("last_modified")),
    )


def _provenance_json(value: Provenance) -> dict[str, Any]:
    result = asdict(value)
    for name in ("retrieved_at", "last_checked_at", "source_time", "fresh_until"):
        result[name] = None if result[name] is None else as_utc(result[name]).isoformat()
    return result


def _envelope(value: Any, kind: str) -> dict[str, Any]:
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int:
        raise ValueError("Invalid cache envelope")
    if value["schema_version"] != SCHEMA_VERSION or value.get("kind") != kind:
        raise ValueError("Incompatible cache schema")
    return value


def validate_source_shape(component: Component, body: dict[str, Any]) -> None:
    """Structural guard only; weather units/status/required values belong to adapters."""
    if not isinstance(body, dict):
        raise ValueError("Source body must be an object")
    if component in (Component.ALERTS, Component.STATIONS):
        features = body.get("features")
        if not isinstance(features, list) or any(
            not isinstance(f, dict) or not isinstance(f.get("properties"), dict) for f in features
        ):
            raise ValueError("Source features must contain property objects")
    else:
        properties = body.get("properties")
        if not isinstance(properties, dict):
            raise ValueError("Source properties must be an object")
        if component in (Component.HOURLY, Component.DAILY):
            periods = properties.get("periods")
            if not isinstance(periods, list):
                raise ValueError("Forecast periods must be a list")
            for period in periods:
                if not isinstance(period, dict):
                    raise ValueError("Forecast period must be an object")
                start, end = _instant(period.get("startTime")), _instant(period.get("endTime"))
                if end <= start:
                    raise ValueError("Forecast interval must end after it starts")
        if component == Component.CONDITIONS and properties.get("timestamp") is not None:
            _instant(properties["timestamp"])


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def atomic_write_json(path: Path, value: Any) -> None:
    """Encode before touching the target; fsync temp data, replace, clean on failure.

    Same-directory replacement is atomic under normal local filesystem semantics.
    This is not a promise of power-loss durability of directory metadata.
    """
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8") + b"\n"
    if len(encoded) > MAX_BYTES:
        raise ValueError("Cache entry is too large")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".pending-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        # The temporary handle is closed before os.replace (required on Windows).
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # Interrupted writes/orphaned temp files are never read as entries.


class CacheStore:
    def __init__(self, paths: RuntimePaths, *, validator: BodyValidator | None = None) -> None:
        self.paths = paths
        self.validator = validator
        self._logged: set[str] = set()

    def _validate_source(self, component: Component, body: dict[str, Any]) -> None:
        validate_source_shape(component, body)
        if self.validator is not None:
            self.validator(component, body)

    def _issue(self, identity: str, message: str) -> str:
        # Bound repetitive diagnostics per store session; never log payload/query/path.
        if identity not in self._logged and len(self._logged) < 64:
            logging.getLogger("weather_terminal").warning("Local storage: %s", message)
            self._logged.add(identity)
        return message

    def _read(self, path: Path) -> ReadResult[Any]:
        try:
            with path.open("rb") as stream:
                encoded = stream.read(MAX_BYTES + 1)
            if len(encoded) > MAX_BYTES:
                raise ValueError("Oversized entry")
            value = json.loads(encoded.decode("utf-8"), object_pairs_hook=_no_duplicates,
                               parse_constant=_invalid_constant, parse_float=_finite_float)
            if not isinstance(value, dict):
                raise ValueError("Entry must be a JSON object")
            return ReadResult(value)
        except FileNotFoundError:
            return ReadResult()
        except (OSError, ValueError, UnicodeError, RecursionError):
            return ReadResult(issues=(self._issue(str(path), "An unreadable or corrupt local entry was ignored."),))

    def _write(self, path: Path, value: Any) -> WriteResult:
        try:
            atomic_write_json(path, value)
            return WriteResult(True)
        except (OSError, ValueError, TypeError, RecursionError):
            return WriteResult(False, self._issue(str(path), "Unable to save local data; in-memory data remains usable."))

    def _invalid(self, identity: str) -> tuple[str, ...]:
        return (self._issue(identity, "An invalid or incompatible local entry was ignored."),)

    def read_selection(self) -> ReadResult[SavedSelection]:
        result = self._read(self.paths.state_file)
        if result.value is None:
            return ReadResult(issues=result.issues)
        try:
            value = _envelope(result.value, "selection")
            location = _location(value.get("location"))
            if value.get("location_key") != location.key:
                raise ValueError("Location key mismatch")
            return ReadResult(SavedSelection(location, _discovery(value.get("discovery"))))
        except (ValueError, TypeError, AttributeError):
            return ReadResult(issues=self._invalid("selection"))

    def write_selection(self, selection: SavedSelection) -> WriteResult:
        try:
            location = _location(asdict(selection.location))
            discovery = _discovery(None if selection.discovery is None else asdict(selection.discovery))
            value = dict(schema_version=SCHEMA_VERSION, kind="selection", location_key=location.key,
                         location=asdict(location), discovery=None if discovery is None else asdict(discovery))
        except (ValueError, TypeError, AttributeError):
            return WriteResult(False, self._issue("selection-write", "Invalid selection was not saved."))
        return self._write(self.paths.state_file, value)

    def component_path(self, location: Location, component: Component, endpoint: str) -> Path:
        component = Component(component)  # Never allow arbitrary path fragments.
        return self.paths.cache_dir / location.key / component.value / (endpoint_key(endpoint) + ".json")

    def write_component(self, entry: CachedPayload) -> WriteResult:
        try:
            component = Component(entry.component)
            location = _location(asdict(entry.location))
            endpoint = _url(entry.endpoint)
            self._validate_source(component, entry.raw_body)
            provenance = _provenance(_provenance_json(entry.provenance))
            value = dict(schema_version=SCHEMA_VERSION, kind="component", location_key=location.key,
                         location=asdict(location), component=component.value, endpoint=endpoint,
                         endpoint_key=endpoint_key(endpoint), raw_body=entry.raw_body,
                         provenance=_provenance_json(provenance))
            path = self.component_path(location, component, endpoint)
        except (ValueError, TypeError, AttributeError, RecursionError):
            return WriteResult(False, self._issue("component-write", "Invalid source data was not saved."))
        return self._write(path, value)

    def _read_component_file(self, path: Path, location: Location, component: Component,
                             endpoint: str | None) -> ReadResult[CachedPayload]:
        result = self._read(path)
        if result.value is None:
            return ReadResult(issues=result.issues)
        try:
            value = _envelope(result.value, "component")
            saved_location = _location(value.get("location"))
            url = _url(value.get("endpoint"))
            if (saved_location.key != location.key or value.get("location_key") != location.key
                    or value.get("component") != component.value
                    or value.get("endpoint_key") != endpoint_key(url)
                    or path.name != endpoint_key(url) + ".json"
                    or (endpoint is not None and url != endpoint)):
                raise ValueError("Cache identity mismatch")
            self._validate_source(component, value.get("raw_body"))
            return ReadResult(CachedPayload(location, component, url, value["raw_body"],
                                            _provenance(value.get("provenance"))))
        except (ValueError, TypeError, AttributeError, RecursionError):
            return ReadResult(issues=self._invalid(str(path)))

    def read_component(self, location: Location, component: Component,
                       *, endpoint: str | None = None) -> ReadResult[CachedPayload]:
        component = Component(component)
        if endpoint is not None:
            return self._read_component_file(self.component_path(location, component, endpoint),
                                             location, component, endpoint)
        # A saved station endpoint may not be known until observation discovery.
        # Only this location/component is searched; no cross-location fallback.
        directory = self.paths.cache_dir / location.key / component.value
        issues: list[str] = []
        entries: list[CachedPayload] = []
        try:
            paths = sorted(directory.glob("*.json"))
        except OSError:
            return ReadResult(issues=self._invalid(str(directory)))
        for path in paths:
            if not re.fullmatch(r"[0-9a-f]{64}\.json", path.name):
                continue
            result = self._read_component_file(path, location, component, None)
            issues.extend(result.issues)
            if result.value is not None:
                entries.append(result.value)
        newest = max(entries, key=lambda e: (e.provenance.last_checked_at, e.provenance.retrieved_at, e.endpoint), default=None)
        return ReadResult(newest, tuple(issues))

    def _search_document(self) -> ReadResult[dict[str, Any]]:
        result = self._read(self.paths.cache_dir / "locations.json")
        if result.value is None:
            return ReadResult(issues=result.issues)
        try:
            value = _envelope(result.value, "searches")
            if not isinstance(value.get("entries"), dict):
                raise ValueError("Invalid search entries")
            return ReadResult(value)
        except (ValueError, TypeError):
            return ReadResult(issues=self._invalid("searches"))

    def read_search(self, provider: str, query: str) -> ReadResult[CachedSearch]:
        key = search_key(provider, query)
        document = self._search_document()
        if document.value is None:
            return ReadResult(issues=document.issues)
        value = document.value["entries"].get(key)
        if value is None:
            return ReadResult()
        try:
            if not isinstance(value, dict) or value.get("provider") != provider or value.get("query") != normalize_query(query):
                raise ValueError("Search identity mismatch")
            raw, results = value.get("raw_body"), value.get("results")
            if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw) or not isinstance(results, list):
                raise ValueError("Invalid search body")
            return ReadResult(CachedSearch(provider, normalize_query(query), _instant(value.get("retrieved_at")),
                                           raw, tuple(_location(item) for item in results)))
        except (ValueError, TypeError, AttributeError):
            return ReadResult(issues=self._invalid("search-" + key))

    def write_search(self, entry: CachedSearch) -> WriteResult:
        try:
            provider, query = _url(entry.provider), normalize_query(entry.query)
            if not isinstance(entry.raw_body, list) or any(not isinstance(item, dict) for item in entry.raw_body):
                raise ValueError("Invalid search body")
            results = [asdict(_location(asdict(item))) for item in entry.results]
            value = dict(provider=provider, query=query, retrieved_at=as_utc(entry.retrieved_at).isoformat(),
                         raw_body=entry.raw_body, results=results)
            key = search_key(provider, query)
        except (ValueError, TypeError, AttributeError):
            return WriteResult(False, self._issue("search-write", "Invalid search data was not saved."))
        document = self._search_document()
        if document.issues:
            return WriteResult(False, self._issue("search-preserve", "Existing search cache needs recovery before new searches can be saved."))
        payload = document.value or dict(schema_version=SCHEMA_VERSION, kind="searches", entries={})
        payload["entries"][key] = value
        return self._write(self.paths.cache_dir / "locations.json", payload)
