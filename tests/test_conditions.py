from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from weather_terminal.conditions import (
    normalize_stations, normalize_observation, choose_observation, fresh_observation,
    restore_conditions, render_conditions, observation_endpoint, observation_station, distance_miles,
)
from weather_terminal.config import AppConfig, FreshnessSettings
from weather_terminal.models import ErrorKind
from weather_terminal.services.http import HttpService, ServiceFailure
from weather_terminal.services.nws import NwsService
from weather_terminal.services.observations import retrieve_conditions
from tests.phase2_helpers import LOCATION, NOW
from tests.phase4_helpers import RoutingTransport, stations_body, observation_body, obs_url
from tests.phase3_helpers import reply
from tests.test_app import FakeClock


def test_station_list_is_distance_sorted_not_source_order_and_limits_identity():
    data = normalize_stations(stations_body(8), LOCATION)
    assert [s.identifier for s in data.stations[:5]] == [f"SYN{i}" for i in range(1, 6)]
    assert data.stations[0].miles == pytest.approx(.69, abs=.02)
    assert distance_miles(LOCATION, LOCATION.latitude, LOCATION.longitude) == 0
    assert observation_endpoint("SYN1") == obs_url("SYN1")
    assert observation_station(obs_url("SYN1")) == "SYN1"


@pytest.mark.parametrize("url", ["https://evil.example/stations/SYN1/observations/latest",
    "https://api.weather.gov/stations/../observations/latest", "https://api.weather.gov/stations/SYN1/observations/latest?x=1"])
def test_invalid_station_endpoints_cannot_be_restored(url):
    with pytest.raises(ServiceFailure):
        restore_conditions(observation_body(), LOCATION, endpoint=url)


def test_bad_station_record_skipped_not_requestable_and_duplicates_deduped():
    body = stations_body(2)
    body["features"].append(body["features"][0])
    body["features"][1]["geometry"] = None
    data = normalize_stations(body, LOCATION)
    assert len(data.stations) == 1 and data.notes
    with pytest.raises(ServiceFailure):
        normalize_stations({"features": [{"properties": None}]}, LOCATION)


def test_null_zero_and_feels_like_precedence_and_unit_conversion():
    body = observation_body()
    p = body["properties"]
    p["windSpeed"]["value"] = 1
    p["windGust"]["value"] = 2
    p["windChill"]["value"] = 0
    p["heatIndex"]["value"] = 20
    o = normalize_observation(body, "SYN1")
    assert o.temperature_f == 32 and o.feels_like_f == 32 and o.feels_like_source == "NWS wind chill"
    assert o.wind_mph == pytest.approx(2.236936292) and o.gust_mph == pytest.approx(4.473872584)
    assert o.humidity_percent == 0 and o.direction_degrees == 0 and o.dewpoint_f is None
    p["windChill"]["value"] = None
    assert normalize_observation(body).feels_like_f == 68
    p["heatIndex"]["value"] = None
    assert normalize_observation(body).feels_like_f is None


@pytest.mark.parametrize("field", ["windSpeed", "windGust", "relativeHumidity", "dewpoint", "windChill"])
def test_invalid_optional_units_do_not_invalidate_usable_observation(field):
    body = observation_body()
    body["properties"][field] = {"value": 1, "unitCode": "unknown:unit"}
    o = normalize_observation(body)
    assert o.temperature_f == 32 and o.notes


@pytest.mark.parametrize("field,value", [("timestamp", None), ("timestamp", "2026-01-10T12:00:00"),
    ("temperature", {"value": None}), ("temperature", {"value": True, "unitCode": "wmoUnit:degC"}),
    ("station", "https://evil.example/stations/SYN1")])
def test_unusable_required_observation_fields_are_failure(field, value):
    body = observation_body()
    body["properties"][field] = value
    with pytest.raises(ServiceFailure):
        normalize_observation(body, "SYN1")


def test_station_identity_mismatch_is_failure():
    with pytest.raises(ServiceFailure):
        normalize_observation(observation_body("SYN2"), "SYN1")


