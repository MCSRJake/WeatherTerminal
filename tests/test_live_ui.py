"""Live-flow UI with injected synthetic HTTP; public APIs are never used."""
import asyncio
import sys

import pytest
from textual.widgets import Input, Select, Static

from weather_terminal.app import LocationPreview, WeatherApp
from weather_terminal.config import AppConfig, RuntimePaths
from weather_terminal.models import Component
from weather_terminal.services.http import HttpService
from tests.phase2_helpers import LOCATION
from tests.phase3_helpers import GEO, POINT, POINT_BODY, HOURLY, ScriptTransport, candidate, hourly_body, reply
from tests.test_app import FakeClock


@pytest.fixture(autouse=True)
def streams(monkeypatch, capsys):
    monkeypatch.setattr(sys, "__stdout__", sys.stdout)
    monkeypatch.setattr(sys, "__stderr__", sys.stderr)


def app_for(root, script):
    clock = FakeClock()
    transport = ScriptTransport(clock, script)
    http = HttpService(AppConfig(), clock=clock, transport=transport)
    # Preserve this module's narrow hourly integration contract; full production
    # four-component behavior is covered by test_weather_components.py.
    app = WeatherApp(config=AppConfig(), paths=RuntimePaths(root), clock=clock, http=http,
                     enabled_components=(Component.HOURLY,))
    return app, transport


async def join(task):
    assert task is not None
    await asyncio.wait_for(asyncio.shield(task), 5)


async def test_typing_sends_nothing_submit_selects_and_renders_hourly(tmp_path):
    app, transport = app_for(tmp_path, [reply(GEO, [candidate()]), reply(POINT, POINT_BODY), reply(HOURLY, hourly_body())])
    async with app.run_test(size=(80, 24)) as pilot:
        dialog = app.screen
        assert isinstance(dialog, LocationPreview)
        pump_task = dialog.task
        assert isinstance(pump_task, asyncio.Task)
        assert dialog.lookup_task is None
        await pilot.press(*list("Oxford, MS"))
        assert not transport.calls
        assert dialog.task is pump_task
        await pilot.press("enter")
        assert dialog.lookup_task is not None
        assert dialog.lookup_task is not pump_task
        await join(dialog.lookup_task)
        await join(app.refresh_task)
        await pilot.pause()
        assert not isinstance(app.screen, LocationPreview)
        assert "Oxford, MS" in str(app.location_label.content)
        hourly = str(app.query_one("#hourly-panel", Static).content)
        assert "Synthetic clear" in hourly and "Precip %" in hourly
        assert "Recently checked" in hourly
        assert "NOT VERIFIED" in str(app.clock_label.content)
        assert "unavailable" in str(app.query_one("#alerts-panel", Static).content)
        await pilot.press("q")
    assert transport.closed
    assert not app.tasks


async def test_ambiguous_search_requires_explicit_selection(tmp_path):
    raw = [candidate(), candidate("Oxford", "Ohio", "39.5", "-84.7", "Butler County")]
    app, transport = app_for(tmp_path, [reply(GEO, raw), reply(POINT, POINT_BODY), reply(HOURLY, hourly_body())])
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = app.screen
        await pilot.press(*list("Oxford"), "enter")
        await join(dialog.lookup_task)
        await pilot.pause()
        assert isinstance(app.screen, LocationPreview)
        assert len(transport.calls) == 1 and app.offline.selection is None
        assert dialog.choices.display
        # Select is an ordinary keyboard-accessible Textual control; the test
        # sets its value, then activates the same confirmation action as Enter.
        dialog.choices.value = 0
        dialog.choose_button.focus()
        await pilot.press("enter")
        await join(dialog.lookup_task)
        await join(app.refresh_task)
        assert app.offline.selection.location.key == LOCATION.key
        await pilot.press("q")


async def test_slow_search_does_not_block_resize_and_cancel_keeps_old_state(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow(url):
        entered.set()
        await release.wait()
        return reply(url, [candidate()])
    app, transport = app_for(tmp_path, [slow])
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = app.screen
        await pilot.press(*list("Oxford"), "enter")
        await asyncio.wait_for(entered.wait(), 2)
        lookup_task = dialog.lookup_task
        pump_task = dialog.task
        assert lookup_task is not None and lookup_task is not pump_task
        assert isinstance(pump_task, asyncio.Task) and not pump_task.done()
        await pilot.resize_terminal(80, 24)
        assert dialog.task is pump_task
        await pilot.press("escape")
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(lookup_task), 2)
        await pilot.pause()
        # Textual clears its message-pump reference after unmount; don't read the
        # framework task property once the screen is dismissed.
        assert lookup_task.cancelled()
        assert app.offline.selection is None and not app.columns.has_class("wide")
        assert not app.paths.state_file.exists()
        await pilot.press("q")
    assert transport.closed


async def test_saved_hourly_startup_outage_retains_normalized_cache(tmp_path):
    first, _ = app_for(tmp_path, [reply(GEO, [candidate()]), reply(POINT, POINT_BODY), reply(HOURLY, hourly_body())])
    async with first.run_test() as pilot:
        dialog = first.screen
        await pilot.press(*list("Oxford, MS"), "enter")
        await join(dialog.lookup_task)
        await join(first.refresh_task)
        original = first.weather.hourly.provenance
        await pilot.press("q")
    # A failure, not a cached read, must mark status failed without advancing
    # successful provenance. There is no startup geocoding or point request.
    second, transport = app_for(tmp_path, [reply(HOURLY, status=403)])
    async with second.run_test() as pilot:
        assert not isinstance(second.screen, LocationPreview)
        await join(second.refresh_task)
        await pilot.pause()
        text = str(second.query_one("#hourly-panel", Static).content)
        assert "Cached" in text and "Refresh failed" in text and "Synthetic clear" in text
        assert second.weather.hourly.provenance == original
        assert len(transport.calls) == 1 and transport.calls[0][0] == HOURLY
        await pilot.press("q")


async def test_cancel_from_candidate_list_does_not_commit(tmp_path):
    app, transport = app_for(tmp_path, [reply(GEO, [candidate(), candidate("Oxford", "Ohio", "39.5", "-84.7", "Butler County")])])
    async with app.run_test() as pilot:
        dialog = app.screen
        await pilot.press(*list("Oxford"), "enter")
        await join(dialog.lookup_task)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, LocationPreview)
        assert app.offline.selection is None
        assert not app.paths.state_file.exists()
        assert len(transport.calls) == 1
        await pilot.press("q")


async def test_quit_during_pending_search_closes_transport(tmp_path):
    entered = asyncio.Event()
    async def hang(url):
        entered.set()
        await asyncio.Event().wait()
    app, transport = app_for(tmp_path, [hang])
    async with app.run_test() as pilot:
        await pilot.press(*list("Oxford"), "enter")
        await asyncio.wait_for(entered.wait(), 2)
        await pilot.press("ctrl+c")
    assert transport.closed and not app.tasks
    assert not app.paths.state_file.exists()
