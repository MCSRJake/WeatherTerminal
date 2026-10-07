"""Phase 4 independent weather components with plain, provenance-rich panels."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Select, Static

from weather_terminal.clock import Clock, SystemClock
from weather_terminal.config import AppConfig, RuntimePaths
from weather_terminal.models import Component, as_utc
from weather_terminal.freshness import format_time
from weather_terminal.offline import DISPLAY_COMPONENTS, OfflineSnapshot, load_offline, render_cached_component
from weather_terminal.storage import CacheStore
from weather_terminal.hourly import render_hourly
from weather_terminal.conditions import render_conditions
from weather_terminal.daily import render_daily, select_days
from weather_terminal.alerts import render_alerts, alert_status, alert_details
from weather_terminal.services.http import HttpService, ServiceFailure
from weather_terminal.slice import WeatherSlice, WEATHER_COMPONENTS
from weather_terminal.text import plain_text


class LocationPreview(ModalScreen[None]):
    # Let Textual focus the input when composition/activation makes it available.
    AUTO_FOCUS = "#city-input"
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+c", "app.quit", "Quit", priority=True),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="location-dialog", can_focus=False):
            yield Static("Choose a U.S. town or city", classes="dialog-title", markup=False)
            yield Static(
                "Submit a city, optionally with its state (Oxford, MS). Uncached searches "
                "go to the configured external geocoder. No autocomplete; do not enter confidential information.", markup=False,
            )
            yield Input(placeholder="Oxford, MS", id="city-input")
            yield Static("", id="lookup-status", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("Search", id="submit-city", variant="primary")
                yield Button("Cancel", id="cancel-city")
            self.choices = Select([], prompt="Select city and state", id="city-choices")
            self.choices.display = False
            yield self.choices
            self.choose_button = Button("Use selected city", id="choose-city", variant="primary")
            self.choose_button.display = False
            yield self.choose_button
            yield Static(
                "Location lookup: © OpenStreetMap contributors\n"
                "https://www.openstreetmap.org/copyright", classes="muted", markup=False,
            )

    def __init__(self) -> None:
        super().__init__()
        self.busy = False
        self.active = True
        self.candidates = ()
        # Textual's task property is its read-only message-pump task. Keep our
        # cancellable provider operation separate; never replace task or _task.
        self.lookup_task: asyncio.Task | None = None

    def _status(self, message: str) -> None:
        self.query_one("#lookup-status", Static).update(plain_text(message))

    def _submit(self) -> None:
        query = self.query_one(Input).value.strip()
        if not query:
            self._status("Please enter a town or city.")
        elif not self.app.network_enabled:
            self._status("Network disabled for this isolated test. No request was sent; no location was changed.")
        elif not self.busy:
            self.busy = True
            self.choices.display = self.choose_button.display = False
            self.query_one("#submit-city", Button).disabled = True
            self.lookup_task = self.app.start_task(self._search(query))

    async def _search(self, query: str) -> None:
        self._status("Searching submitted city… Current dashboard is retained until supported selection succeeds.")
        try:
            result = await self.app.weather.geocoder.search(query)
            if not self.active or not self.is_mounted:
                return
            self.candidates = result.candidates
            if result.issues:
                self.app.notify("Local search cache issue; results remain usable.", severity="warning")
            if not result.candidates:
                self._status("No supported U.S. settlement found. Try City, State. Territories are not supported.")
            elif len(result.candidates) == 1:
                await self._confirm(result.candidates[0])
            else:
                self.choices.set_options([(Text(plain_text(f"{place.label}" + (f" — {place.county}" if place.county else ""))), i)
                                          for i, place in enumerate(result.candidates)])
                self.choices.display = self.choose_button.display = True
                self._status(f"{len(result.candidates)} matching settlements{' (cached search)' if result.cached else ''}. Select a city/state, then Use selected city.")
                self.choices.focus()
        except ServiceFailure as exc:
            if self.active and self.is_mounted:
                self._status(exc.error.message)
        finally:
            self.busy = False
            if self.active and self.is_mounted:
                self.query_one("#submit-city", Button).disabled = False

    async def _confirm(self, location) -> None:
        self.app.weather.invalidate_pending()
        ticket = self.app.weather.intent
        self._status(f"Checking NWS support for {location.label}…")
        if await self.app.weather.confirm(location, ticket=ticket):
            if self.active and self.is_mounted:
                self.active = False
                self.dismiss(None)
            self.app.start_refresh(replace_running=True)

    async def _choose(self) -> None:
        try:
            value = self.choices.value
            if value is Select.NULL:
                self._status("Select a city and state first.")
            else:
                await self._confirm(self.candidates[int(value)])
        except ServiceFailure as exc:
            if self.active and self.is_mounted:
                self._status(exc.error.message)
        finally:
            self.busy = False

    def on_unmount(self) -> None:
        self.active = False

    def _cancel(self) -> None:
        self.active = False
        if self.app.weather is not None:
            self.app.weather.invalidate_pending()
        if self.lookup_task is not None:
            self.lookup_task.cancel()
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "cancel-city":
            self._cancel()
        elif event.button.id == "submit-city":
            self._submit()
        elif event.button.id == "choose-city" and not self.busy:
            self.busy = True
            self.lookup_task = self.app.start_task(self._choose())

    def action_cancel(self) -> None:
        self._cancel()


class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close", show=False),
        Binding("ctrl+c", "app.quit", "Quit", priority=True),
    ]

    def __init__(self, paths: RuntimePaths) -> None:
        super().__init__()
        self.paths = paths

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-dialog"):
            yield Static("Weather Terminal — four weather components", classes="dialog-title", markup=False)
            yield Static(plain_text(
                "IMPLEMENTED\n"
                "Submit-only U.S. city search, ambiguous city/state selection, NWS point discovery, "
                "station observations, twelve-hour and seven-day forecasts, official point alerts, manual/periodic refresh, persistent source storage, "
                "cache-first startup, original provenance, settings, logs, and single-instance guard.\n\n"
                "NOT IMPLEMENTED\n"
                "Polished selectable tables, per-alert/day selection, location details, "
                "conditional HTTP revalidation, HTTP freshness scheduling, and full retry/rediscovery coordination.\n\n"
                "CONTROLS\n"
                "L: city search   ?: help   Q / Ctrl+C: quit\n"
                "R: refresh all weather components (coalesced and provider-paced)\n"
                "A / D: full official alerts / daily forecast text (scrollable read-only snapshots)\n"
                "Tab / Shift+Tab: focus controls   Escape: close a dialog\n"
                "Location-input letters are ordinary text, not dashboard shortcuts.\n\n"
                "DATA DEFINITIONS\n"
                "Retrieval time is not observation time. Precip % is probability, not amount. "
                "The daily low is an overnight forecast. Point forecasts and alerts do not "
                "necessarily cover every part of a named municipality.\n\n"
                "SAFETY\n"
                "This is not a guaranteed emergency-notification system. Use official warning "
                "channels. Disk-restored alerts and any failed alert check are immediately unverified.\n\n"
                f"LOCAL FILES\nConfiguration: {self.paths.config_file}\n"
                f"Remembered location: {self.paths.state_file}\n"
                f"Source cache: {self.paths.cache_dir}\nLog: {self.paths.log_file}\n\n"
                "CACHED DATA\n"
                "Reading disk never updates retrieval/check/source times. Cached alerts are "
                "always unverified; expired entries stay expired. Close the app before resetting "
                "local files. Storage problems do not mean a network outage.\n\n"
                "SOURCES AND PRIVACY\n"
                "Uncached submitted city text goes to the configured geocoder; NWS receives coordinates. "
                "No periodic geocoding, autocomplete, telemetry, or silent provider fallback.\n"
                "Nominatim public service: identifying User-Agent, caching, no confidential information, "
                "at most one request per second. Policy: https://operations.osmfoundation.org/policies/nominatim/\n"
                "NWS: https://www.weather.gov/documentation/services-web-api\n"
                "Location lookup: © OpenStreetMap contributors\n"
                "https://www.openstreetmap.org/copyright\n"
            ), markup=False)
            yield Button("Close", id="close-help", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class SourceDetails(ModalScreen[None]):
    """Read-only snapshot of complete source text; per-row selectors come in Phase 5."""
    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close", show=False),
                Binding("ctrl+c", "app.quit", "Quit", priority=True)]

    def __init__(self, title: str, text: str) -> None:
        super().__init__()
        self.detail_title, self.detail_text = title, text

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="source-details"):
            yield Static(plain_text(self.detail_title), classes="dialog-title", markup=False)
            yield Static(plain_text(self.detail_text), id="detail-text", markup=False)
            yield Button("Close", id="close-details", variant="primary")

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)


class WeatherApp(App[None]):
    TITLE = "Weather Terminal"
    SUB_TITLE = "NWS observations, forecasts, and point alerts"
    # A relative CSS_PATH is resolved against the concrete App subclass's module.
    # Anchor this asset to our module so test/extension subclasses cannot redirect
    # it to tests/styles.tcss, and launching from another directory also works.
    CSS_PATH = Path(__file__).resolve().with_name("styles.tcss")
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("l", "location", "Location"),
        Binding("r", "refresh_weather", "Refresh"),
        Binding("a", "alert_details", "Alerts"),
        Binding("d", "day_details", "Day details"),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    def __init__(
        self, *, config: AppConfig, paths: RuntimePaths, clock: Clock | None = None,
        storage: CacheStore | None = None, network_enabled: bool = True,
        http: HttpService | None = None, enabled_components: tuple[Component, ...] = WEATHER_COMPONENTS,
    ) -> None:
        super().__init__()
        self.config = config
        self.paths = paths
        self.clock = clock if clock is not None else SystemClock()
        self.storage = storage if storage is not None else CacheStore(paths)
        self.offline = OfflineSnapshot()
        self.panel_refs: dict[Component, Static] = {}
        self.network_enabled = network_enabled
        self.weather: WeatherSlice | None = None
        self.injected_http = http
        # Isolated slice tests may restrict components; production always uses all four.
        self.enabled_components = tuple(Component(c) for c in enabled_components)
        self.component_tasks: dict[Component, asyncio.Task] = {}
        self.tasks: set[asyncio.Task] = set()
        self.refresh_task: asyncio.Task | None = None
        self.closing = False

    def _panel(self, title: str, explanation: str, identifier: str) -> Static:
        panel = Static(
            f"{title}\n{explanation}\nRetrieved: never | Last checked: never",
            id=identifier, classes="weather-panel", markup=False,
        )
        self.panel_refs[Component(identifier.removesuffix("-panel"))] = panel
        return panel

    def compose(self) -> ComposeResult:
        yield Header()
        self.clock_label = Static("", id="clock", markup=False)
        yield self.clock_label
        self.resize_hint = Static("", id="resize-hint", markup=False)
        yield self.resize_hint
        with VerticalScroll(id="dashboard"):
            self.location_label = Static(
                "No location selected. Press L to search a U.S. city.",
                id="location-status", markup=False,
            )
            yield self.location_label
            self.storage_notice = Static("", id="storage-notice", markup=False)
            yield self.storage_notice
            yield self._panel(
                "ALERTS — unavailable", "No current alert status has been verified.", "alerts-panel"
            )
            self.columns = Horizontal(id="conditions-hourly")
            with self.columns:
                yield self._panel(
                    "CURRENT CONDITIONS — unavailable", "No usable station observation retrieved yet.",
                    "conditions-panel",
                )
                yield self._panel(
                    "NEXT 12 HOURS — unavailable", "No valid hourly forecast retrieved yet.",
                    "hourly-panel",
                )
            yield self._panel(
                "NEXT 7 DAYS — unavailable", "No valid daily forecast retrieved yet.", "daily-panel"
            )
            yield Static(
                "NWS weather data | Location lookup: © OpenStreetMap contributors. "
                "Point-based weather and alerts; not a guaranteed emergency-notification system.",
                classes="muted", markup=False,
            )
        yield Footer()

    async def on_mount(self) -> None:
        # Disk work is bounded and run off the event loop. Construction/import
        # remains side-effect free; startup reads never create or repair files.
        self.offline = await asyncio.to_thread(load_offline, self.storage)
        if self.network_enabled:
            http = self.injected_http or HttpService(self.config, clock=self.clock)
            self.weather = WeatherSlice(self.config, self.storage, http, changed=self._weather_changed)
            self.weather.restore(self.offline)
        self._render_offline()
        self._update_clock()
        self._update_layout(self.size.width, self.size.height)
        self.set_interval(1, self._update_clock)
        # Startup requires the modal, not merely a callback queued for a future
        # repaint. Complete its mount before Textual signals startup readiness.
        # Awaiting push_screen waits for mounting, NOT for user dismissal.
        if self.offline.selection is None:
            await self.action_location()
        elif self.network_enabled:
            self.start_refresh()
        if self.network_enabled:
            for component in self.enabled_components:
                seconds = getattr(self.config.refresh, f"{component.value}_seconds")
                self.set_interval(seconds, lambda c=component: self.start_component(c))

    def _weather_changed(self) -> None:
        if self.weather is not None and not self.closing:
            self.offline = self.weather.snapshot
            self._update_clock()  # Includes immediate alert verification/failure status.

    def start_task(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        def finished(completed):
            self.tasks.discard(completed)
            if not completed.cancelled():
                error = completed.exception()
                if error is not None:
                    logging.getLogger("weather_terminal").error(
                        "Unexpected background operation failure", exc_info=(type(error), error, error.__traceback__),
                    )
                    if not self.closing:
                        self.notify("Unexpected operation failure; see the local log. Last good data is retained.", severity="error")
        task.add_done_callback(finished)
        return task

    def start_component(self, component: Component) -> asyncio.Task | None:
        if self.weather is None or self.offline.selection is None or self.closing:
            return None
        previous = self.component_tasks.get(component)
        if previous is not None and not previous.done():
            return previous
        task = self.start_task(self.weather.refresh(component))
        self.component_tasks[component] = task
        return task

    def start_refresh(self, *, replace_running: bool = False) -> None:
        if self.weather is None or self.offline.selection is None or self.closing:
            return
        previous = self.refresh_task
        if previous is not None and not previous.done() and not replace_running:
            return
        async def refresh():
            if replace_running:
                obsolete = tuple(task for task in self.component_tasks.values() if not task.done())
                for task in obsolete:
                    task.cancel()
                if obsolete:
                    await asyncio.gather(*obsolete, return_exceptions=True)
                if previous is not None and not previous.done():
                    previous.cancel()
                    await asyncio.gather(previous, return_exceptions=True)
            requests = [self.start_component(component) for component in self.enabled_components]
            await asyncio.gather(*(task for task in requests if task is not None))
        self.refresh_task = self.start_task(refresh())

    async def on_unmount(self) -> None:
        self.closing = True
        if self.weather is not None:
            self.weather.closed = True
            self.weather.invalidate_pending()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.weather is not None:
            await self.weather.aclose()

    def _render_offline(self) -> None:
        selection = self.offline.selection
        if selection is not None:
            self.location_label.update(plain_text(
                f"{selection.location.label} | Saved forecast point: "
                f"{selection.location.latitude}, {selection.location.longitude}\n"
                "Press L to change location. Forecasts cover this representative point, not the entire municipality."
            ))
        self.storage_notice.display = bool(self.offline.issues)
        self.storage_notice.update(
            "Local storage warning: one or more entries could not be loaded; other data is retained. "
            "See the log/README for recovery; usable in-memory data is retained."
            if self.offline.issues else ""
        )
        now = as_utc(self.clock.now())
        for component in DISPLAY_COMPONENTS:
            state = self.weather.states.get(component) if self.weather is not None else None
            if state is not None and component == Component.ALERTS:
                self.panel_refs[component].update(render_alerts(state, now, self.offline.timezone_name, self.config.freshness))
                continue
            if state is not None and state.data is not None and state.provenance is not None:
                renderer = {Component.HOURLY: render_hourly, Component.CONDITIONS: render_conditions, Component.DAILY: render_daily}[component]
                self.panel_refs[component].update(renderer(
                    state.data, state.provenance, now, self.offline.timezone_name, self.config.freshness,
                    cached=state.loaded_from_disk, attempted=state.last_attempted_at,
                    loading=state.in_flight, error=state.error.message if state.error else None,
                ))
                continue
            entry = self.offline.entries.get(component)
            if entry is not None:
                self.panel_refs[component].update(render_cached_component(
                    entry, now, self.config.freshness, self.offline.timezone_name,
                ))
            else:
                message = {
                    Component.ALERTS: "No current alert status has been verified.",
                    Component.CONDITIONS: "No usable station observation retrieved yet.",
                    Component.DAILY: "No valid daily forecast retrieved yet.",
                    Component.HOURLY: "No valid hourly forecast retrieved yet.",
                }[component]
                if state is not None:
                    message += " Loading…" if state.in_flight else ""
                    if state.error:
                        message += f" Refresh failed: {state.error.message}"
                title = {Component.ALERTS: "ALERTS", Component.CONDITIONS: "CURRENT CONDITIONS",
                         Component.HOURLY: "NEXT 12 HOURS", Component.DAILY: "NEXT 7 DAYS"}[component]
                attempt = format_time(state.last_attempted_at, self.offline.timezone_name) if state is not None and state.last_attempted_at else 'never this session'
                self.panel_refs[component].update(plain_text(
                    f"{title} — unavailable\n{message}\nRetrieved: never | Last checked: never\nLast attempted: {attempt}"
                ))

    def _update_clock(self) -> None:
        now = as_utc(self.clock.now())
        # No OS-local timezone assumption; use saved point timezone or explicit UTC.
        stamp = (f"{now:%Y-%m-%d %H:%M:%S} UTC" if self.offline.selection is None
                 else format_time(now, self.offline.timezone_name))
        state = self.weather.states.get(Component.ALERTS) if self.weather is not None else None
        self.clock_label.update(f"{stamp} | Alert status: {alert_status(state, now, self.config.freshness)}")
        self._render_offline()  # Age/coverage/expiry changes without disk reads or HTTP.

    def _update_layout(self, width: int, height: int) -> None:
        self.columns.set_class(width >= 110, "wide")
        self.resize_hint.update(
            "For easier navigation, enlarge the terminal to at least 80 × 24."
            if width < 80 or height < 24 else ""
        )
        self.resize_hint.display = width < 80 or height < 24

    def on_resize(self, event: events.Resize) -> None:
        if hasattr(self, "columns"):
            self._update_layout(event.size.width, event.size.height)

    async def action_location(self) -> None:
        await self.push_screen(LocationPreview())

    def action_help(self) -> None:
        self.push_screen(HelpScreen(self.paths))

    def action_refresh_weather(self) -> None:
        if self.network_enabled:
            self.start_refresh()
        else:
            self.notify("Network disabled for this isolated test. No request was sent.")

    def action_alert_details(self) -> None:
        state = self.weather.states.get(Component.ALERTS) if self.weather is not None else None
        if state is None or state.data is None:
            self.notify("No usable alert set available; current alert status is not verified.")
            return
        now = self.clock.now()
        text = render_alerts(state, now, self.offline.timezone_name, self.config.freshness)
        text += "\n\nREAD-ONLY SNAPSHOT; dashboard refresh does not update an open details view.\n\n"
        text += "\n\n".join(alert_details(a, now, self.offline.timezone_name) for a in state.data.alerts)
        self.push_screen(SourceDetails("Official alert text — snapshot", text))

    def action_day_details(self) -> None:
        state = self.weather.states.get(Component.DAILY) if self.weather is not None else None
        if state is None or state.data is None:
            self.notify("No usable daily forecast available.")
            return
        text = render_daily(state.data, state.provenance, self.clock.now(), self.offline.timezone_name, self.config.freshness,
                            cached=state.loaded_from_disk, error=state.error.message if state.error else None)
        days, _ = select_days(state.data, self.clock.now(), self.offline.timezone_name)
        text += "\n\nREAD-ONLY SNAPSHOT; close and reopen for newer data.\n"
        for day in days:
            text += f"\n{day.date:%A %Y-%m-%d}\n"
            for label, period in (("Day", day.daytime), ("Overnight", day.nighttime)):
                text += f"{label}: {period.detailed if period else 'Unavailable'}\n"
        self.push_screen(SourceDetails("Full NWS daily forecast — snapshot", text))