def test_choose_nearest_fresh_then_most_recent_stale_and_exclude_future():
    stations = normalize_stations(stations_body(3), LOCATION).stations
    old = normalize_observation(observation_body("SYN1", when=NOW - timedelta(hours=3)))
    fresh = normalize_observation(observation_body("SYN2", when=NOW - timedelta(minutes=30)))
    recent = normalize_observation(observation_body("SYN3", when=NOW - timedelta(minutes=10)))
    pairs = [(s, SimpleNamespace(observation=o)) for s, o in zip(stations, (old, fresh, recent))]
    assert choose_observation(pairs, NOW, 5400)[0].identifier == "SYN2"
    assert choose_observation(pairs, NOW + timedelta(hours=3), 5400)[0].identifier == "SYN3"
    assert not fresh_observation(replace(fresh, timestamp=NOW + timedelta(seconds=1)), NOW, 5400)
    future = [(stations[0], SimpleNamespace(observation=replace(old, timestamp=NOW + timedelta(days=1))))]
    assert choose_observation(future, NOW, 5400) is None


async def test_checks_only_nearest_five_and_partial_failure_does_not_block_choice():
    clock = FakeClock()
    routes = {obs_url(f"SYN{i}"): [reply(obs_url(f"SYN{i}"), observation_body(f"SYN{i}", when=NOW - timedelta(minutes=10)))] for i in range(1, 6)}
    routes[obs_url("SYN1")] = [reply(obs_url("SYN1"), status=403)]
    transport = RoutingTransport(clock, routes)
    http = HttpService(AppConfig(), clock=clock, transport=transport)
    result = await retrieve_conditions(NwsService(http), normalize_stations(stations_body(8), LOCATION),
                                       previous=None, reevaluate=True, threshold=5400)
    assert len(transport.calls) == 5
    assert result.data.station.identifier == "SYN2" and "1 station requests" in result.data.selection_note
    assert result.provenance.source_time == NOW - timedelta(minutes=10)
    assert result.provenance.retrieved_at >= NOW


async def test_routine_refresh_reuses_fresh_working_station():
    clock = FakeClock()
    stations = normalize_stations(stations_body(), LOCATION)
    previous = restore_conditions(observation_body(), LOCATION, stations, obs_url())
    transport = RoutingTransport(clock, {obs_url(): [reply(obs_url(), observation_body())]})
    result = await retrieve_conditions(NwsService(HttpService(AppConfig(), clock=clock, transport=transport)), stations,
                                      previous=previous, reevaluate=False, threshold=5400)
    assert len(transport.calls) == 1 and "Reused" in result.data.selection_note


async def test_stale_working_station_rechecks_others_and_falls_back_to_newest():
    clock = FakeClock()
    stations = normalize_stations(stations_body(), LOCATION)
    previous = restore_conditions(observation_body(), LOCATION, stations, obs_url())
    routes = {obs_url(f"SYN{i}"): [reply(obs_url(f"SYN{i}"), observation_body(f"SYN{i}", when=NOW - timedelta(hours=5 - i)))] for i in range(1, 4)}
    result = await retrieve_conditions(NwsService(HttpService(AppConfig(), clock=clock, transport=RoutingTransport(clock, routes))),
                                      stations, previous=previous, reevaluate=False, threshold=5400)
    assert result.data.station.identifier == "SYN3" and "stale" in result.data.selection_note
    text = render_conditions(result.data, result.provenance, clock.now(), "America/Chicago", FreshnessSettings())
    assert "Observation stale" in text and "Gusts: Unavailable" in text and "Observed:" in text


async def test_station_probe_concurrency_is_bounded_and_cancellation_is_joined():
    import asyncio
    stations = normalize_stations(stations_body(5), LOCATION)
    entered, release = asyncio.Event(), asyncio.Event()
    class FakeNws:
        http = SimpleNamespace(clock=FakeClock())
        current = maximum = 0
        async def observation(self, identifier):
            self.current += 1
            self.maximum = max(self.maximum, self.current)
            if self.current == 2:
                entered.set()
            try:
                await release.wait()
                raise ServiceFailure(ErrorKind.NETWORK, "synthetic")
            finally:
                self.current -= 1
    nws = FakeNws()
    task = asyncio.create_task(retrieve_conditions(nws, stations, previous=None, reevaluate=True, threshold=5400))
    await asyncio.wait_for(entered.wait(), 2)
    assert nws.maximum == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert nws.current == 0


