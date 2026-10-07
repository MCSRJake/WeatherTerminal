from copy import deepcopy
from datetime import timedelta

import pytest

from weather_terminal.config import FreshnessSettings
from weather_terminal.hourly import normalize_hourly, render_hourly, select_hours, temperature_f
from weather_terminal.models import Provenance
from weather_terminal.services.http import ServiceFailure
from tests.phase2_helpers import NOW
from tests.phase3_helpers import hourly_body


def test_contains_partly_elapsed_hour_and_selects_twelve_slots():
    forecast = normalize_hourly(hourly_body())
    hours, notice = select_hours(forecast, NOW + timedelta(minutes=40))
    assert len(hours) == 12 and hours[0].start == NOW
    assert not notice


def test_nulls_real_zeros_and_quantitative_values():
    body = hourly_body(1)
    period = body["properties"]["periods"][0]
    period.update(temperature={"value": 0, "unitCode": "wmoUnit:degC"},
                  windSpeed={"value": 0, "unitCode": "wmoUnit:m_s-1"})
    forecast = normalize_hourly(body)
    assert forecast.periods[0].temperature_f == 32
    assert forecast.periods[0].precipitation_percent == 0
    assert forecast.periods[0].wind == "N 0.0 mph"
    period.update(temperature=None, probabilityOfPrecipitation={"value": None})
    missing = normalize_hourly(body).periods[0]
    assert missing.temperature_f is None and missing.precipitation_percent is None


@pytest.mark.parametrize("value,unit,expected", [(0,"F",0), (0,"C",32), (273.15,"K",32)])
def test_temperature_units(value, unit, expected):
    assert temperature_f(value, unit) == pytest.approx(expected)


def test_gap_fewer_periods_and_expired_dates_are_honest():
    body = hourly_body(3)
    body["properties"]["periods"].pop(1)
    forecast = normalize_hourly(body)
    hours, notice = select_hours(forecast, NOW)
    assert len(hours) == 2 and "gaps" in notice and "Only 2" in notice
    _, notice = select_hours(forecast, NOW - timedelta(hours=2))
    assert "No period covers now" in notice
    hours, notice = select_hours(forecast, NOW + timedelta(days=2))
    assert "OUTDATED DATA" in notice and hours[0].start == NOW
    text = render_hourly(forecast, Provenance(NOW, NOW), NOW + timedelta(days=2), "America/Chicago",
                         FreshnessSettings(), cached=True, error="simulated outage")
    assert "2026-01-10" in text and "Refresh failed" in text
    assert "Retrieved: 2026-01-10 06:00:00 CST -0600" in text


@pytest.mark.parametrize("change", [
    {"temperature": True}, {"temperatureUnit": "unknown"}, {"temperature": float("nan")},
    {"endTime": "2026-01-10T11:00:00Z"}, {"startTime": "2026-01-10T12:00:00"},
    {"probabilityOfPrecipitation": {"value": 120}}, {"windSpeed": {"value": 1, "unitCode": "unknown"}},
])
def test_invalid_periods_reject_entire_replacement(change):
    body = hourly_body(1)
    body["properties"]["periods"][0].update(change)
    with pytest.raises(ServiceFailure):
        normalize_hourly(body)


def test_duplicate_and_overlapping_periods_are_rejected():
    body = hourly_body(2)
    body["properties"]["periods"][1] = deepcopy(body["properties"]["periods"][0])
    with pytest.raises(ServiceFailure):
        normalize_hourly(body)


def test_empty_coverage_is_unavailable_not_invented():
    hours, notice = select_hours(normalize_hourly(hourly_body(0)), NOW)
    assert not hours and "Unavailable" in notice


def test_remote_text_is_plain_and_control_sequences_removed():
    body = hourly_body(1)
    body["properties"]["periods"][0]["shortForecast"] = "[red]unsafe\x1b[2J"
    forecast = normalize_hourly(body)
    text = render_hourly(forecast, Provenance(NOW, NOW), NOW, None, FreshnessSettings(), cached=False)
    assert "[red]" in text and "\x1b" not in text


def test_dst_repeated_hours_keep_distinct_offsets():
    body = hourly_body(2)
    for period, start, end in zip(body["properties"]["periods"],
                                ["2026-11-01T01:00:00-05:00", "2026-11-01T01:00:00-06:00"],
                                ["2026-11-01T01:00:00-06:00", "2026-11-01T02:00:00-06:00"]):
        period.update(startTime=start, endTime=end)
    from weather_terminal.models import parse_instant
    instant = parse_instant("2026-11-01T06:00:00Z")
    text = render_hourly(normalize_hourly(body), Provenance(instant, instant), instant, "America/Chicago", FreshnessSettings(), cached=False)
    assert "CDT -0500" in text and "CST -0600" in text


@pytest.mark.parametrize("wind,expected", [("16.09344 km/h", "10.0 mph"), ("0 to 16.09344 km/h", "0.0 to 10.0 mph")])
def test_scalar_wind_units_and_ranges_are_converted_to_mph(wind, expected):
    body = hourly_body(1)
    body["properties"]["periods"][0]["windSpeed"] = wind
    assert expected in normalize_hourly(body).periods[0].wind


def test_negative_quantitative_wind_cannot_be_presented_as_valid_mph():
    body = hourly_body(1)
    body["properties"]["periods"][0]["windSpeed"] = {"value": -1, "unitCode": "wmoUnit:m_s-1"}
    with pytest.raises(ServiceFailure):
        normalize_hourly(body)


@pytest.mark.parametrize("cached", [False, True])
def test_hourly_display_precision_keeps_numeric_data_and_raw_body(cached):
    body = hourly_body(2)
    first, second = body["properties"]["periods"]
    first.update(temperature={"value": 29.19494444444444, "unitCode": "wmoUnit:degC"},
                 windSpeed={"value": 2.57, "unitCode": "wmoUnit:m_s-1"},
                 probabilityOfPrecipitation={"value": 35.2872})
    second.update(temperature=None, windSpeed=None,
                  probabilityOfPrecipitation={"value": None})
    original_body = deepcopy(body)
    data = normalize_hourly(body)
    text = render_hourly(data, Provenance(NOW, NOW), NOW, None, FreshnessSettings(), cached=cached)
    assert " | 84.6 | 35% | N 5.7 mph | Synthetic clear" in text
    assert " | — | — | N Unavailable | Synthetic clear" in text
    assert data.periods[0].temperature_f == pytest.approx(84.5509)
    assert data.periods[0].precipitation_percent == 35.2872
    assert body == original_body


@pytest.mark.parametrize("wind,expected", [
    ({"value": 2.57, "unitCode": "wmoUnit:m_s-1"}, "N 5.7 mph"),
    ("2.57 m/s", "N 5.7 mph"),
    ("2.57 to 4.12 m/s", "N 5.7 to 9.2 mph"),
    ("5.74893 mph", "N 5.7 mph"),
    ("Calm", "N Calm"),
    ("Light and variable", "N Light and variable"),
])
def test_forecast_wind_precision_handles_quantities_scalars_ranges_and_text(wind, expected):
    body = hourly_body(1)
    body["properties"]["periods"][0]["windSpeed"] = wind
    assert normalize_hourly(body).periods[0].wind == expected


def test_hourly_display_zero_temperature_and_probability_are_not_missing():
    body = hourly_body(1)
    body["properties"]["periods"][0]["temperature"] = 0
    data = normalize_hourly(body)
    text = render_hourly(data, Provenance(NOW, NOW), NOW, None, FreshnessSettings(), cached=False)
    assert " | 0.0 | 0% | N 0.0 mph | Synthetic clear" in text
