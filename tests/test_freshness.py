from datetime import datetime, timedelta, timezone

import pytest

from weather_terminal.config import FreshnessSettings
from weather_terminal.freshness import (
    alert_expiry, assess_freshness, forecast_coverage, format_time, source_intervals, state_labels,
)
from weather_terminal.models import Component, ErrorKind, OperationalError, Provenance
from weather_terminal.offline import render_cached_component, restore_component
from tests.phase2_helpers import NOW, entry, fixture_bodies

SETTINGS = FreshnessSettings()


def test_dst_fall_back_repeated_hours_are_distinguishable():
    first = datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)
    second = first + timedelta(hours=1)
    a, b = format_time(first, "America/Chicago"), format_time(second, "America/Chicago")
    assert "2026-11-01 01:30:00 CDT -0500" == a
    assert "2026-11-01 01:30:00 CST -0600" == b


def test_dst_spring_forward_does_not_invent_missing_hour():
    before = datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)
    after = before + timedelta(hours=1)
    assert "01:30:00 CST -0600" in format_time(before, "America/Chicago")
    assert "03:30:00 CDT -0500" in format_time(after, "America/Chicago")


def test_midnight_is_location_local_not_machine_local():
    utc = datetime(2026, 1, 11, 5, 59, tzinfo=timezone.utc)
    assert format_time(utc, "America/Chicago").startswith("2026-01-10 23:59")
    assert format_time(utc + timedelta(minutes=1), "America/Chicago").startswith("2026-01-11 00:00")


@pytest.mark.parametrize("zone", [None, "Invalid/Zone", "../unsafe"])
def test_explicit_utc_fallback(zone):
    assert "UTC +0000" in format_time(NOW, zone)
    if zone:
        assert "timezone unavailable" in format_time(NOW, zone)
    assert format_time(None, zone) == "Unavailable"


@pytest.mark.parametrize("component,threshold", [
    (Component.CONDITIONS, 900), (Component.ALERTS, 180),
    (Component.HOURLY, 2700), (Component.DAILY, 2700),
])
def test_verification_threshold_is_independent_of_retrieval(component, threshold):
    provenance = Provenance(NOW - timedelta(days=2), NOW, source_time=NOW)
    assert not assess_freshness(component, provenance, NOW + timedelta(seconds=threshold), SETTINGS).verification_stale
    assert assess_freshness(component, provenance, NOW + timedelta(seconds=threshold + 1), SETTINGS).verification_stale


def test_recent_download_and_304_do_not_make_old_observation_new():
    provenance = Provenance(NOW, NOW, source_time=NOW - timedelta(hours=2))
    cached = entry(Component.CONDITIONS, provenance=provenance)
    state = restore_component(cached, generation=2, normalize=lambda body: body["properties"])
    assert "Cached" in state_labels(state, NOW, SETTINGS)
    assert assess_freshness(state.component, provenance, NOW, SETTINGS).observation_stale
    state.begin_attempt(NOW + timedelta(minutes=1))
    state.accept_not_modified(NOW + timedelta(minutes=1))
    assert state.provenance.source_time == provenance.source_time
    assert state.provenance.retrieved_at == NOW
    assert assess_freshness(state.component, state.provenance, NOW + timedelta(minutes=1), SETTINGS).observation_stale


@pytest.mark.parametrize("source", [None, NOW + timedelta(minutes=1)])
def test_missing_or_future_source_not_treated_as_fresh_observation(source):
    report = assess_freshness(Component.CONDITIONS, Provenance(NOW, NOW, source), NOW, SETTINGS)
    assert report.observation_stale is None
    assert any("unverified" in warning for warning in report.warnings)


def test_future_success_timestamps_are_not_silently_current():
    report = assess_freshness(Component.ALERTS, Provenance(NOW + timedelta(minutes=1), NOW + timedelta(minutes=1)), NOW, SETTINGS)
    assert report.verification_stale
    assert any("system clock" in warning for warning in report.warnings)


def test_failure_notice_is_immediate_before_age_threshold():
    cached = entry(Component.ALERTS, body={"features": []})
    state = restore_component(cached, generation=1, normalize=lambda body: body["features"])
    state.begin_attempt(NOW + timedelta(seconds=1))
    state.fail(OperationalError(ErrorKind.NETWORK, "Timeout"))
    labels = state_labels(state, NOW + timedelta(seconds=1), SETTINGS)
    assert "Refresh failed" in labels and "Cached" in labels
    assert state.provenance == cached.provenance
    assert state.last_attempted_at == NOW + timedelta(seconds=1)


def test_containing_interval_next_future_and_expired_original_dates():
    intervals = source_intervals(fixture_bodies()["hourly"])
    containing = forecast_coverage(intervals, NOW + timedelta(minutes=30))
    assert containing.status == "available"
    assert len(containing.selected) == 2
    assert containing.selected[0].start == NOW
    future = forecast_coverage(intervals, NOW - timedelta(days=1))
    assert future.status == "gap"
    expired = forecast_coverage(intervals, NOW + timedelta(days=2))
    assert expired.status == "expired"
    assert expired.selected == intervals
    assert expired.selected[0].start.date().isoformat() == "2026-01-10"
    assert forecast_coverage((), NOW).status == "unavailable"


def test_coverage_never_fabricates_slots_and_handles_internal_gap():
    intervals = source_intervals(fixture_bodies()["hourly"])
    gapped = (intervals[0], intervals[2])
    result = forecast_coverage(gapped, NOW + timedelta(minutes=30))
    assert result.status == "gap"
    assert result.selected == (intervals[2],)
    assert len(forecast_coverage(intervals, NOW, limit=1).selected) == 1
    with pytest.raises(ValueError):
        forecast_coverage(intervals, NOW, limit=0)


@pytest.mark.parametrize("expires,expected", [
    ("2026-01-10T11:00:00Z", "Expired"), ("2026-01-10T12:00:00Z", "Expired"),
    ("2026-01-10T13:00:00Z", "Not known expired; current status unverified"),
    (None, "Expiration unavailable; current status unverified"),
    ("2026-01-10T13:00:00", "Expiration invalid; current status unverified"),
    ("bad", "Expiration invalid; current status unverified"),
])
def test_alert_expiry_is_not_current_status(expires, expected):
    assert alert_expiry({"expires": expires}, NOW) == expected


def test_cached_empty_alerts_are_never_an_all_clear_even_when_recent():
    cached = entry(Component.ALERTS, body={"features": []})
    text = render_cached_component(cached, NOW, SETTINGS, None)
    assert "No alerts at last check" in text
    assert "NOT a current all-clear" in text
    assert "No active alerts for this point" not in text


def test_preview_preserves_dates_and_sanitizes_text():
    cached = entry()
    cached.raw_body["properties"]["periods"][1]["shortForecast"] = "[red]SYNTHETIC[/red]\x1b[31m"
    text = render_cached_component(cached, NOW + timedelta(days=1), SETTINGS, "America/Chicago")
    assert "Forecast coverage expired" in text and "OUTDATED DATA" in text
    assert "2026-01-10" in text
    assert "[red]" in text  # Literal text, not Rich markup; widgets use markup=False.
    assert "\x1b" not in text
