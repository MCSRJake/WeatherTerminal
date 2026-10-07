import asyncio
from dataclasses import replace
from datetime import timedelta
from email.utils import format_datetime

import httpx
import pytest

from weather_terminal.config import AppConfig, HttpSettings
from weather_terminal.services.http import HttpReply, HttpService, HttpxTransport, ServiceFailure, decode_json, retry_after, validate_destination
from tests.phase2_helpers import NOW
from tests.phase3_helpers import GEO, POINT, ScriptTransport, reply
from tests.test_app import FakeClock


@pytest.mark.parametrize("url", ["http://api.weather.gov/points/1,2", "https://evil.example/points/1,2",
    "https://api.weather.gov.evil.example/points/1,2", "https://user@api.weather.gov/points/1,2",
    "https://api.weather.gov:444/points/1,2", "https://api.weather.gov/points/1,2#x", "https://api.weather.gov/ bad"])
def test_nws_destinations_reject_unsafe_urls(url):
    with pytest.raises(ServiceFailure):
        validate_destination(url, provider="nws", geocoder_endpoint=GEO)


def test_geocoder_allows_only_configured_host_path_port():
    validate_destination(GEO, provider="geocoder", geocoder_endpoint=GEO)
    custom = "https://geo.example:8443/search"
    validate_destination(custom, provider="geocoder", geocoder_endpoint=custom)
    for url in ("https://evil.example/search", "https://nominatim.openstreetmap.org/other"):
        with pytest.raises(ServiceFailure):
            validate_destination(url, provider="geocoder", geocoder_endpoint=GEO)


async def test_retry_spacing_headers_and_redirect_destination_checks():
    clock = FakeClock()
    transport = ScriptTransport(clock, [reply(GEO, status=503), reply(GEO, [])])
    http = HttpService(AppConfig(), clock=clock, transport=transport)
    await http.get(GEO, provider="geocoder")
    assert len(transport.calls) == 2
    assert transport.calls[1][3] - transport.calls[0][3] >= 1
    assert transport.calls[0][1]["User-Agent"].startswith("WeatherTerminal/")
    assert transport.calls[0][1]["Accept"] == "application/json"
    await http.aclose()
    assert transport.closed


async def test_foreign_redirect_is_rejected_before_second_request():
    clock = FakeClock()
    transport = ScriptTransport(clock, [reply(POINT, status=302, headers={"Location": "https://evil.example/data"})])
    http = HttpService(AppConfig(), clock=clock, transport=transport)
    with pytest.raises(ServiceFailure):
        await http.get(POINT, provider="nws")
    assert len(transport.calls) == 1


async def test_same_nws_host_redirect_is_allowed():
    clock = FakeClock()
    target = "https://api.weather.gov/points/1,2"
    transport = ScriptTransport(clock, [reply(POINT, status=302, headers={"Location": "/points/1,2"}), reply(target, {})])
    http = HttpService(AppConfig(), clock=clock, transport=transport)
    await http.get(POINT, provider="nws")
    assert len(transport.calls) == 2


async def test_retry_after_preserved_across_later_operations_without_waiting_forever():
    clock = FakeClock()
    transport = ScriptTransport(clock, [reply(POINT, status=429, headers={"Retry-After": "3600"})])
    http = HttpService(AppConfig(), clock=clock, transport=transport)
    for _ in range(2):
        with pytest.raises(ServiceFailure):
            await http.get(POINT, provider="nws")
    assert len(transport.calls) == 1
    assert http.next_allowed["nws"] >= 3600


@pytest.mark.parametrize("status", [400, 401, 403, 404, 304])
async def test_nontransient_status_is_not_retried(status):
    clock = FakeClock()
    transport = ScriptTransport(clock, [reply(POINT, status=status)])
    with pytest.raises(ServiceFailure):
        await HttpService(AppConfig(), clock=clock, transport=transport).get(POINT, provider="nws")
    assert len(transport.calls) == 1


async def test_transport_errors_retry_at_most_twice():
    clock = FakeClock()
    transport = ScriptTransport(clock, [httpx.ConnectError("simulated") for _ in range(3)])
    with pytest.raises(ServiceFailure, match="Network request failed"):
        await HttpService(AppConfig(), clock=clock, transport=transport).get(POINT, provider="nws")
    assert len(transport.calls) == 3


