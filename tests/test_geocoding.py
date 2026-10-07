from copy import deepcopy
import asyncio

import pytest

from weather_terminal.services.geocoding import parse_candidates
from weather_terminal.services.http import ServiceFailure
from tests.phase3_helpers import GEO, candidate, reply, slice_for


def test_unique_ambiguous_and_obvious_duplicates():
    one = candidate()
    duplicate = candidate(lat="34.3662", lon="-89.5191")
    other = candidate("Oxford", "Ohio", "39.5", "-84.7", "Butler County")
    assert len(parse_candidates([one, duplicate])) == 1
    assert [place.state for place in parse_candidates([one, duplicate, other])] == ["MS", "OH"]
    assert parse_candidates([]) == ()


@pytest.mark.parametrize("change", [
    {"category": "amenity"}, {"addresstype": "county"}, {"addresstype": "state"},
    {"lat": "NaN"}, {"lon": "inf"}, {"name": 17},
    {"address": {"country_code": "ca", "state": "Mississippi"}},
    {"address": {"country_code": "us", "state": "Puerto Rico"}},
])
def test_irrelevant_or_invalid_results_are_excluded(change):
    body = candidate()
    body.update(change)
    assert parse_candidates([body]) == ()


@pytest.mark.parametrize("body", [{}, [None], ["city"]])
def test_malformed_result_list_is_not_success(body):
    with pytest.raises(ServiceFailure):
        parse_candidates(body)


async def test_normalized_repeat_and_concurrent_submission_use_cache(tmp_path):
    controller, transport, _ = slice_for(tmp_path, [reply(GEO, [candidate()])])
    first, second = await asyncio.gather(controller.geocoder.search(" Oxford, MS "),
                                         controller.geocoder.search("oxford, ms"))
    assert first.candidates == second.candidates
    assert not first.cached and second.cached
    assert len(transport.calls) == 1
    parameters = transport.calls[0][2]
    assert parameters["countrycodes"] == "us"
    assert parameters["format"] == "jsonv2" and parameters["addressdetails"] == "1"
    assert parameters["limit"] == "10"


async def test_successful_empty_query_is_cached(tmp_path):
    controller, transport, _ = slice_for(tmp_path, [reply(GEO, [])])
    assert not (await controller.geocoder.search("not a city")).candidates
    assert (await controller.geocoder.search("not a city")).cached
    assert len(transport.calls) == 1


async def test_invalid_query_never_sends_request(tmp_path):
    controller, transport, _ = slice_for(tmp_path, [])
    for query in ("", "\x1bTown", "x" * 201):
        with pytest.raises(ServiceFailure):
            await controller.geocoder.search(query)
    assert not transport.calls


async def test_cache_write_failure_keeps_search_results_usable(tmp_path, monkeypatch):
    from weather_terminal.storage import WriteResult
    controller, transport, _ = slice_for(tmp_path, [reply(GEO, [candidate()])])
    monkeypatch.setattr(controller.store, "write_search", lambda value: WriteResult(False, "test storage issue"))
    result = await controller.geocoder.search("Oxford, MS")
    assert result.candidates[0].state == "MS" and result.issues
