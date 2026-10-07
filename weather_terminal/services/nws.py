"""NWS discovered forecasts/stations, usable observations, and official point alerts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from weather_terminal.hourly import HourlyForecast, normalize_hourly
from weather_terminal.daily import normalize_daily
from weather_terminal.alerts import normalize_alerts
from weather_terminal.conditions import normalize_stations, normalize_observation, observation_endpoint
from weather_terminal.models import EndpointDiscovery, ErrorKind, Location, Provenance
from weather_terminal.services.http import HttpService, ServiceFailure, decode_json, validate_destination


def point_url(location: Location) -> str:
    # NWS accepts four decimal places; saved city coordinates/cache keys retain
    # their full precision. This rounding is only the service's point query.
    return f"https://api.weather.gov/points/{location.latitude:.4f},{location.longitude:.4f}"


def validate_discovery(discovery: EndpointDiscovery, geocoder_endpoint: str) -> None:
    for url in (discovery.point_url, discovery.hourly_url, discovery.daily_url, discovery.stations_url):
        validate_destination(url, provider="nws", geocoder_endpoint=geocoder_endpoint)
    if not urlsplit(discovery.point_url).path.startswith("/points/"):
        raise ServiceFailure(ErrorKind.INVALID_RESPONSE, "Invalid NWS point endpoint.")
    for url, suffix in ((discovery.hourly_url, "/forecast/hourly"), (discovery.daily_url, "/forecast"),
                        (discovery.stations_url, "/stations")):
        path = urlsplit(url).path
        if not path.startswith("/gridpoints/") or not path.endswith(suffix) or urlsplit(url).query:
            raise ServiceFailure(ErrorKind.UNSUPPORTED, "NWS does not supply the required forecast endpoints for this point.")


def normalize_point(body: Any, endpoint: str, geocoder_endpoint: str) -> EndpointDiscovery:
    try:
        if not isinstance(body, dict) or not isinstance(body.get("properties"), dict):
            raise ValueError
        properties = body["properties"]
        urls = [properties.get(key) for key in ("forecastHourly", "forecast", "observationStations")]
        if any(not isinstance(value, str) or not value for value in urls):
            raise ServiceFailure(ErrorKind.UNSUPPORTED, "NWS does not supply the required forecasts for this point.")
        zone = properties.get("timeZone")
        if zone is not None:
            if not isinstance(zone, str):
                raise ValueError
            try:
                ZoneInfo(zone)
            except (ZoneInfoNotFoundError, ValueError):
                # Explicit UTC fallback is safer than guessing the machine's zone.
                zone = None
        discovery = EndpointDiscovery(endpoint, *urls, zone)
        validate_discovery(discovery, geocoder_endpoint)
        return discovery
    except (ValueError, TypeError, AttributeError):
        raise ServiceFailure(ErrorKind.INVALID_RESPONSE, "NWS point metadata is invalid.") from None


@dataclass(frozen=True)
class PointResult:
    discovery: EndpointDiscovery
    body: dict[str, Any]
    provenance: Provenance


@dataclass(frozen=True)
class HourlyResult:
    forecast: HourlyForecast
    body: dict[str, Any]
    provenance: Provenance


@dataclass(frozen=True)
class WeatherResult:
    data: Any
    body: dict[str, Any]
    provenance: Provenance
    endpoint: str


class NwsService:
    def __init__(self, http: HttpService) -> None:
        self.http = http

    def _provenance(self, headers, source_time=None) -> Provenance:
        now = self.http.clock.now()
        lowered = {key.lower(): value for key, value in headers.items()}
        # Headers are retained for Phase 6's conditional request support. Never
        # claim cache reads as revalidation; this phase issues unconditional GETs.
        return Provenance(now, now, source_time, lowered.get("etag"), lowered.get("last-modified"))

    async def point(self, location: Location) -> PointResult:
        endpoint = point_url(location)
        reply = await self.http.get(endpoint, provider="nws")
        body = decode_json(reply)
        discovery = normalize_point(body, endpoint, self.http.config.geocoding.endpoint)
        return PointResult(discovery, body, self._provenance(reply.headers))

    async def hourly(self, discovery: EndpointDiscovery) -> HourlyResult:
        validate_discovery(discovery, self.http.config.geocoding.endpoint)
        reply = await self.http.get(discovery.hourly_url, provider="nws")
        body = decode_json(reply)
        forecast = normalize_hourly(body)
        return HourlyResult(forecast, body, self._provenance(reply.headers, forecast.source_time))

    async def daily(self, discovery: EndpointDiscovery) -> WeatherResult:
        validate_discovery(discovery, self.http.config.geocoding.endpoint)
        reply = await self.http.get(discovery.daily_url, provider="nws")
        body = decode_json(reply)
        data = normalize_daily(body)
        return WeatherResult(data, body, self._provenance(reply.headers, data.source_time), discovery.daily_url)

    async def stations(self, discovery: EndpointDiscovery, location: Location) -> WeatherResult:
        validate_discovery(discovery, self.http.config.geocoding.endpoint)
        reply = await self.http.get(discovery.stations_url, provider="nws")
        body = decode_json(reply)
        data = normalize_stations(body, location)
        return WeatherResult(data, body, self._provenance(reply.headers), discovery.stations_url)

    async def observation(self, identifier: str) -> WeatherResult:
        endpoint = observation_endpoint(identifier)
        reply = await self.http.get(endpoint, provider="nws")
        body = decode_json(reply)
        data = normalize_observation(body, identifier)
        return WeatherResult(data, body, self._provenance(reply.headers, data.timestamp), endpoint)

    async def alerts(self, location: Location) -> WeatherResult:
        from weather_terminal.offline import alerts_endpoint
        from weather_terminal.storage import SavedSelection
        endpoint = alerts_endpoint(SavedSelection(location))
        reply = await self.http.get(endpoint, provider="nws")
        body = decode_json(reply)
        data = normalize_alerts(body)
        return WeatherResult(data, body, self._provenance(reply.headers), endpoint)


def validate_point_location(discovery: EndpointDiscovery, location: Location) -> None:
    """Reject a saved discovery for a different point, including same-host URLs."""
    import math
    try:
        parsed = urlsplit(discovery.point_url)
        if parsed.query:
            raise ValueError
        latitude, longitude = map(float, parsed.path.removeprefix("/points/").split(","))
        if (not math.isfinite(latitude) or not math.isfinite(longitude)
                or abs(latitude - location.latitude) > .000051
                or abs(longitude - location.longitude) > .000051):
            raise ValueError
    except (ValueError, TypeError):
        raise ServiceFailure(ErrorKind.INVALID_RESPONSE, "Saved NWS metadata belongs to a different forecast point.") from None
