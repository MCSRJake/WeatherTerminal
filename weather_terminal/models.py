"""UI-independent location, provenance, and request-state foundations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import math
from typing import Any, Generic, TypeVar


def as_utc(value: datetime) -> datetime:
    """Reject naive instants; normalize aware instants instead of guessing a zone."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required")
    return value.astimezone(timezone.utc)


def parse_instant(value: str) -> datetime:
    return as_utc(datetime.fromisoformat(value))


class Component(StrEnum):
    CONDITIONS = "conditions"
    HOURLY = "hourly"
    DAILY = "daily"
    ALERTS = "alerts"
    POINT = "point"
    STATIONS = "stations"


class ErrorKind(StrEnum):
    NETWORK = "network"
    PROVIDER = "provider"
    UNSUPPORTED = "unsupported"
    MISSING_DATA = "missing_data"
    INVALID_RESPONSE = "invalid_response"
    STORAGE = "storage"


@dataclass(frozen=True)
class OperationalError:
    kind: ErrorKind
    message: str
    retryable: bool = False


@dataclass(frozen=True)
class Location:
    name: str
    state: str
    latitude: float
    longitude: float
    county: str | None = None
    timezone_name: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.state.strip():
            raise ValueError("Location needs a name and state")
        for name, lower, upper in (("latitude", -90, 90), ("longitude", -180, 180)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"Invalid {name}")
            if not math.isfinite(value) or not lower <= value <= upper:
                raise ValueError(f"Invalid {name}")

    @property
    def key(self) -> str:
        # Exact float coordinates; endpoint identity is a separate cache dimension.
        coordinates = f"{float(self.latitude).hex()},{float(self.longitude).hex()}"
        return hashlib.sha256(coordinates.encode("ascii")).hexdigest()[:24]

    @property
    def label(self) -> str:
        return f"{self.name}, {self.state}"


@dataclass(frozen=True)
class EndpointDiscovery:
    point_url: str
    hourly_url: str
    daily_url: str
    stations_url: str
    timezone_name: str | None
    # Destination validation belongs to the provider adapter, not this container.


@dataclass(frozen=True)
class Provenance:
    retrieved_at: datetime
    last_checked_at: datetime
    source_time: datetime | None = None
    etag: str | None = None
    last_modified: str | None = None
    fresh_until: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("retrieved_at", "last_checked_at", "source_time", "fresh_until"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, as_utc(value))
        if self.last_checked_at < self.retrieved_at:
            raise ValueError("Last checked time cannot precede body retrieval time")


T = TypeVar("T")


@dataclass
class ComponentState(Generic[T]):
    component: Component
    location_key: str
    generation: int
    endpoint: str | None = None
    data: T | None = None
    raw_body: dict[str, Any] | list[Any] | None = None
    provenance: Provenance | None = None
    loaded_from_disk: bool = False
    in_flight: bool = False
    last_attempted_at: datetime | None = None
    error: OperationalError | None = None
    next_eligible_monotonic: float = 0.0

    def begin_attempt(self, now: datetime) -> None:
        instant = as_utc(now)
        if self.in_flight:
            raise RuntimeError("Component request already in progress")
        self.last_attempted_at = instant
        self.in_flight = True
        # Preserve the previous failure notice until a request actually succeeds.

    def accept_body(
        self, *, data: T, raw_body: dict[str, Any] | list[Any],
        endpoint: str, provenance: Provenance,
    ) -> None:
        if data is None or raw_body is None:
            raise ValueError("Successful data cannot be None; empty alert sets are valid")
        self.data = data
        self.raw_body = raw_body
        self.endpoint = endpoint
        self.provenance = provenance
        self.loaded_from_disk = False
        self.error = None
        self.in_flight = False

    def accept_not_modified(self, now: datetime) -> None:
        if self.raw_body is None or self.data is None or self.provenance is None:
            raise ValueError("304 requires a usable cached body")
        previous = self.provenance
        checked = as_utc(now)
        if checked < previous.last_checked_at:
            raise ValueError("Revalidation time cannot move backwards")
        self.provenance = Provenance(
            retrieved_at=previous.retrieved_at, last_checked_at=checked,
            source_time=previous.source_time, etag=previous.etag,
            last_modified=previous.last_modified,
            # Revalidation freshness must be recomputed from response headers later.
            fresh_until=None,
        )
        self.loaded_from_disk = False
        self.in_flight = False
        self.error = None

    def fail(self, error: OperationalError) -> None:
        self.error = error
        self.in_flight = False
        # Successful data, timestamps, endpoint, and disk-origin flag stay untouched.

    def belongs_to(self, location_key: str, generation: int) -> bool:
        return self.location_key == location_key and self.generation == generation
