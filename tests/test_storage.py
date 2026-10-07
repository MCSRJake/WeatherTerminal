from dataclasses import replace
from datetime import timedelta
import json
import logging
from pathlib import Path

import pytest

from weather_terminal.config import RuntimePaths
from weather_terminal.models import Component, Location, Provenance
from weather_terminal.storage import (
    CacheStore, CachedSearch, SavedSelection, atomic_write_json, normalize_query,
)
from tests.phase2_helpers import DISCOVERY, LOCATION, NOW, SELECTION, entry, seed_cache


@pytest.fixture
def store(tmp_path):
    return CacheStore(RuntimePaths(tmp_path))


def test_construction_and_missing_reads_do_not_create_files(tmp_path, store):
    assert store.read_selection().value is None
    assert store.read_component(LOCATION, Component.HOURLY).value is None
    assert store.read_search("https://nominatim.openstreetmap.org/search", "Oxford").value is None
    assert not list(tmp_path.iterdir())


def test_selection_round_trip(store):
    assert store.write_selection(SELECTION).saved
    assert store.read_selection().value == SELECTION
    assert store.write_selection(SavedSelection(LOCATION)).saved
    assert store.read_selection().value.discovery is None


@pytest.mark.parametrize("component", list(Component))
def test_all_component_round_trips(store, component):
    if component == Component.POINT:
        cached = entry(Component.HOURLY)
        cached = replace(cached, component=component, endpoint=DISCOVERY.point_url, raw_body={"properties": {"timeZone": "America/Chicago"}})
    elif component == Component.STATIONS:
        cached = replace(entry(), component=component, endpoint=DISCOVERY.stations_url, raw_body={"features": []})
    else:
        cached = entry(component)
    assert store.write_component(cached).saved
    read = store.read_component(LOCATION, component, endpoint=cached.endpoint)
    assert read.value == cached
    assert not read.issues
    # Independent store instance simulates a restart, not a memory memoization.
    assert CacheStore(store.paths).read_component(LOCATION, component).value == cached


def test_read_never_advances_provenance_and_unknown_fields_survive(store):
    cached = entry(provenance=Provenance(NOW, NOW + timedelta(minutes=1), NOW - timedelta(hours=2), etag='"v1"'))
    cached.raw_body["unknownExtra"] = {"zero": 0, "missing": None}
    assert store.write_component(cached).saved
    for _ in range(3):
        read = store.read_component(LOCATION, Component.HOURLY).value
        assert read.provenance == cached.provenance
        assert read.raw_body["unknownExtra"] == {"zero": 0, "missing": None}


def test_cache_isolated_by_coordinates_and_endpoint(store):
    cached = entry()
    assert store.write_component(cached).saved
    other_city = Location("Oxford", "OH", 39.5, -84.7)
    other_url = "https://api.weather.gov/gridpoints/MEG/2,2/forecast/hourly"
    assert store.read_component(other_city, Component.HOURLY).value is None
    assert store.read_component(LOCATION, Component.HOURLY, endpoint=other_url).value is None
    second = replace(cached, endpoint=other_url, provenance=Provenance(NOW, NOW + timedelta(minutes=1)))
    assert store.write_component(second).saved
    assert store.read_component(LOCATION, Component.HOURLY, endpoint=cached.endpoint).value == cached
    assert store.read_component(LOCATION, Component.HOURLY).value == second


@pytest.mark.parametrize("text", ["{", "null", "[]", '{"x":1,"x":2}', '{"x":NaN}', '{"x":1e400}', '\ufeff{"x":1}'])
def test_corrupt_json_is_reported_without_crashing(store, text):
    store.paths.root.mkdir(parents=True, exist_ok=True)
    store.paths.state_file.write_text(text, encoding="utf-8")
    result = store.read_selection()
    assert result.value is None
    assert result.issues


@pytest.mark.parametrize("mutation", [
    {"schema_version": 99}, {"schema_version": True}, {"kind": "wrong"},
    {"location_key": "wrong"}, {"component": "alerts"}, {"endpoint_key": "wrong"},
    {"endpoint": "https://api.weather.gov/other"},
    {"provenance": {"retrieved_at": "2026-01-10T12:00:00", "last_checked_at": "2026-01-10T12:00:00Z"}},
    {"raw_body": {"properties": {"periods": [{"startTime": "bad", "endTime": "bad"}]}}},
])
def test_identity_schema_and_timestamp_mismatch_are_isolated(store, mutation):
    cached = entry()
    assert store.write_component(cached).saved
    good = entry(Component.DAILY)
    assert store.write_component(good).saved
    path = store.component_path(LOCATION, cached.component, cached.endpoint)
    document = json.loads(path.read_text(encoding="utf-8"))
    document.update(mutation)
    path.write_text(json.dumps(document), encoding="utf-8")
    assert store.read_component(LOCATION, Component.HOURLY).value is None
    assert store.read_component(LOCATION, Component.HOURLY).issues
    assert store.read_component(LOCATION, Component.DAILY).value == good