async def test_no_usable_stations_or_all_observations_missing_is_a_failure():
    clock = FakeClock()
    nws = NwsService(HttpService(AppConfig(), clock=clock, transport=RoutingTransport(clock, {})))
    with pytest.raises(ServiceFailure, match="no stations"):
        await retrieve_conditions(nws, normalize_stations({"features": []}, LOCATION), previous=None, reevaluate=True, threshold=5400)
    routes = {obs_url(f"SYN{i}"): [reply(obs_url(f"SYN{i}"), observation_body(f"SYN{i}", temp=None))] for i in range(1, 4)}
    nws = NwsService(HttpService(AppConfig(), clock=clock, transport=RoutingTransport(clock, routes)))
    with pytest.raises(ServiceFailure, match="No timestamped"):
        await retrieve_conditions(nws, normalize_stations(stations_body(), LOCATION), previous=None, reevaluate=True, threshold=5400)


def test_paginated_station_list_is_not_accepted_as_complete_ranking():
    with pytest.raises(ServiceFailure):
        normalize_stations({"features": [], "pagination": {"next": "https://api.weather.gov/stations?cursor=next"}}, LOCATION)


def station_next_url(identifiers, cursor="opaque-provider-cursor"):
    from urllib.parse import urlencode
    query = [(f"id[{i}]", identifier) for i, identifier in enumerate(identifiers)]
    query.append(("cursor", cursor))
    return "https://api.weather.gov/stations?" + urlencode(query)


def test_redundant_filtered_station_next_link_is_accepted_and_distance_sorted():
    # Synthetic equivalent of NWS returning all IDs but advertising another page.
    # Filter order differs from source order and distance order intentionally.
    body = stations_body(28)
    body["pagination"] = {"next": station_next_url([f"SYN{i}" for i in range(1, 29)])}
    data = normalize_stations(body, LOCATION)
    assert [s.identifier for s in data.stations] == [f"SYN{i}" for i in range(1, 29)]
    assert not data.notes


@pytest.mark.parametrize("identifiers", [
    ["SYN1", "SYN2", "SYN3", "UNSEEN"],
    ["SYN1", "SYN2"],
    ["SYN1", "SYN1", "SYN2", "SYN3"],
    [],
])
def test_station_next_filter_must_match_all_returned_identities(identifiers):
    body = stations_body()
    body["pagination"] = {"next": station_next_url(identifiers)}
    with pytest.raises(ServiceFailure, match="station list is invalid"):
        normalize_stations(body, LOCATION)


@pytest.mark.parametrize("next_url", [
    "http://api.weather.gov/stations?id[0]=SYN1&cursor=x",
    "https://evil.example/stations?id[0]=SYN1&cursor=x",
    "https://user@api.weather.gov/stations?id[0]=SYN1&cursor=x",
    "https://api.weather.gov:444/stations?id[0]=SYN1&cursor=x",
    "https://api.weather.gov:invalid/stations?id[0]=SYN1&cursor=x",
    "https://api.weather.gov/stations/other?id[0]=SYN1&cursor=x",
    "https://api.weather.gov/stations?id[0]=SYN1&cursor=x#fragment",
    "https://api.weather.gov/stations?id[0]=SYN1&cursor=x\n",
    "https://api.weather.gov/stations?id[0]=SYN1&cursor=x&cursor=y",
    "https://api.weather.gov/stations?id[0]=SYN1&cursor=",
    "https://api.weather.gov/stations?id[0]=SYN1",
    "https://api.weather.gov/stations?id[1]=SYN1&cursor=x",
    "https://api.weather.gov/stations?id[0]=SYN1&id[0]=SYN1&cursor=x",
    "https://api.weather.gov/stations?id[0]=SYN1&cursor=x&unknown=1",
    "https://api.weather.gov/stations?id[0]=SYN1&cursor=x&broken",
    "https://api.weather.gov/stations?id[0]=bad%2Fid&cursor=x",
    True,
    False,
    0,
    [],
    {},
])
def test_invalid_station_next_links_are_not_exempted(next_url):
    body = stations_body(1)
    body["pagination"] = {"next": next_url}
    with pytest.raises(ServiceFailure, match="station list is invalid"):
        normalize_stations(body, LOCATION)


@pytest.mark.parametrize("pagination", [None, {}, {"next": None}, {"next": ""}])
def test_station_pagination_without_continuation_remains_accepted(pagination):
    body = stations_body(1)
    body["pagination"] = pagination
    assert normalize_stations(body, LOCATION).stations[0].identifier == "SYN1"


