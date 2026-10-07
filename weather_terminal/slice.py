"""Selection transaction and independent Phase 4 component operations.

Timers are a thin integration layer, not Phase 6's HTTP-freshness/conditional
scheduler. Generation checks and serialized commits protect memory and disk.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Callable

from weather_terminal.alerts import normalize_alerts
from weather_terminal.conditions import normalize_stations, restore_conditions
from weather_terminal.daily import normalize_daily, select_days
from weather_terminal.config import AppConfig
from weather_terminal.hourly import normalize_hourly
from weather_terminal.models import Component, ComponentState
from weather_terminal.offline import OfflineSnapshot, load_selection, restore_component, alerts_endpoint
from weather_terminal.services.geocoding import Geocoder
from weather_terminal.services.http import HttpService, ServiceFailure, validate_destination
from weather_terminal.services.nws import NwsService, normalize_point, point_url, validate_discovery, validate_point_location
from weather_terminal.services.observations import retrieve_conditions
from weather_terminal.storage import CacheStore, CachedPayload, SavedSelection

WEATHER_COMPONENTS = (Component.ALERTS, Component.CONDITIONS, Component.HOURLY, Component.DAILY)


class WeatherSlice:
    def __init__(self, config: AppConfig, store: CacheStore, http: HttpService,
                 *, changed: Callable[[], None] | None = None) -> None:
        self.config, self.store, self.http = config, store, http
        self.geocoder, self.nws = Geocoder(http, store), NwsService(http)
        self.snapshot = OfflineSnapshot()
        self.states: dict[Component, ComponentState] = {}
        self.changed = changed or (lambda: None)
        self.generation = 0
        self.intent = 0
        self.lock = asyncio.Lock()
        self.metadata_lock = asyncio.Lock()
        self.closed = False
        self.running: set[Component] = set()
        self.station_data = None
        self.station_epoch = 0
        self.conditions_station_epoch = -1

    @property
    def hourly(self):
        return self.states.get(Component.HOURLY)

    def _install(self, snapshot: OfflineSnapshot) -> None:
        self.snapshot = snapshot
        self.states = {}
        self.station_data = None
        self.station_epoch += 1
        self.conditions_station_epoch = -1
        selection = snapshot.selection
        if selection is None:
            self.changed()
            return
        station_entry = snapshot.entries.get(Component.STATIONS)
        if station_entry is not None:
            try:
                self.station_data = normalize_stations(station_entry.raw_body, selection.location)
            except ServiceFailure:
                snapshot.entries.pop(Component.STATIONS, None)
                snapshot.issues += ("Cached station metadata failed semantic validation.",)
        def daily(body):
            data = normalize_daily(body)
            select_days(data, self.http.clock.now(), snapshot.timezone_name)  # Validate local-date duplicates before use.
            return data
        adapters = {Component.HOURLY: normalize_hourly, Component.DAILY: daily, Component.ALERTS: normalize_alerts}
        for component in WEATHER_COMPONENTS:
            entry = snapshot.entries.get(component)
            if entry is None:
                continue
            try:
                validate_destination(entry.endpoint, provider="nws", geocoder_endpoint=self.config.geocoding.endpoint)
                if component == Component.ALERTS and entry.endpoint != alerts_endpoint(selection):
                    raise ValueError("Different alert point")
                normalize = (lambda body, e=entry: restore_conditions(body, selection.location, self.station_data, e.endpoint)) if component == Component.CONDITIONS else adapters[component]
                self.states[component] = restore_component(entry, generation=self.generation, normalize=normalize)
            except (ServiceFailure, ValueError):
                snapshot.entries.pop(component, None)
                snapshot.issues += (f"Cached {component.value} data failed semantic validation.",)
        self.changed()

    def restore(self, snapshot: OfflineSnapshot) -> None:
        self.generation += 1
        self._install(snapshot)

    def invalidate_pending(self) -> None:
        self.intent += 1

    async def _point(self, location):
        cached = await asyncio.to_thread(self.store.read_component, location, Component.POINT, endpoint=point_url(location))
        entry = cached.value
        if entry is not None:
            age = (self.http.clock.now() - entry.provenance.last_checked_at).total_seconds()
            if 0 <= age < self.config.refresh.metadata_seconds:
                try:
                    discovery = normalize_point(entry.raw_body, entry.endpoint, self.config.geocoding.endpoint)
                    validate_point_location(discovery, location)
                    return discovery, entry
                except ServiceFailure:
                    pass
        result = await self.nws.point(location)
        return result.discovery, CachedPayload(location, Component.POINT, result.discovery.point_url,
                                               result.body, result.provenance)

    async def confirm(self, location, *, ticket: int | None = None) -> bool:
        if ticket is None:
            self.invalidate_pending()
            ticket = self.intent
        discovery, point = await self._point(location)
        location = replace(location, timezone_name=discovery.timezone_name or location.timezone_name)
        selection = SavedSelection(location, discovery)
        snapshot = await asyncio.to_thread(load_selection, self.store, selection)
        async with self.lock:
            if self.closed or ticket != self.intent:
                return False
            async def commit():
                issues = list(snapshot.issues)
                for write in (lambda: self.store.write_selection(selection),
                              lambda: self.store.write_component(replace(point, location=location))):
                    result = await self._write(write)
                    if not result.saved:
                        issues.append(result.issue)
                snapshot.issues = tuple(issues)
                snapshot.entries[Component.POINT] = replace(point, location=location)
                self.generation += 1
                self._install(snapshot)
                return True
            return await self._settle(asyncio.create_task(commit()))

    async def _settle(self, task):
        interrupted = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                interrupted = True
                if task.done():
                    result = task.result()
                    break
        if interrupted:
            raise asyncio.CancelledError
        return result

    async def _write(self, operation):
        return await self._settle(asyncio.create_task(asyncio.to_thread(operation)))

    async def _save(self, entry):
        saved = await self._write(lambda: self.store.write_component(entry))
        if not saved.saved:
            self.snapshot.issues += (saved.issue,)

    def _current(self, generation):
        return not self.closed and generation == self.generation

    async def _discovery(self, generation):
        async with self.metadata_lock:
            if not self._current(generation):
                return None
            selection = self.snapshot.selection
            discovery = selection.discovery
            entry = self.snapshot.entries.get(Component.POINT)
            age = None if entry is None else (self.http.clock.now() - entry.provenance.last_checked_at).total_seconds()
            if discovery is None or age is None or not 0 <= age < self.config.refresh.metadata_seconds:
                discovery, point = await self._point(selection.location)
                async with self.lock:
                    if not self._current(generation):
                        return None
                    selection = SavedSelection(replace(selection.location, timezone_name=discovery.timezone_name or selection.location.timezone_name), discovery)
                    self.snapshot.selection = selection
                    point = replace(point, location=selection.location)
                    self.snapshot.entries[Component.POINT] = point
                    saved = await self._write(lambda: self.store.write_selection(selection))
                    if not saved.saved:
                        self.snapshot.issues += (saved.issue,)
                    await self._save(point)
            validate_discovery(discovery, self.config.geocoding.endpoint)
            validate_point_location(discovery, selection.location)
            return discovery

    async def _stations(self, discovery, selection, generation):
        async with self.metadata_lock:
            if not self._current(generation):
                return None
            entry = self.snapshot.entries.get(Component.STATIONS)
            age = None if entry is None else (self.http.clock.now() - entry.provenance.last_checked_at).total_seconds()
            if (self.station_data is not None and entry is not None and entry.endpoint == discovery.stations_url
                    and age is not None and 0 <= age < self.config.refresh.metadata_seconds):
                return self.station_data
            try:
                result = await self.nws.stations(discovery, selection.location)
            except ServiceFailure:
                # Known matching metadata may still evaluate live observations during a metadata outage.
                if self.station_data is not None and entry is not None and entry.endpoint == discovery.stations_url:
                    return replace(self.station_data, notes=self.station_data.notes + ("Station metadata refresh failed; cached station list used.",))
                raise
            async with self.lock:
                if not self._current(generation):
                    return None
                self.station_data = result.data
                self.station_epoch += 1
                entry = CachedPayload(selection.location, Component.STATIONS, result.endpoint, result.body, result.provenance)
                self.snapshot.entries[Component.STATIONS] = entry
                await self._save(entry)
                return result.data

    async def refresh(self, component: Component = Component.HOURLY) -> bool:
        component = Component(component)
        if component not in WEATHER_COMPONENTS:
            raise ValueError("Not a weather component")
        if self.closed or component in self.running or self.snapshot.selection is None:
            return False
        self.running.add(component)
        generation = self.generation
        selection = self.snapshot.selection
        state = self.states.get(component)
        if state is None:
            state = ComponentState(component, selection.location.key, generation)
            self.states[component] = state
        state.begin_attempt(self.http.clock.now())
        self.changed()
        try:
            if component == Component.ALERTS:
                # Point alerts remain independent of forecast/station metadata failures.
                result = await self.nws.alerts(selection.location)
            else:
                discovery = await self._discovery(generation)
                if discovery is None:
                    return False
                selection = self.snapshot.selection
                if component == Component.HOURLY:
                    hourly = await self.nws.hourly(discovery)
                    from weather_terminal.services.nws import WeatherResult
                    result = WeatherResult(hourly.forecast, hourly.body, hourly.provenance, discovery.hourly_url)
                elif component == Component.DAILY:
                    result = await self.nws.daily(discovery)
                    select_days(result.data, self.http.clock.now(), self.snapshot.timezone_name)
                else:
                    stations = await self._stations(discovery, selection, generation)
                    if stations is None:
                        return False
                    result = await retrieve_conditions(self.nws, stations, previous=state.data,
                                                       reevaluate=self.conditions_station_epoch != self.station_epoch,
                                                       threshold=self.config.freshness.observation_seconds)
            async with self.lock:
                if not self._current(generation):
                    return False
                state.accept_body(data=result.data, raw_body=result.body, endpoint=result.endpoint, provenance=result.provenance)
                entry = CachedPayload(selection.location, component, result.endpoint, result.body, result.provenance)
                self.snapshot.entries[component] = entry
                if component == Component.CONDITIONS:
                    self.conditions_station_epoch = self.station_epoch
                await self._save(entry)
                return True
        except ServiceFailure as exc:
            if self._current(generation):
                state.fail(exc.error)
        finally:
            state.in_flight = False
            self.running.discard(component)
            if not self.closed:
                self.changed()
        return False

    async def aclose(self) -> None:
        self.closed = True
        self.invalidate_pending()
        await self.http.aclose()