def test_corrupt_candidate_does_not_block_other_endpoint(store):
    original = entry()
    second = replace(original, endpoint="https://api.weather.gov/gridpoints/MEG/2,2/forecast/hourly")
    assert store.write_component(original).saved
    assert store.write_component(second).saved
    store.component_path(LOCATION, original.component, original.endpoint).write_text("{", encoding="utf-8")
    result = store.read_component(LOCATION, Component.HOURLY)
    assert result.value == second
    assert result.issues


def test_malformed_write_preserves_last_good(store):
    cached = entry()
    assert store.write_component(cached).saved
    path = store.component_path(LOCATION, cached.component, cached.endpoint)
    previous = path.read_bytes()
    for bad_body in (None, [], {}, {"properties": {"periods": "not a list"}},
                     {"properties": {"periods": [], "bad": float("nan")}}):
        assert not store.write_component(replace(cached, raw_body=bad_body)).saved
        assert path.read_bytes() == previous
    assert store.read_component(LOCATION, Component.HOURLY).value == cached


def test_adapter_validator_can_reject_before_replacement(store):
    cached = entry()
    assert store.write_component(cached).saved
    def reject(component, body):
        raise ValueError("simulated provider-required-field failure")
    strict = CacheStore(store.paths, validator=reject)
    assert not strict.write_component(cached).saved
    assert strict.read_component(LOCATION, Component.HOURLY).value is None
    assert store.read_component(LOCATION, Component.HOURLY).value == cached


