from dataclasses import replace
from datetime import timedelta
import socket

import httpx
import pytest
from textual.widgets import Static

from weather_terminal.app import LocationPreview, WeatherApp
from weather_terminal.config import AppConfig, RuntimePaths
from weather_terminal.models import Component, ErrorKind, Location, OperationalError
from weather_terminal.offline import load_offline, restore_component
from weather_terminal.storage import CacheStore, SavedSelection
from tests.phase2_helpers import DISCOVERY, LOCATION, NOW, SELECTION, entry, seed_cache
from tests.test_app import FakeClock


@pytest.fixture(autouse=True)
def headless_standard_streams(monkeypatch, capsys):
    import sys
    monkeypatch.setattr(sys, "__stdout__", sys.stdout)
    monkeypatch.setattr(sys, "__stderr__", sys.stderr)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network access is forbidden in offline tests")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    # Do not replace socket.connect globally: Windows asyncio may need a
    # loopback socket pair to create its event loop, independently of HTTP.


def test_normalized_restore_keeps_original_success_and_resets_session_state(tmp_path):
    seed_cache(tmp_path)
    original = CacheStore(RuntimePaths(tmp_path)).read_component(LOCATION, Component.ALERTS).value
    state = restore_component(original, generation=12, normalize=lambda body: body["features"])
    assert state.loaded_from_disk and not state.in_flight
    assert state.last_attempted_at is None and state.error is None
    assert state.provenance is original.provenance
    assert state.belongs_to(LOCATION.key, 12)
    state.begin_attempt(NOW + timedelta(minutes=1))
    state.fail(OperationalError(ErrorKind.NETWORK, "simulated outage"))
    assert state.raw_body is original.raw_body and state.provenance is original.provenance
    # A failure does not need a disk write; a restarted store retains the original.
    assert CacheStore(RuntimePaths(tmp_path)).read_component(LOCATION, Component.ALERTS).value == original


def test_normalization_failure_never_creates_usable_state():
    with pytest.raises(ValueError, match="normalized"):
        restore_component(entry(), generation=1, normalize=lambda body: None)


def test_zero_and_null_survive_storage_and_restore(tmp_path):
    store = seed_cache(tmp_path)
    original = store.read_component(LOCATION, Component.CONDITIONS).value
    restored = restore_component(original, generation=1, normalize=lambda body: body["properties"])
    assert restored.data["temperature"]["value"] == 0
    assert restored.data["windSpeed"]["value"] == 0
    assert restored.data["windGust"]["value"] is None


async def test_offline_restart_loads_saved_point_dates_and_original_times(tmp_path):
    store = seed_cache(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    for _ in range(2):
        app = WeatherApp(config=AppConfig(), paths=RuntimePaths(tmp_path), clock=FakeClock(), network_enabled=False)
        async with app.run_test(size=(80, 24)) as pilot:
            assert not isinstance(app.screen, LocationPreview)
            assert app.offline.selection == SELECTION
            assert "Oxford, MS" in str(app.query_one("#location-status", Static).content)
            assert "CST -0600" in str(app.clock_label.content)
            for component in (Component.ALERTS, Component.CONDITIONS, Component.HOURLY, Component.DAILY):
                text = str(app.query_one(f"#{component.value}-panel", Static).content)
                assert "cached source preview" in text
                assert "Retrieved: 2026-01-10 06:00:00 CST -0600" in text
                assert "No live check this session" in text
                assert app.offline.entries[component].provenance.retrieved_at == NOW
            assert "Expired" in str(app.query_one("#alerts-panel", Static).content)
            assert "DO NOT DISPLAY AS ACTUAL" not in str(app.query_one("#alerts-panel", Static).content)
            await pilot.press("l")
            await pilot.pause()
            assert isinstance(app.screen, LocationPreview)
            await pilot.press("escape", "q")
            await pilot.pause()
            assert app.return_code == 0
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}


