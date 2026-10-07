import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from weather_terminal.models import Component, Location
from weather_terminal.offline import load_offline
from weather_terminal.services.http import ServiceFailure
from weather_terminal.services.nws import normalize_point, point_url
from weather_terminal.storage import CacheStore, WriteResult
from tests.phase2_helpers import LOCATION
from tests.phase3_helpers import GEO, POINT, POINT_BODY, HOURLY, hourly_body, reply, slice_for


@pytest.mark.parametrize("field,value", [("forecastHourly", None), ("forecast", None),
    ("forecastHourly", "https://evil.example/data"), ("forecastHourly", "https://api.weather.gov/marine")])
def test_unsupported_or_unsafe_point_never_becomes_selection(field, value):
    body = deepcopy(POINT_BODY)
    body["properties"][field] = value
    with pytest.raises(ServiceFailure):
        normalize_point(body, POINT, GEO)


def test_invalid_zone_is_explicit_utc_fallback():
    body = deepcopy(POINT_BODY)
    body["properties"]["timeZone"] = "Not/AZone"
    assert normalize_point(body, POINT, GEO).timezone_name is None


async def test_live_slice_cache_restart_failure_and_original_provenance(tmp_path):
    controller, transport, clock = slice_for(tmp_path, [reply(POINT, POINT_BODY), reply(HOURLY, hourly_body()),
                                                        reply(HOURLY, status=403)])
    assert await controller.confirm(LOCATION)
    assert await controller.refresh()
    before = controller.hourly.provenance
    original = controller.store.read_component(LOCATION, Component.HOURLY).value
    snapshot = load_offline(CacheStore(controller.store.paths))
    restored, _, _ = slice_for(tmp_path, [])
    restored.restore(snapshot)
    assert restored.hourly.loaded_from_disk
    assert restored.hourly.provenance == before
    await clock.sleep(60)
    assert not await controller.refresh()
    assert controller.hourly.error is not None
    assert controller.hourly.provenance is before
    assert controller.store.read_component(LOCATION, Component.HOURLY).value == original
    assert controller.hourly.data.periods[0].start == before.retrieved_at - timedelta(seconds=1)
    # The first NWS point request and paced hourly request advance the fake wall
    # clock by a second; source forecast validity remains at its original instant.


async def test_supported_selection_commits_even_when_hourly_is_unavailable(tmp_path):
    controller, _, _ = slice_for(tmp_path, [reply(POINT, POINT_BODY), reply(HOURLY, status=403)])
    assert await controller.confirm(LOCATION)
    assert not await controller.refresh()
    assert controller.snapshot.selection.location.key == LOCATION.key
    assert controller.hourly.data is None and controller.hourly.error


async def test_unsupported_new_point_retains_existing_label_and_data(tmp_path):
    other = Location("Memphis", "TN", 35.15, -90.05)
    controller, transport, _ = slice_for(tmp_path, [reply(POINT, POINT_BODY), reply(HOURLY, hourly_body()),
                                                   reply(point_url(other), status=404)])
    await controller.confirm(LOCATION)
    await controller.refresh()
    original = controller.snapshot.selection
    body = controller.hourly.raw_body
    state_bytes = controller.store.paths.state_file.read_bytes()
    with pytest.raises(ServiceFailure):
        await controller.confirm(other)
    assert controller.snapshot.selection == original and controller.hourly.raw_body is body
    assert controller.store.paths.state_file.read_bytes() == state_bytes


