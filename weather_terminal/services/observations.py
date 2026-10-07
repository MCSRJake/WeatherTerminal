"""Bounded station evaluation, with reuse of a fresh working station."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from weather_terminal.conditions import Conditions, StationList, choose_observation, fresh_observation
from weather_terminal.models import ErrorKind, Provenance
from weather_terminal.services.http import ServiceFailure
from weather_terminal.services.nws import NwsService


@dataclass(frozen=True)
class ConditionsResult:
    data: Conditions
    body: dict[str, Any]
    provenance: Provenance
    endpoint: str


async def retrieve_conditions(nws: NwsService, stations: StationList, *, previous: Conditions | None,
                              reevaluate: bool, threshold: float) -> ConditionsResult:
    nearest = stations.stations[:5]
    if not nearest:
        raise ServiceFailure(ErrorKind.MISSING_DATA, "NWS supplied no stations with usable coordinates; cached conditions retained.")
    results = []
    failures = 0
    checked = set()
    if previous is not None and not reevaluate:
        station = next((s for s in nearest if s.identifier == previous.observation.identifier), None)
        if station is not None:
            checked.add(station.identifier)
            try:
                response = await nws.observation(station.identifier)
                if fresh_observation(response.data, nws.http.clock.now(), threshold):
                    return ConditionsResult(Conditions(response.data, station, "Reused working station; fresh usable observation. " + " ".join(stations.notes)),
                                            response.body, response.provenance, response.endpoint)
                results.append((station, response))
            except ServiceFailure:
                failures += 1
    semaphore = asyncio.Semaphore(2)
    async def check(station):
        nonlocal failures
        async with semaphore:
            try:
                result = await nws.observation(station.identifier)
                return station, result
            except ServiceFailure:
                failures += 1
                return None
    tasks = [asyncio.create_task(check(station)) for station in nearest if station.identifier not in checked]
    try:
        replies = await asyncio.gather(*tasks)
        results.extend(pair for pair in replies if pair is not None)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    # choose_observation expects .observation; adapt without altering public WeatherResult.
    @dataclass(frozen=True)
    class Candidate:
        observation: Any
        result: Any
    chosen = choose_observation([(station, Candidate(result.data, result)) for station, result in results],
                                nws.http.clock.now(), threshold)
    if chosen is None:
        raise ServiceFailure(ErrorKind.MISSING_DATA, "No timestamped, usable non-future station observations; cached conditions retained.")
    station, candidate = chosen
    result = candidate.result
    fresh = fresh_observation(result.data, nws.http.clock.now(), threshold)
    note = ("Nearest fresh usable station among the nearest five checked." if fresh else
            "No fresh usable observation among the nearest five; showing the most recent usable observation (stale).")
    if failures:
        note += f" {failures} station requests were unavailable or unusable."
    if stations.notes:
        note += " " + " ".join(stations.notes)
    return ConditionsResult(Conditions(result.data, station, note), result.body, result.provenance, result.endpoint)