async def test_whole_operation_deadline_bounds_hanging_transport(monkeypatch):
    # A tiny timeout is only a unit-test override; user settings remain validated.
    class Hanging:
        async def request(self, *args, **kwargs):
            await asyncio.Event().wait()
        async def aclose(self):
            pass
    http = HttpService(AppConfig(), transport=Hanging())
    from types import SimpleNamespace
    http.config = SimpleNamespace(http=SimpleNamespace(operation_timeout_seconds=.01,
        identifying_user_agent="WeatherTerminal/test"), geocoding=AppConfig().geocoding)
    with pytest.raises(ServiceFailure, match="deadline"):
        await http.get(POINT, provider="nws")


@pytest.mark.parametrize("content", [b"{", b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', b'\xff'])
def test_json_invalid_and_nonfinite_values_rejected(content):
    with pytest.raises(ServiceFailure):
        decode_json(HttpReply(200, {}, content, POINT))


def test_retry_after_seconds_and_http_date():
    assert retry_after("120", NOW) == 120
    assert retry_after(format_datetime(NOW + timedelta(hours=1), usegmt=True), NOW) == 3600
    assert retry_after("nonsense", NOW) == 0
    assert retry_after("NaN", NOW) == 0


def test_transport_constructor_is_lazy():
    transport = HttpxTransport(AppConfig())
    assert transport.client is None


async def test_concurrent_geocoder_calls_share_attempt_spacing():
    clock = FakeClock()
    transport = ScriptTransport(clock, [reply(GEO, []), reply(GEO, [])])
    http = HttpService(AppConfig(), clock=clock, transport=transport)
    await asyncio.gather(http.get(GEO, provider="geocoder"), http.get(GEO, provider="geocoder"))
    assert transport.calls[1][3] - transport.calls[0][3] >= 1


async def test_alert_requests_retry_redirect_and_changed_point_share_30_second_gate():
    from tests.phase4_helpers import ALERTS, RoutingTransport
    clock = FakeClock()
    other = "https://api.weather.gov/alerts/active?point=35,-90"
    transport = RoutingTransport(clock, {
        ALERTS: [reply(ALERTS, status=503), reply(ALERTS, {"features": []}),
                 reply(ALERTS, status=302, headers={"Location": other})],
        other: [reply(other, {"features": []}), reply(other, {"features": []})],
    })
    http = HttpService(AppConfig(), clock=clock, transport=transport)
    await http.get(ALERTS, provider="nws")
    await http.get(ALERTS, provider="nws")
    await http.get(other, provider="nws")
    times = [call[1] for call in transport.calls]
    assert len(times) == 5 and all(b - a >= 30 for a, b in zip(times, times[1:]))


async def test_alert_wait_releases_provider_lock_for_independent_hourly_request():
    from tests.phase4_helpers import ALERTS, RoutingTransport
    from tests.phase3_helpers import HOURLY, hourly_body
    entered, release = asyncio.Event(), asyncio.Event()
    class HeldClock(FakeClock):
        async def sleep(self, seconds):
            if seconds >= 20:
                entered.set()
                await release.wait()
            await super().sleep(seconds)
    clock = HeldClock()
    transport = RoutingTransport(clock, {ALERTS: [reply(ALERTS, {"features": []}), reply(ALERTS, {"features": []})],
                                         HOURLY: [reply(HOURLY, hourly_body())]})
    http = HttpService(AppConfig(), clock=clock, transport=transport)
    await http.get(ALERTS, provider="nws")
    task = asyncio.create_task(http.get(ALERTS, provider="nws"))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        await asyncio.wait_for(http.get(HOURLY, provider="nws"), 2)
        assert transport.calls[-1][0] == HOURLY
    finally:
        release.set()
    await task
    assert transport.calls[-1][1] - transport.calls[0][1] >= 30


async def test_station_404_is_missing_observation_not_unsupported_city():
    from weather_terminal.models import ErrorKind
    from tests.phase4_helpers import obs_url
    clock = FakeClock()
    transport = ScriptTransport(clock, [reply(obs_url(), status=404)])
    with pytest.raises(ServiceFailure) as failure:
        await HttpService(AppConfig(), clock=clock, transport=transport).get(obs_url(), provider="nws")
    assert failure.value.error.kind == ErrorKind.MISSING_DATA
    assert failure.value.status == 404 and len(transport.calls) == 1