async def test_cancel_pending_point_cannot_commit_or_write(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow(url):
        entered.set()
        await release.wait()
        return reply(url, POINT_BODY)
    controller, _, _ = slice_for(tmp_path, [slow])
    task = asyncio.create_task(controller.confirm(LOCATION))
    await asyncio.wait_for(entered.wait(), 2)
    controller.invalidate_pending()
    release.set()
    assert not await task
    assert controller.snapshot.selection is None
    assert not controller.store.paths.state_file.exists()
    assert not list(tmp_path.rglob("*.json"))


async def test_late_old_hourly_cannot_populate_new_location_or_cache(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow(url):
        entered.set()
        await release.wait()
        return reply(url, hourly_body())
    other = Location("Memphis", "TN", 35.15, -90.05)
    controller, _, _ = slice_for(tmp_path, [reply(POINT, POINT_BODY), slow])
    await controller.confirm(LOCATION)
    task = asyncio.create_task(controller.refresh())
    await asyncio.wait_for(entered.wait(), 2)
    # Resolve/commit a new selection without using the held NWS HTTP lock. In
    # production cancellation frees it; this injects a late response deliberately.
    from weather_terminal.models import Provenance
    from weather_terminal.storage import CachedPayload
    discovery = normalize_point(POINT_BODY, point_url(other), GEO)
    async def resolved(location):
        return discovery, CachedPayload(location, Component.POINT, point_url(other), POINT_BODY,
                                        Provenance(controller.http.clock.now(), controller.http.clock.now()))
    controller._point = resolved
    assert await controller.confirm(other)
    release.set()
    assert not await task
    assert controller.snapshot.selection.location.key == other.key
    assert controller.hourly is None
    assert controller.store.read_component(LOCATION, Component.HOURLY).value is None
    assert controller.store.read_component(other, Component.HOURLY).value is None


async def test_repeated_refresh_coalesces(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow(url):
        entered.set()
        await release.wait()
        return reply(url, hourly_body())
    controller, transport, _ = slice_for(tmp_path, [reply(POINT, POINT_BODY), slow])
    await controller.confirm(LOCATION)
    first = asyncio.create_task(controller.refresh())
    await asyncio.wait_for(entered.wait(), 2)
    assert not await controller.refresh()
    assert len(transport.calls) == 2
    release.set()
    assert await first


async def test_malformed_hourly_response_never_replaces_good_disk_or_memory(tmp_path):
    invalid = hourly_body()
    invalid["properties"]["periods"][0]["temperatureUnit"] = "bogus"
    controller, _, _ = slice_for(tmp_path, [reply(POINT, POINT_BODY), reply(HOURLY, hourly_body()), reply(HOURLY, invalid)])
    await controller.confirm(LOCATION)
    await controller.refresh()
    previous = controller.hourly.provenance
    bytes_before = controller.store.component_path(LOCATION, Component.HOURLY, HOURLY).read_bytes()
    assert not await controller.refresh()
    assert controller.hourly.provenance is previous
    assert controller.store.component_path(LOCATION, Component.HOURLY, HOURLY).read_bytes() == bytes_before


async def test_new_good_memory_retained_when_disk_write_fails(tmp_path, monkeypatch):
    controller, _, _ = slice_for(tmp_path, [reply(POINT, POINT_BODY), reply(HOURLY, hourly_body())])
    await controller.confirm(LOCATION)
    monkeypatch.setattr(controller.store, "write_component", lambda entry: WriteResult(False, "Synthetic disk failure"))
    assert await controller.refresh()
    assert controller.hourly.data and controller.snapshot.issues


async def test_fresh_point_cache_reused_without_another_point_request(tmp_path):
    controller, transport, _ = slice_for(tmp_path, [reply(POINT, POINT_BODY)])
    await controller.confirm(LOCATION)
    await controller.confirm(LOCATION)
    assert len(transport.calls) == 1


def test_saved_metadata_for_different_coordinates_is_rejected():
    from weather_terminal.services.nws import validate_point_location
    discovery = normalize_point(POINT_BODY, POINT, GEO)
    validate_point_location(discovery, LOCATION)
    with pytest.raises(ServiceFailure):
        validate_point_location(discovery, Location("Memphis", "TN", 35.15, -90.05))


async def test_cancellation_after_commit_acceptance_finishes_disk_and_memory_together(tmp_path, monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()
    controller, _, _ = slice_for(tmp_path, [reply(POINT, POINT_BODY)])
    original = controller.store.write_selection
    def held_write(selection):
        entered.set()
        assert release.wait(5)
        return original(selection)
    monkeypatch.setattr(controller.store, "write_selection", held_write)
    task = asyncio.create_task(controller.confirm(LOCATION))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)  # Deliver cancellation, not an arbitrary timing wait.
        task.cancel()  # Escape followed by exit must not abandon an atomic write.
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert controller.snapshot.selection.location.key == LOCATION.key
    assert controller.store.read_selection().value == controller.snapshot.selection
    assert controller.store.read_component(LOCATION, Component.POINT).value is not None