async def test_clock_recomputes_coverage_and_expiry_without_rereading_cache(tmp_path, monkeypatch):
    store = seed_cache(tmp_path)
    app = WeatherApp(config=AppConfig(), paths=RuntimePaths(tmp_path), clock=FakeClock(), storage=store, network_enabled=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        original = app.offline.entries[Component.HOURLY].provenance
        def forbid_reads(*args, **kwargs):
            raise AssertionError("Display tick must not read disk")
        monkeypatch.setattr(store, "read_selection", forbid_reads)
        monkeypatch.setattr(store, "read_component", forbid_reads)
        app.clock.instant += timedelta(days=2)
        app._update_clock()
        text = str(app.query_one("#hourly-panel", Static).content)
        assert "Forecast coverage expired" in text and "2026-01-10" in text
        assert app.offline.entries[Component.HOURLY].provenance is original
        assert "2026-01-12" in str(app.clock_label.content)
        await pilot.press("q")


async def test_corrupt_component_keeps_other_panels_and_visible_warning(tmp_path):
    store = seed_cache(tmp_path)
    hourly = entry()
    store.component_path(LOCATION, Component.HOURLY, hourly.endpoint).write_text("{", encoding="utf-8")
    app = WeatherApp(config=AppConfig(), paths=RuntimePaths(tmp_path), clock=FakeClock(), network_enabled=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.storage_notice.display
        assert "Local storage warning" in str(app.storage_notice.content)
        assert "unavailable" in str(app.query_one("#hourly-panel", Static).content)
        assert "cached source preview" in str(app.query_one("#daily-panel", Static).content)
        assert "cached source preview" in str(app.query_one("#conditions-panel", Static).content)
        await pilot.press("q")


async def test_corrupt_selection_opens_usable_dialog_without_other_city_weather(tmp_path, caplog):
    store = seed_cache(tmp_path)
    store.paths.state_file.write_text("{", encoding="utf-8")
    app = WeatherApp(config=AppConfig(), paths=RuntimePaths(tmp_path), clock=FakeClock(), network_enabled=False)
    async with app.run_test() as pilot:
        assert isinstance(app.screen, LocationPreview)
        await pilot.press("escape")
        await pilot.pause()
        assert "No location selected" in str(app.location_label.content)
        assert not app.offline.entries
        assert app.storage_notice.display
        # This warning is expected: this test deliberately wrote invalid JSON
        # in its temporary state file. It is not damage to the user's runtime.
        assert any(
            record.name == "weather_terminal"
            and "An unreadable or corrupt local entry was ignored." in record.getMessage()
            for record in caplog.records
        )
        await pilot.press("q")


async def test_selection_without_cache_shows_honest_missing_state(tmp_path):
    store = CacheStore(RuntimePaths(tmp_path))
    assert store.write_selection(SELECTION).saved
    app = WeatherApp(config=AppConfig(), paths=RuntimePaths(tmp_path), clock=FakeClock(), network_enabled=False)
    async with app.run_test() as pilot:
        assert not isinstance(app.screen, LocationPreview)
        assert "Oxford, MS" in str(app.location_label.content)
        assert "never" in str(app.query_one("#alerts-panel", Static).content)
        assert "NOT VERIFIED" in str(app.clock_label.content)
        assert not app.storage_notice.display
        await pilot.press("q")


def test_selection_change_loads_only_that_locations_entries(tmp_path):
    store = seed_cache(tmp_path)
    other = Location("Memphis", "TN", 35.15, -90.05, timezone_name="America/Chicago")
    assert store.write_selection(SavedSelection(other)).saved
    loaded = load_offline(store)
    assert loaded.selection.location == other
    assert not loaded.entries
    assert store.read_component(LOCATION, Component.HOURLY).value is not None


def test_saved_endpoint_mismatch_does_not_use_superseded_forecast(tmp_path):
    store = seed_cache(tmp_path)
    changed = replace(DISCOVERY, hourly_url="https://api.weather.gov/gridpoints/MEG/99,99/forecast/hourly")
    assert store.write_selection(SavedSelection(LOCATION, changed)).saved
    loaded = load_offline(store)
    assert Component.HOURLY not in loaded.entries
    assert Component.DAILY in loaded.entries


def test_cached_empty_alerts_remain_unverified_on_restart(tmp_path):
    store = seed_cache(tmp_path)
    assert store.write_component(entry(Component.ALERTS, body={"features": []})).saved
    loaded = load_offline(CacheStore(RuntimePaths(tmp_path)))
    assert loaded.entries[Component.ALERTS].raw_body == {"features": []}
    assert loaded.entries[Component.ALERTS].provenance.retrieved_at == NOW