@pytest.mark.parametrize("pagination", [False, [], "invalid"])
def test_invalid_station_pagination_structure_is_rejected(pagination):
    body = stations_body(1)
    body["pagination"] = pagination
    with pytest.raises(ServiceFailure, match="station list is invalid"):
        normalize_stations(body, LOCATION)


def test_redundant_station_pagination_keeps_skip_notes_and_deduplication():
    body = stations_body(3)
    body["features"][0]["geometry"] = None  # SYN3 is present, but unusable.
    body["features"].append(body["features"][1])  # Duplicate SYN2.
    body["pagination"] = {"next": station_next_url(["SYN1", "SYN2", "SYN3"])}
    data = normalize_stations(body, LOCATION)
    assert [s.identifier for s in data.stations] == ["SYN1", "SYN2"]
    assert data.notes == ("1 station records lacked usable identity/coordinates and were skipped.",)


@pytest.mark.parametrize("feature", [None, {"properties": None}, {"properties": {}},
                                      {"properties": {"stationIdentifier": "bad/id"}}])
def test_redundant_station_pagination_requires_readable_returned_identities(feature):
    body = stations_body(1)
    body["features"].append(feature)
    body["pagination"] = {"next": station_next_url(["SYN1"])}
    with pytest.raises(ServiceFailure, match="station list is invalid"):
        normalize_stations(body, LOCATION)


async def test_station_service_accepts_exhausted_filter_without_following_next_link():
    from tests.phase3_helpers import POINT_BODY, POINT
    from tests.phase4_helpers import STATIONS
    from weather_terminal.services.nws import normalize_point

    clock = FakeClock()
    config = AppConfig()
    discovery = normalize_point(POINT_BODY, POINT, config.geocoding.endpoint)
    body = stations_body(3)
    body["pagination"] = {"next": station_next_url(["SYN1", "SYN2", "SYN3"])}
    transport = RoutingTransport(clock, {STATIONS: [reply(STATIONS, body)]})
    result = await NwsService(HttpService(config, clock=clock, transport=transport)).stations(discovery, LOCATION)
    assert len(transport.calls) == 1
    assert result.endpoint == STATIONS and result.body == body
    assert [s.identifier for s in result.data.stations] == ["SYN1", "SYN2", "SYN3"]
    # Raw cached bodies take the same normalization path; retain the provider link.
    assert normalize_stations(result.body, LOCATION) == result.data


@pytest.mark.parametrize("cached", [False, True])
def test_conditions_display_precision_preserves_observation_and_source(cached):
    from copy import deepcopy
    from weather_terminal.models import Provenance

    body = observation_body(temp=30.15)
    p = body["properties"]
    p["heatIndex"]["value"] = 29.19494444444444  # About 84.5509 °F.
    p["dewpoint"]["value"] = 18.123456
    p["windSpeed"]["value"] = 2.57
    p["windGust"]["value"] = 4.12
    p["windDirection"]["value"] = 184.678
    p["relativeHumidity"]["value"] = 35.2872
    original_body = deepcopy(body)
    data = restore_conditions(body, LOCATION, normalize_stations(stations_body(), LOCATION), obs_url())
    original_observation = replace(data.observation)
    text = render_conditions(data, Provenance(NOW, NOW), NOW, "America/Chicago",
                             FreshnessSettings(), cached=cached)
    assert "Temperature: 86.3 °F" in text
    assert "Feels like: 84.6 °F (NWS heat index)" in text
    assert "Wind: 185° | Sustained: 5.7 mph | Gusts: 9.2 mph" in text
    assert "Humidity: 35% | Dew point: 64.6 °F" in text
    assert "84.5509" not in text and "35.2872" not in text
    assert body == original_body and data.observation == original_observation
    assert data.observation.wind_mph == pytest.approx(2.57 * 2.236936292)
    assert data.observation.humidity_percent == 35.2872


def test_conditions_display_keeps_zero_distinct_from_unavailable():
    from weather_terminal.models import Provenance

    data = restore_conditions(observation_body(), LOCATION)
    text = render_conditions(data, Provenance(NOW, NOW), NOW, None, FreshnessSettings())
    assert "Temperature: 32.0 °F" in text
    assert "Wind: 0° | Sustained: 0.0 mph | Gusts: Unavailable" in text
    assert "Humidity: 0% | Dew point: Unavailable" in text
    assert "Feels like: Unavailable (not supplied by NWS)" in text
