from datetime import datetime, timedelta, timezone

import pytest

from weather_terminal.models import (
    Component, ComponentState, ErrorKind, Location, OperationalError, Provenance, as_utc, parse_instant,
)

NOW = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)


def test_aware_instants_normalized_to_utc():
    value = datetime(2026, 1, 10, 6, tzinfo=timezone(timedelta(hours=-6)))
    assert as_utc(value) == NOW
    assert as_utc(value).tzinfo is timezone.utc
    assert parse_instant("2026-01-10T12:00:00Z") == NOW


@pytest.mark.parametrize("value", [datetime(2026, 1, 10), "2026-01-10", None])
def test_naive_or_non_datetime_rejected(value):
    with pytest.raises(ValueError):
        as_utc(value)


def test_naive_iso_rejected():
    with pytest.raises(ValueError):
        parse_instant("2026-01-10T12:00:00")


def test_provenance_rejects_check_before_retrieval():
    with pytest.raises(ValueError):
        Provenance(retrieved_at=NOW, last_checked_at=NOW - timedelta(seconds=1))


def test_provenance_normalizes_optional_source_time():
    offset = datetime(2026, 1, 10, 6, tzinfo=timezone(timedelta(hours=-6)))
    provenance = Provenance(offset, offset, source_time=offset)
    assert provenance.source_time.tzinfo is timezone.utc


def test_location_identity_is_coordinate_based():
    first = Location("Oxford", "Mississippi", 34.366, -89.519)
    alias = Location("Oxford", "MS", 34.366, -89.519)
    other = Location("Oxford", "Ohio", 39.5, -84.7)
    assert first.key == alias.key
    assert first.key != other.key
    assert first.label == "Oxford, Mississippi"


@pytest.mark.parametrize("lat,lon", [(91, 0), (0, -181), (float("nan"), 0), (0, True)])
def test_invalid_coordinates_rejected(lat, lon):
    with pytest.raises(ValueError):
        Location("Test", "MS", lat, lon)


def successful_state():
    state = ComponentState[list](Component.ALERTS, "city-key", generation=3)
    state.begin_attempt(NOW)
    state.accept_body(
        data=[], raw_body={"features": []}, endpoint="https://api.weather.gov/alerts/active",
        provenance=Provenance(NOW, NOW),
    )
    return state


def test_successful_empty_alert_set_is_data():
    state = successful_state()
    assert state.data == []
    assert state.raw_body == {"features": []}
    assert not state.in_flight
    assert state.last_attempted_at == NOW


def test_failure_retains_payload_and_successful_timestamps():
    state = successful_state()
    previous = state.provenance
    body = state.raw_body
    state.loaded_from_disk = True
    state.begin_attempt(NOW + timedelta(minutes=1))
    state.fail(OperationalError(ErrorKind.NETWORK, "Connection timed out", retryable=True))
    assert state.provenance is previous
    assert state.raw_body is body
    assert state.data == []
    assert state.loaded_from_disk
    assert not state.in_flight
    assert state.last_attempted_at == NOW + timedelta(minutes=1)
    assert state.error is not None


def test_304_advances_only_successful_check_time():
    state = successful_state()
    checked = NOW + timedelta(minutes=1)
    state.begin_attempt(checked)
    state.accept_not_modified(checked)
    assert state.provenance.retrieved_at == NOW
    assert state.provenance.last_checked_at == checked
    assert state.data == []
    assert not state.in_flight


def test_304_requires_usable_body():
    state = ComponentState(Component.ALERTS, "city-key", generation=1)
    with pytest.raises(ValueError, match="cached body"):
        state.accept_not_modified(NOW)
    assert state.provenance is None


def test_304_rejects_backwards_time():
    state = successful_state()
    previous = state.provenance
    with pytest.raises(ValueError, match="backwards"):
        state.accept_not_modified(NOW - timedelta(seconds=1))
    assert state.provenance is previous


def test_attempt_does_not_hide_previous_error():
    state = successful_state()
    error = OperationalError(ErrorKind.PROVIDER, "Provider unavailable")
    state.fail(error)
    state.begin_attempt(NOW + timedelta(minutes=1))
    assert state.error is error
    with pytest.raises(RuntimeError, match="already in progress"):
        state.begin_attempt(NOW)


def test_location_generation_check():
    state = successful_state()
    assert state.belongs_to("city-key", 3)
    assert not state.belongs_to("different-city", 3)
    assert not state.belongs_to("city-key", 4)
