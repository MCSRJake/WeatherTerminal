"""Reusable, bounded HTTPS boundary and provider-wide request policy.

No client is created until the first request. Redirects are checked before sending;
TLS verification remains enabled. Phase 6 adds conditional cache orchestration.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
import json
import math
import random
from typing import Any, Mapping, Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from weather_terminal.clock import Clock, SystemClock
from weather_terminal.config import AppConfig
from weather_terminal.models import ErrorKind, OperationalError, as_utc
from weather_terminal.storage import MAX_BYTES


@dataclass(frozen=True)
class HttpReply:
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    url: str


class AsyncHttpTransport(Protocol):
    async def request(self, url: str, *, headers: Mapping[str, str],
                      params: Mapping[str, str] | None = None) -> HttpReply: ...
    async def aclose(self) -> None: ...


class ServiceFailure(Exception):
    def __init__(self, kind: ErrorKind, message: str, *, retryable: bool = False,
                 status: int | None = None) -> None:
        super().__init__(message)
        self.error = OperationalError(kind, message, retryable)
        self.status = status


def validate_destination(url: str, *, provider: str, geocoder_endpoint: str) -> str:
    try:
        value = urlsplit(url)
        if (value.scheme != "https" or not value.hostname or value.username is not None
                or value.password is not None or value.fragment or (provider == "nws" and value.port not in (None, 443))
                or any(ord(c) <= 32 or ord(c) == 127 for c in url)):
            raise ValueError
        if provider == "nws":
            if value.hostname != "api.weather.gov":
                raise ValueError
        elif provider == "geocoder":
            expected = urlsplit(geocoder_endpoint)
            # Explicit local configuration authorizes the geocoder host/path, not
            # arbitrary remote destinations or redirects to a different host.
            if (value.hostname, value.port, value.path) != (expected.hostname, expected.port, expected.path):
                raise ValueError
        else:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ServiceFailure(ErrorKind.INVALID_RESPONSE, "Provider endpoint is not an allowed HTTPS destination.") from None
    return url


class HttpxTransport:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.client: httpx.AsyncClient | None = None

    async def request(self, url: str, *, headers: Mapping[str, str],
                      params: Mapping[str, str] | None = None) -> HttpReply:
        if self.client is None:
            self.client = httpx.AsyncClient(
                verify=True, follow_redirects=False,
                timeout=httpx.Timeout(self.config.http.read_timeout_seconds,
                                      connect=self.config.http.connect_timeout_seconds),
            )
        async with self.client.stream("GET", url, headers=headers, params=params) as response:
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_BYTES:
                    raise ServiceFailure(ErrorKind.INVALID_RESPONSE, "Provider response is too large.")
            return HttpReply(response.status_code, dict(response.headers), bytes(chunks), str(response.url))

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON")
    return number


def decode_json(reply: HttpReply) -> Any:
    try:
        if len(reply.content) > MAX_BYTES:
            raise ValueError
        return json.loads(reply.content.decode("utf-8"), parse_constant=_reject_constant,
                          object_pairs_hook=_unique, parse_float=_float)
    except (ValueError, UnicodeError, RecursionError):
        raise ServiceFailure(ErrorKind.INVALID_RESPONSE, "Provider returned invalid JSON.") from None


def retry_after(value: str | None, now: datetime) -> float:
    if not value:
        return 0
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    except ValueError:
        pass
    try:
        return max(0, (as_utc(parsedate_to_datetime(value)) - as_utc(now)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return 0


class HttpService:
    def __init__(self, config: AppConfig, *, clock: Clock | None = None,
                 transport: AsyncHttpTransport | None = None) -> None:
        self.config = config
        self.clock = clock or SystemClock()
        self.transport = transport or HttpxTransport(config)
        self.locks = {"nws": asyncio.Lock(), "geocoder": asyncio.Lock()}
        self.next_allowed = {"nws": 0.0, "geocoder": 0.0}
        # Across all point selections, retries, redirects, and manual requests.
        self.alerts_next_allowed = 0.0
        self.closed = False

    async def get(self, url: str, *, provider: str,
                  params: Mapping[str, str] | None = None) -> HttpReply:
        validate_destination(url, provider=provider, geocoder_endpoint=self.config.geocoding.endpoint)
        if self.closed:
            raise ServiceFailure(ErrorKind.NETWORK, "HTTP session is closed.")
        try:
            async with asyncio.timeout(self.config.http.operation_timeout_seconds):
                return await self._get(url, provider, params)
        except TimeoutError:
            raise ServiceFailure(ErrorKind.NETWORK, "Request deadline exceeded; last good data is retained.", retryable=True) from None

    async def _get(self, url: str, provider: str, params: Mapping[str, str] | None) -> HttpReply:
        headers = {"User-Agent": self.config.http.identifying_user_agent,
                   "Accept": "application/geo+json" if provider == "nws" else "application/json",
                   "Accept-Language": "en"}
        current, query, redirects, attempts = url, params, 0, 0
        while True:
            try:
                reply = await self._paced_request(current, provider, headers, query)
            except (httpx.TransportError, OSError):
                if attempts >= 2:
                    raise ServiceFailure(ErrorKind.NETWORK, "Network request failed; last good data is retained.", retryable=True) from None
                attempts += 1
                self.next_allowed[provider] = max(self.next_allowed[provider], self.clock.monotonic() + 2 ** attempts + random.uniform(0, .25))
                continue
            validate_destination(reply.url, provider=provider, geocoder_endpoint=self.config.geocoding.endpoint)
            response_headers = {key.lower(): value for key, value in reply.headers.items()}
            if reply.status_code in (429, 500, 502, 503, 504):
                wait = max(retry_after(response_headers.get("retry-after"), self.clock.now()),
                           2 ** (attempts + 1) + random.uniform(0, .25))
                self.next_allowed[provider] = max(self.next_allowed[provider], self.clock.monotonic() + wait)
                if attempts >= 2 or wait >= self.config.http.operation_timeout_seconds:
                    raise ServiceFailure(ErrorKind.PROVIDER, "Provider unavailable or throttled; try again later.", retryable=True, status=reply.status_code)
                attempts += 1
                continue
            if reply.status_code in (301, 302, 303, 307, 308):
                redirects += 1
                if redirects > 3 or not response_headers.get("location"):
                    raise ServiceFailure(ErrorKind.INVALID_RESPONSE, "Provider redirect could not be followed safely.")
                current = validate_destination(urljoin(reply.url, response_headers["location"]), provider=provider,
                                               geocoder_endpoint=self.config.geocoding.endpoint)
                query = None
                continue
            if reply.status_code != 200:
                # No conditional requests are issued yet; a stray 304 is not success.
                unsupported = reply.status_code == 404 and provider == "nws" and urlsplit(current).path.startswith("/points/")
                if unsupported:
                    kind, message = ErrorKind.UNSUPPORTED, "No NWS forecast coverage for this point."
                elif reply.status_code == 404 and provider == "nws" and urlsplit(current).path.startswith("/stations/"):
                    kind, message = ErrorKind.MISSING_DATA, "NWS station/observation endpoint is unavailable; last good conditions retained."
                else:
                    kind, message = ErrorKind.PROVIDER, "Provider rejected the request; last good data is retained."
                raise ServiceFailure(kind, message, status=reply.status_code)
            return reply

    async def _paced_request(self, url, provider, headers, params):
        is_alert = provider == "nws" and urlsplit(url).path.startswith("/alerts")
        while True:
            async with self.locks[provider]:
                deadline = max(self.next_allowed[provider], self.alerts_next_allowed if is_alert else 0.0)
                delay = deadline - self.clock.monotonic()
                if delay >= self.config.http.operation_timeout_seconds:
                    raise ServiceFailure(ErrorKind.PROVIDER, "Provider retry window is still closed; try again later.", retryable=True)
                if delay <= 0:
                    spacing = self.config.geocoding.min_interval_seconds if provider == "geocoder" else 1.0
                    self.next_allowed[provider] = self.clock.monotonic() + spacing
                    if is_alert:
                        self.alerts_next_allowed = self.clock.monotonic() + 30.0
                    return await self.transport.request(url, headers=headers, params=params)
            # Waiting for alerts must not hold the provider lock and block an
            # otherwise eligible forecast/observation request. Recheck on wake.
            await self.clock.sleep(delay)

    async def aclose(self) -> None:
        self.closed = True
        await self.transport.aclose()