def test_failed_replacement_keeps_target_and_cleans_temp(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    atomic_write_json(target, {"original": True})
    previous = target.read_bytes()
    def fail_replace(source, destination):
        assert Path(source).parent == target.parent
        assert json.loads(Path(source).read_text(encoding="utf-8")) == {"new": True}
        raise PermissionError("simulated locked destination")
    monkeypatch.setattr("weather_terminal.storage.os.replace", fail_replace)
    with pytest.raises(PermissionError):
        atomic_write_json(target, {"new": True})
    assert target.read_bytes() == previous
    assert not list(tmp_path.glob(".pending-*"))


def test_disk_failure_does_not_mutate_usable_in_memory_payload(store, monkeypatch):
    original = entry()
    assert store.write_component(original).saved
    new = replace(original, provenance=Provenance(NOW + timedelta(hours=1), NOW + timedelta(hours=1)))
    def fail_replace(*args):
        raise PermissionError("simulated disk failure")
    monkeypatch.setattr("weather_terminal.storage.os.replace", fail_replace)
    result = store.write_component(new)
    assert not result.saved and "in-memory" in result.issue
    assert new.provenance.retrieved_at == NOW + timedelta(hours=1)
    assert new.raw_body == original.raw_body
    assert store.read_component(LOCATION, Component.HOURLY).value == original
    assert not list(store.paths.cache_dir.rglob(".pending-*"))


def test_atomic_replacement_success_and_orphan_temp_ignored(tmp_path, store):
    cached = entry()
    assert store.write_component(cached).saved
    path = store.component_path(LOCATION, cached.component, cached.endpoint)
    (path.parent / ".pending-interrupted.tmp").write_text("bad", encoding="utf-8")
    changed = replace(cached, provenance=Provenance(NOW, NOW + timedelta(hours=1)))
    assert store.write_component(changed).saved
    assert store.read_component(LOCATION, Component.HOURLY).value == changed


def test_size_limits_and_failed_serialization_preserve_target(tmp_path, monkeypatch):
    path = tmp_path / "file.json"
    atomic_write_json(path, {"old": True})
    previous = path.read_bytes()
    monkeypatch.setattr("weather_terminal.storage.MAX_BYTES", 20)
    with pytest.raises(ValueError):
        atomic_write_json(path, {"big": "x" * 30})
    with pytest.raises(TypeError):
        atomic_write_json(path, {"bad": object()})
    assert path.read_bytes() == previous
    assert not list(tmp_path.glob(".pending-*"))


def test_unreadable_and_oversized_reads_are_safe(store, monkeypatch):
    seed_cache(store.paths.root)
    monkeypatch.setattr("weather_terminal.storage.MAX_BYTES", 20)
    assert store.read_selection().issues
    def denied(*args, **kwargs):
        raise PermissionError("simulated read failure")
    monkeypatch.setattr(Path, "open", denied)
    assert store.read_component(LOCATION, Component.DAILY, endpoint=DISCOVERY.daily_url).issues


def test_repetitive_diagnostics_are_bounded_and_do_not_contain_payload(store, caplog):
    store.paths.state_file.write_text('{"private":"secret-query"', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="weather_terminal"):
        for _ in range(10):
            assert store.read_selection().issues
    assert len(caplog.records) == 1
    assert "secret-query" not in caplog.text


def test_search_round_trip_normalized_and_provider_isolated(store):
    provider = "https://nominatim.openstreetmap.org/search"
    cached = CachedSearch(provider, "  OXFORD,   MS ", NOW, [{"display_name": "synthetic Oxford"}], (LOCATION,))
    assert store.write_search(cached).saved
    read = CacheStore(store.paths).read_search(provider, "oxford, ms").value
    assert read.query == "oxford, ms"
    assert read.retrieved_at == NOW and read.results == (LOCATION,)
    assert read.raw_body == cached.raw_body
    assert store.read_search("https://example.org/search", "Oxford, MS").value is None
    assert store.write_search(CachedSearch(provider, "no match", NOW, [], ())).saved
    assert store.read_search(provider, "Oxford, MS").value == read
    assert store.read_search(provider, "no match").value.results == ()


@pytest.mark.parametrize("query", ["", "   ", "x" * 201, "Oxford\x1b[31m", None])
def test_invalid_queries_rejected(query):
    with pytest.raises(ValueError):
        normalize_query(query)


def test_search_corruption_does_not_overwrite_existing_file(store):
    path = store.paths.cache_dir / "locations.json"
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    previous = path.read_bytes()
    cached = CachedSearch("https://nominatim.openstreetmap.org/search", "Oxford", NOW, [], ())
    assert store.read_search(cached.provider, cached.query).issues
    assert not store.write_search(cached).saved
    assert path.read_bytes() == previous


def test_schema_mismatch_selection_does_not_clear_component_files(store):
    seed_cache(store.paths.root)
    value = json.loads(store.paths.state_file.read_text(encoding="utf-8"))
    value["schema_version"] = 100
    store.paths.state_file.write_text(json.dumps(value), encoding="utf-8")
    assert store.read_selection().issues
    assert store.read_component(LOCATION, Component.HOURLY).value is not None


def test_fsync_failure_keeps_last_good_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    atomic_write_json(path, {"old": True})
    previous = path.read_bytes()
    def fail_sync(descriptor):
        raise OSError("simulated flush failure")
    monkeypatch.setattr("weather_terminal.storage.os.fsync", fail_sync)
    with pytest.raises(OSError):
        atomic_write_json(path, {"new": True})
    assert path.read_bytes() == previous
    assert not list(tmp_path.glob(".pending-*"))


def test_invalid_component_cannot_escape_cache_root(store):
    result = store.write_component(replace(entry(), component="../../outside"))
    assert not result.saved
    assert not store.paths.cache_dir.exists()


def test_single_bad_search_entry_does_not_block_another_query(store):
    from weather_terminal.storage import search_key
    provider = "https://nominatim.openstreetmap.org/search"
    first = CachedSearch(provider, "Oxford", NOW, [], (LOCATION,))
    second = CachedSearch(provider, "Memphis", NOW, [], ())
    assert store.write_search(first).saved and store.write_search(second).saved
    path = store.paths.cache_dir / "locations.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["entries"][search_key(provider, "Oxford")]["retrieved_at"] = "naive-or-invalid"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert store.read_search(provider, "Oxford").issues
    assert store.read_search(provider, "Memphis").value.results == ()


def test_future_source_timestamp_is_preserved_not_replaced_with_retrieval(store):
    cached = entry(provenance=Provenance(NOW, NOW, source_time=NOW + timedelta(minutes=1)))
    assert store.write_component(cached).saved
    assert store.read_component(LOCATION, Component.HOURLY).value.provenance.source_time == NOW + timedelta(minutes=1)


@pytest.mark.parametrize("field", ["etag", "last_modified"])
def test_cached_http_validator_controls_are_rejected(store, field):
    cached = entry()
    assert store.write_component(cached).saved
    path = store.component_path(LOCATION, cached.component, cached.endpoint)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["provenance"][field] = "bad\r\nInjected: header"
    path.write_text(json.dumps(document), encoding="utf-8")
    result = store.read_component(LOCATION, cached.component, endpoint=cached.endpoint)
    assert result.value is None and result.issues
