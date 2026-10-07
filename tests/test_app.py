from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest
from textual.widgets import Input, Static

from weather_terminal.app import HelpScreen, LocationPreview, WeatherApp
from weather_terminal.config import AppConfig, RuntimePaths


@pytest.fixture(autouse=True)
def headless_standard_streams(monkeypatch, capsys):
    # Textual stores sys.__stdout__/sys.__stderr__ at App construction and uses
    # them to print headless failures. Under Windows fd capture those original
    # console handles can be invalid. Point them at pytest's live sys-capture
    # streams so diagnostics remain captured and the original exception surfaces.
    # This changes only the test harness; no Textual exceptions are suppressed.
    monkeypatch.setattr(sys, "__stdout__", sys.stdout)
    monkeypatch.setattr(sys, "__stderr__", sys.stderr)


class FakeClock:
    def __init__(self):
        self.instant = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)
        self.elapsed = 0.0

    def now(self):
        return self.instant

    def monotonic(self):
        return self.elapsed

    async def sleep(self, seconds):
        self.elapsed += seconds
        self.instant += timedelta(seconds=seconds)


def make_app(tmp_path):
    return WeatherApp(config=AppConfig(), paths=RuntimePaths(tmp_path), clock=FakeClock(), network_enabled=False)


def test_location_dialog_constructs_without_shadowing_framework_task():
    # Construction must be possible before there is a running message pump.
    # Textual owns task (read-only) and _task; our network task uses another name.
    dialog = LocationPreview()
    assert dialog.lookup_task is None
    assert "task" not in vars(dialog)
    assert "task" not in LocationPreview.__dict__


async def test_first_mount_opens_location_preview_and_typing_is_safe(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        # run_test yields after application startup. The required first screen
        # must already be mounted; no key press/repaint/sleep should trigger it.
        assert isinstance(app.screen, LocationPreview)
        assert app.screen.is_mounted
        assert len(app.screen_stack) == 2
        await pilot.pause()
        assert isinstance(app.screen, LocationPreview)
        assert app.screen.focused is app.screen.query_one("#city-input", Input)
        await pilot.press(*list("Oxford, MS qrald"))
        assert app.screen.query_one(Input).value == "Oxford, MS qrald"
        assert isinstance(app.screen, LocationPreview)
        await pilot.press("enter")
        await pilot.pause()
        content = app.screen.query_one("#lookup-status", Static).content
        assert "No request was sent" in str(content)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, LocationPreview)
        assert "No location selected" in str(app.query_one("#location-status", Static).content)
        assert not list(tmp_path.iterdir())  # UI construction itself does not write local files.


async def test_blank_submission_stays_in_dialog(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, LocationPreview)
        assert "Please enter" in str(app.screen.query_one("#lookup-status", Static).content)


async def test_modal_tab_navigation_and_cancel(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("tab", "tab", "enter")  # Input -> Submit -> Cancel.
        await pilot.pause()
        assert not isinstance(app.screen, LocationPreview)
        await pilot.press("l")
        await pilot.pause()
        assert isinstance(app.screen, LocationPreview)
        await pilot.press("escape")


async def test_help_and_unimplemented_actions(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape", "question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape", "r", "a", "d")
        await pilot.pause()
        assert app.return_code is None
        assert "never" in str(app.query_one("#alerts-panel", Static).content)


@pytest.mark.parametrize("key", ["q", "ctrl+c"])
async def test_quit_keys(tmp_path, key):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape", key)
        await pilot.pause()
        assert app.return_code == 0


async def test_ctrl_c_quits_from_location_input(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, LocationPreview)
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert app.return_code == 0


@pytest.mark.parametrize("size,wide,hint", [
    ((120, 40), True, False), ((80, 24), False, False), ((60, 18), False, True),
])
async def test_responsive_layout_and_honest_empty_panels(tmp_path, size, wide, hint):
    app = make_app(tmp_path)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.columns.has_class("wide") is wide
        assert app.resize_hint.display is hint
        for panel in ("alerts", "conditions", "hourly", "daily"):
            text = str(app.query_one(f"#{panel}-panel", Static).content)
            assert "unavailable" in text
            assert "Retrieved: never | Last checked: never" in text
        assert "NOT VERIFIED" in str(app.clock_label.content)


async def test_resize_after_mount_and_injected_clock(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert not app.columns.has_class("wide")
        await app.clock.sleep(1)
        app._update_clock()
        assert "2026-01-10 12:00:01 UTC" in str(app.clock_label.content)


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_location_dialog_reopens_with_input_focus(tmp_path, size):
    app = make_app(tmp_path)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        for _ in range(3):
            assert isinstance(app.screen, LocationPreview)
            field = app.screen.query_one("#city-input", Input)
            assert app.screen.focused is field
            assert not app.screen.query_one("#location-dialog").can_focus
            await pilot.press("q", "l", "r", "a", "d")
            assert field.value == "qlrad"
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, LocationPreview)
            await pilot.press("l")
            await pilot.pause()
        await pilot.press("escape")


async def test_headless_mount_failure_preserves_original_exception(tmp_path):
    class BrokenLocation(LocationPreview):
        def on_mount(self):
            raise RuntimeError("simulated-screen-mount-failure")

    class BrokenPreviewApp(WeatherApp):
        async def action_location(self):
            await self.push_screen(BrokenLocation())

    app = BrokenPreviewApp(config=AppConfig(), paths=RuntimePaths(tmp_path), clock=FakeClock(), network_enabled=False)
    with pytest.raises(RuntimeError, match="simulated-screen-mount-failure"):
        async with app.run_test() as pilot:
            await pilot.pause()


async def test_subclass_loads_package_styles_and_mounts_from_another_directory(tmp_path, monkeypatch):
    # Textual resolves relative CSS against the concrete subclass module. Keep
    # the asset rooted in weather_terminal even for a subclass defined here.
    class ExtendedWeatherApp(WeatherApp):
        pass

    monkeypatch.chdir(tmp_path)
    app = ExtendedWeatherApp(config=AppConfig(), paths=RuntimePaths(tmp_path), clock=FakeClock(), network_enabled=False)
    expected_css = Path(__file__).resolve().parents[1] / "weather_terminal" / "styles.tcss"
    assert app.css_path == [expected_css]
    assert expected_css.is_file()
    async with app.run_test(size=(80, 24)) as pilot:
        assert isinstance(app.screen, LocationPreview)
        assert app.screen.is_mounted
        await pilot.pause()
        assert app.screen.focused is app.screen.query_one("#city-input", Input)
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        # Styling was loaded and applied, not just a valid path recorded.
        assert app.query_one("#conditions-hourly").styles.layout.name == "vertical"
        await pilot.press("l")
        await pilot.pause()
        assert isinstance(app.screen, LocationPreview)
        assert app.screen.focused is app.screen.query_one("#city-input", Input)
