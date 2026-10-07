"""Phase 4 full-component integration and UI; deterministic synthetic responses only."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import sys

import pytest
from textual.widgets import Static

from weather_terminal.app import WeatherApp, LocationPreview, SourceDetails
from weather_terminal.config import AppConfig, RuntimePaths
from weather_terminal.models import Component, Location, ErrorKind
from weather_terminal.offline import load_offline
from weather_terminal.services.http import HttpService, ServiceFailure
from weather_terminal.services.nws import normalize_point, point_url
from weather_terminal.slice import WeatherSlice, WEATHER_COMPONENTS
from weather_terminal.storage import CacheStore, CachedPayload, WriteResult
from tests.phase2_helpers import LOCATION, NOW
from tests.phase3_helpers import GEO, POINT, POINT_BODY, HOURLY, hourly_body, reply
from tests.phase4_helpers import (
    DAILY, STATIONS, ALERTS, RoutingTransport, successful_routes, obs_url, observation_body,
    daily_body, stations_body, alert_feature,
)
from tests.test_app import FakeClock


@pytest.fixture(autouse=True)
def streams(monkeypatch, capsys):
    monkeypatch.setattr(sys, "__stdout__", sys.stdout)
    monkeypatch.setattr(sys, "__stderr__", sys.stderr)


def make(root, routes):
    clock = FakeClock()
    transport = RoutingTransport(clock, routes)
    http = HttpService(AppConfig(), clock=clock, transport=transport)
    controller = WeatherSlice(AppConfig(), CacheStore(RuntimePaths(root)), http)
    return controller, transport, clock


async def refresh_all(controller):
    return await asyncio.gather(*(controller.refresh(c) for c in WEATHER_COMPONENTS))


async def populated(root, alerts=None):
    controller, transport, clock = make(root, successful_routes(alerts=alerts))
    assert await controller.confirm(LOCATION)
    assert all(await refresh_all(controller))
    return controller, transport, clock


async def test_all_components_live_normalized_persisted_then_cached_without_time_changes(tmp_path):
    controller, transport, clock = await populated(tmp_path, [alert_feature()])
    assert controller.states[Component.CONDITIONS].data.observation.temperature_f == 32
    assert len(controller.states[Component.DAILY].data.periods) == 14
    assert len(controller.states[Component.ALERTS].data.alerts) == 1
    before = {c: state.provenance for c, state in controller.states.items()}
    restored, _, _ = make(tmp_path, {})
    restored.restore(load_offline(restored.store))
    assert set(restored.states) == set(WEATHER_COMPONENTS)
    for c, state in restored.states.items():
        assert state.loaded_from_disk and state.error is None
        assert state.provenance == before[c]
    assert restored.states[Component.CONDITIONS].data.station.name == "Synthetic station SYN1"
    assert restored.station_data is not None
    assert all(call[2]["User-Agent"] for call in transport.calls)


async def test_partial_component_failures_preserve_independent_memory_disk_and_timestamps(tmp_path):
    controller, transport, clock = await populated(tmp_path, [alert_feature()])
    before = {c: state.provenance for c, state in controller.states.items()}
    bytes_before = {c: controller.store.component_path(LOCATION, c, state.endpoint).read_bytes() for c, state in controller.states.items()}
    clock.instant += timedelta(minutes=5)
    clock.elapsed += 300
    transport.routes[ALERTS].append(reply(ALERTS, status=403))
    transport.routes[DAILY].append(reply(DAILY, daily_body()))
    transport.routes[HOURLY].append(reply(HOURLY, hourly_body()))
    # Cached working station fails; next nearest five are checked, all unusable.
    for i in range(1, 4):
        transport.routes[obs_url(f"SYN{i}")].append(reply(obs_url(f"SYN{i}"), status=403))
    results = await refresh_all(controller)
    assert results == [False, False, True, True]
    for c in (Component.ALERTS, Component.CONDITIONS):
        state = controller.states[c]
        assert state.error and state.provenance is before[c]
        assert controller.store.component_path(LOCATION, c, state.endpoint).read_bytes() == bytes_before[c]
    assert controller.hourly.provenance.retrieved_at > before[Component.HOURLY].retrieved_at
    assert controller.states[Component.DAILY].provenance.retrieved_at > before[Component.DAILY].retrieved_at


async def test_successful_empty_alert_set_authoritatively_replaces_previous_set(tmp_path):
    controller, transport, clock = await populated(tmp_path, [alert_feature()])
    await clock.sleep(30)
    transport.routes[ALERTS].append(reply(ALERTS, {"features": []}))
    assert await controller.refresh(Component.ALERTS)
    assert not controller.states[Component.ALERTS].data.alerts
    assert controller.store.read_component(LOCATION, Component.ALERTS, endpoint=ALERTS).value.raw_body == {"features": []}


@pytest.mark.parametrize("component", [Component.ALERTS, Component.DAILY, Component.CONDITIONS])
async def test_invalid_replacement_cannot_poison_component_cache(tmp_path, component):
    controller, transport, clock = await populated(tmp_path, [alert_feature()])
    await clock.sleep(30)
    state = controller.states[component]
    before = state.provenance
    previous = controller.store.component_path(LOCATION, component, state.endpoint).read_bytes()
    if component == Component.ALERTS:
        transport.routes[ALERTS].append(reply(ALERTS, {"features": [{}]}))
    elif component == Component.DAILY:
        body = daily_body()
        body["properties"]["periods"][0]["isDaytime"] = None
        transport.routes[DAILY].append(reply(DAILY, body))
    else:
        for i in range(1, 4):
            body = observation_body(f"SYN{i}")
            body["properties"]["temperature"] = {"value": None}
            transport.routes[obs_url(f"SYN{i}")].append(reply(obs_url(f"SYN{i}"), body))
    assert not await controller.refresh(component)
    assert state.provenance is before and state.error
    assert controller.store.component_path(LOCATION, component, state.endpoint).read_bytes() == previous


async def test_usable_memory_retained_when_new_component_persistence_fails(tmp_path, monkeypatch):
    controller, _, _ = make(tmp_path, successful_routes(alerts=[alert_feature()]))
    await controller.confirm(LOCATION)
    monkeypatch.setattr(controller.store, "write_component", lambda entry: WriteResult(False, "Synthetic disk unavailable"))
    assert all(await refresh_all(controller))
    assert all(controller.states[c].data is not None for c in WEATHER_COMPONENTS)
    assert controller.snapshot.issues


@pytest.mark.parametrize("component,endpoint,body", [(Component.ALERTS, ALERTS, {"features": [alert_feature()]}),
    (Component.DAILY, DAILY, daily_body()), (Component.CONDITIONS, obs_url(), observation_body())])
async def test_late_old_component_response_cannot_update_new_point_or_write_cache(tmp_path, component, endpoint, body):
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow(url):
        entered.set()
        await release.wait()
        return reply(url, body)
    routes = {POINT: [reply(POINT, POINT_BODY)], endpoint: [slow]}
    if component == Component.CONDITIONS:
        routes[STATIONS] = [reply(STATIONS, stations_body(1))]
    controller, _, clock = make(tmp_path, routes)
    await controller.confirm(LOCATION)
    task = asyncio.create_task(controller.refresh(component))
    await asyncio.wait_for(entered.wait(), 2)
    other = Location("Memphis", "TN", 35.15, -90.05)
    discovery = normalize_point(POINT_BODY, point_url(other), GEO)
    async def support(location):
        from weather_terminal.models import Provenance
        return discovery, CachedPayload(location, Component.POINT, point_url(other), POINT_BODY, Provenance(clock.now(), clock.now()))
    controller._point = support
    assert await controller.confirm(other)
    release.set()
    assert not await task
    assert controller.snapshot.selection.location.key == other.key
    assert component not in controller.states
    assert controller.store.read_component(LOCATION, component).value is None
    assert controller.store.read_component(other, component).value is None


async def test_weather_components_do_not_require_working_forecast_metadata_for_point_alerts(tmp_path):
    controller, transport, clock = await populated(tmp_path)
    await clock.sleep(86401)
    transport.routes[POINT].append(reply(POINT, status=403))
    transport.routes[ALERTS].append(reply(ALERTS, {"features": [alert_feature()]}))
    assert not await controller.refresh(Component.HOURLY)
    assert await controller.refresh(Component.ALERTS)
    assert controller.states[Component.ALERTS].data.alerts


async def test_full_ui_startup_panels_full_source_text_and_quit(tmp_path):
    controller, _, _ = await populated(tmp_path, [alert_feature()])
    # Restart from real-format cached bodies; mocked outage responses preserve all panels.
    clock = FakeClock()
    # Restart later in wall time, with a new process-relative monotonic epoch.
    # Rewinding before the old station check would correctly request metadata.
    clock.instant = controller.http.clock.now() + timedelta(minutes=1)
    routes = {HOURLY: [reply(HOURLY, status=403)], DAILY: [reply(DAILY, status=403)], ALERTS: [reply(ALERTS, status=403)],
              **{obs_url(f"SYN{i}"): [reply(obs_url(f"SYN{i}"), status=403)] for i in range(1, 4)}}
    transport = RoutingTransport(clock, routes)
    app = WeatherApp(config=AppConfig(), paths=controller.store.paths, clock=clock,
                     http=HttpService(AppConfig(), clock=clock, transport=transport))
    async with app.run_test(size=(80, 24)) as pilot:
        assert not isinstance(app.screen, LocationPreview)
        await asyncio.wait_for(asyncio.shield(app.refresh_task), 5)
        app._update_clock()
        await pilot.pause()
        assert "NOT VERIFIED" in str(app.clock_label.content)
        for c in WEATHER_COMPONENTS:
            panel = str(app.query_one(f"#{c.value}-panel", Static).content)
            assert "Retrieved:" in panel and "Last checked:" in panel and "Refresh failed" in panel
        assert "Observed:" in str(app.panel_refs[Component.CONDITIONS].content)
        assert "Overnight low" in str(app.panel_refs[Component.DAILY].content)
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, SourceDetails)
        text = str(app.screen.query_one("#detail-text", Static).content)
        assert "Do not paraphrase." in text and "Keep all lines." in text and "snapshot" in text.lower()
        await pilot.press("escape", "d")
        await pilot.pause()
        assert isinstance(app.screen, SourceDetails)
        assert "Second line retained." in str(app.screen.query_one("#detail-text", Static).content)
        await pilot.press("escape")
        await pilot.resize_terminal(120, 40)
        assert app.columns.has_class("wide")
        await pilot.press("q")
    assert transport.closed and not app.tasks


async def test_full_ui_live_success_header_and_independent_refresh_coalescing(tmp_path):
    routes = successful_routes()
    controller, transport, clock = make(tmp_path, routes)
    await controller.confirm(LOCATION)
    # Reuse cached point from confirmation; no startup lookup, all four new requests.
    app = WeatherApp(config=AppConfig(), paths=controller.store.paths, clock=clock, http=controller.http)
    async with app.run_test() as pilot:
        await asyncio.wait_for(asyncio.shield(app.refresh_task), 5)
        app._update_clock()
        await pilot.pause()
        assert "No active alerts for this point" in str(app.clock_label.content)
        for c in WEATHER_COMPONENTS:
            assert app.weather.states[c].data is not None
        # Per-component refresh calls return the same owned task while in progress.
        started, release = asyncio.Event(), asyncio.Event()
        async def held(url):
            started.set()
            await release.wait()
            return reply(url, hourly_body())
        transport.routes[HOURLY].append(held)
        one = app.start_component(Component.HOURLY)
        two = app.start_component(Component.HOURLY)
        assert one is two
        await asyncio.wait_for(started.wait(), 2)
        await pilot.resize_terminal(80, 24)
        release.set()
        assert await one
        await pilot.press("q")
    assert not app.tasks and transport.closed


async def test_semantically_corrupt_component_cache_is_excluded_without_erasing_other_weather(tmp_path):
    controller, _, _ = await populated(tmp_path, [alert_feature()])
    entry = controller.snapshot.entries[Component.DAILY]
    body = daily_body()
    body["properties"]["periods"][0]["temperatureUnit"] = "invalid"
    # Structural storage permits this body, semantic adapter must reject it.
    assert controller.store.write_component(replace(entry, raw_body=body)).saved
    restored, _, _ = make(tmp_path, {})
    restored.restore(load_offline(restored.store))
    assert Component.DAILY not in restored.states and Component.DAILY not in restored.snapshot.entries
    assert set(restored.states) == {Component.ALERTS, Component.CONDITIONS, Component.HOURLY}
    assert restored.snapshot.issues


async def test_conditions_use_cached_station_metadata_when_metadata_fetch_fails(tmp_path):
    controller, transport, clock = await populated(tmp_path)
    await clock.sleep(86401)
    # Revalidate point; station refresh fails but the old matching station list
    # remains useful for new observation checks. Never recast the failure as a
    # successful metadata update.
    previous = controller.snapshot.entries[Component.STATIONS].provenance
    transport.routes[POINT].append(reply(POINT, POINT_BODY))
    transport.routes[STATIONS].append(reply(STATIONS, status=403))
    transport.routes[obs_url()].append(reply(obs_url(), observation_body(when=clock.now())))
    assert await controller.refresh(Component.CONDITIONS)
    assert controller.snapshot.entries[Component.STATIONS].provenance is previous
    assert "Station metadata refresh failed" in controller.states[Component.CONDITIONS].data.selection_note


async def test_full_ui_quit_cancels_station_probes_and_all_owned_tasks(tmp_path):
    controller, transport, clock = make(tmp_path, successful_routes())
    await controller.confirm(LOCATION)
    entered = asyncio.Event()
    async def hang(url):
        entered.set()
        await asyncio.Event().wait()
    transport.routes[obs_url()] = [hang]
    app = WeatherApp(config=AppConfig(), paths=controller.store.paths, clock=clock, http=controller.http)
    async with app.run_test() as pilot:
        await asyncio.wait_for(entered.wait(), 3)
        await pilot.press("ctrl+c")
    assert transport.closed and not app.tasks
    assert all(not state.in_flight for state in app.weather.states.values())


async def test_metadata_refresh_does_not_erase_another_live_component_state(tmp_path):
    controller, transport, clock = await populated(tmp_path, [alert_feature()])
    before = controller.hourly
    await clock.sleep(86401)
    transport.routes[POINT].append(reply(POINT, POINT_BODY))
    transport.routes[DAILY].append(reply(DAILY, daily_body()))
    assert await controller.refresh(Component.DAILY)
    assert controller.hourly is before
    assert not controller.hourly.loaded_from_disk
    assert controller.states[Component.ALERTS].data.alerts


async def test_redundant_station_next_link_supports_live_conditions_and_cached_restart(tmp_path):
    from urllib.parse import urlencode

    routes = successful_routes()
    body = stations_body(3)
    body["pagination"] = {"next": "https://api.weather.gov/stations?" + urlencode([
        ("id[0]", "SYN1"), ("id[1]", "SYN2"), ("id[2]", "SYN3"),
        ("cursor", "opaque-provider-cursor"),
    ])}
    routes[STATIONS] = [reply(STATIONS, body)]
    controller, transport, _ = make(tmp_path, routes)
    assert await controller.confirm(LOCATION)
    assert all(await refresh_all(controller))
    assert controller.states[Component.CONDITIONS].data.station.identifier == "SYN1"
    entry = controller.store.read_component(LOCATION, Component.STATIONS, endpoint=STATIONS).value
    assert entry is not None and entry.raw_body == body
    assert body["pagination"]["next"] not in [call[0] for call in transport.calls]

    restored, restart_transport, _ = make(tmp_path, {})
    restored.restore(load_offline(restored.store))
    assert restored.station_data == controller.station_data
    assert Component.STATIONS in restored.snapshot.entries
    state = restored.states[Component.CONDITIONS]
    assert state.loaded_from_disk and state.error is None
    assert state.data.station.identifier == "SYN1"
    assert state.provenance == controller.states[Component.CONDITIONS].provenance
    assert not restored.snapshot.issues and not restart_transport.calls
