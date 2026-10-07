from copy import deepcopy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from weather_terminal.config import FreshnessSettings
from weather_terminal.daily import normalize_daily, select_days, render_daily
from weather_terminal.models import Provenance
from weather_terminal.services.http import ServiceFailure
from tests.phase2_helpers import NOW
from tests.phase4_helpers import daily_body


def test_seven_local_dates_group_day_and_night_with_zero_probability():
    data = normalize_daily(daily_body())
    days, note = select_days(data, NOW, "America/Chicago")
    assert len(days) == 7 and not note
    assert days[0].date.isoformat() == "2026-01-10" and days[-1].date.isoformat() == "2026-01-16"
    assert all(d.daytime.temperature_f == 50 and d.nighttime.temperature_f == 20 for d in days)
    assert days[0].daytime.precipitation_percent == 0
    assert "Second line retained." in days[0].nighttime.detailed


def test_missing_todays_daytime_is_not_inferred_and_uncovered_dates_unavailable():
    body = daily_body(4)
    body["properties"]["periods"].pop(0)
    data = normalize_daily(body)
    days, _ = select_days(data, NOW + timedelta(hours=12), "America/Chicago")
    assert days[0].daytime is None and days[0].nighttime.temperature_f == 20
    assert days[-1].daytime is None and days[-1].nighttime is None
    text = render_daily(data, Provenance(NOW, NOW), NOW + timedelta(hours=12), "America/Chicago", FreshnessSettings())
    assert "Sat 2026-01-10 | — | 20.0 |" in text and "Unavailable" in text
    assert "Overnight low" in text and "not inferred" in text


def test_midnight_in_location_not_machine_zone_and_utc_fallback():
    data = normalize_daily(daily_body())
    instant = datetime(2026, 1, 11, 1, tzinfo=timezone.utc)
    assert select_days(data, instant, "America/Chicago")[0][0].date.isoformat() == "2026-01-10"
    assert select_days(data, instant, "Invalid/Zone")[0][0].date.isoformat() == "2026-01-11"


@pytest.mark.parametrize("first", [datetime(2026, 3, 7, 6, tzinfo=ZoneInfo("America/Chicago")),
                                   datetime(2026, 10, 31, 6, tzinfo=ZoneInfo("America/Chicago"))])
def test_dst_day_night_rows_follow_local_start_dates(first):
    data = normalize_daily(daily_body(start=first))
    days, _ = select_days(data, first, "America/Chicago")
    assert len(days) == 7
    for i, day in enumerate(days):
        assert day.date == first.date() + timedelta(days=i)
        assert day.daytime and day.nighttime


def test_expired_forecast_retains_original_dates_instead_of_today():
    data = normalize_daily(daily_body(4))
    later = NOW + timedelta(days=60)
    days, notice = select_days(data, later, "America/Chicago")
    assert days[0].date.isoformat() == "2026-01-10" and "OUTDATED DATA" in notice
    text = render_daily(data, Provenance(NOW, NOW), later, "America/Chicago", FreshnessSettings(), cached=True)
    assert "2026-01-10" in text and "Forecast coverage expired" in text


@pytest.mark.parametrize("value", [[], None, "bad"])
def test_empty_or_invalid_period_sets(value):
    body = daily_body()
    body["properties"]["periods"] = value
    if value == []:
        days, notice = select_days(normalize_daily(body), NOW, "America/Chicago")
        assert len(days) == 7 and all(d.daytime is None for d in days) and "Unavailable" in notice
    else:
        with pytest.raises(ServiceFailure):
            normalize_daily(body)


@pytest.mark.parametrize("field,value", [("isDaytime", "true"), ("endTime", NOW.isoformat()),
    ("temperature", True), ("temperatureUnit", "unknown"), ("probabilityOfPrecipitation", {"value": 101}),
    ("detailedForecast", {})])
def test_invalid_required_fields_do_not_normalize(field, value):
    body = daily_body()
    body["properties"]["periods"][0][field] = value
    with pytest.raises(ServiceFailure):
        normalize_daily(body)


def test_duplicate_date_type_cannot_be_used_even_if_intervals_do_not_overlap():
    body = daily_body(2)
    body["properties"]["periods"][1]["isDaytime"] = True
    with pytest.raises(ServiceFailure):
        select_days(normalize_daily(body), NOW, "America/Chicago")


def test_quantitative_temperature_missing_values_and_remote_plain_text():
    body = daily_body(2)
    day, night = body["properties"]["periods"]
    day["temperature"] = {"value": 0, "unitCode": "wmoUnit:degC"}
    night["temperature"] = None
    day["shortForecast"] = "[red]literal[/red]\x1b[2J"
    data = normalize_daily(body)
    assert data.periods[0].temperature_f == 32 and data.periods[1].temperature_f is None
    assert "[red]" in data.periods[0].short and "\x1b" not in data.periods[0].short


@pytest.mark.parametrize("cached", [False, True])
def test_daily_display_precision_preserves_high_low_probabilities_and_source(cached):
    body = daily_body(2)
    day, night = body["properties"]["periods"]
    day.update(temperature={"value": 29.19494444444444, "unitCode": "wmoUnit:degC"},
               probabilityOfPrecipitation={"value": 35.2872})
    night.update(temperature=-12.36, probabilityOfPrecipitation={"value": 65.7872})
    original_body = deepcopy(body)
    data = normalize_daily(body)
    text = render_daily(data, Provenance(NOW, NOW), NOW, "America/Chicago", FreshnessSettings(), cached=cached)
    assert "Sat 2026-01-10 | 84.6 | -12.4 | Synthetic day / Synthetic night | 35% / 66%" in text
    assert "Sun 2026-01-11 | — | — | Unavailable / Unavailable | — / —" in text
    assert data.periods[0].temperature_f == pytest.approx(84.5509)
    assert data.periods[0].precipitation_percent == 35.2872
    assert data.periods[1].temperature_f == -12.36
    assert body == original_body


def test_daily_display_zero_is_distinct_from_missing():
    body = daily_body(2)
    day, night = body["properties"]["periods"]
    day["temperature"] = 0
    night.update(temperature=None, probabilityOfPrecipitation={"value": None})
    text = render_daily(normalize_daily(body), Provenance(NOW, NOW), NOW, "America/Chicago", FreshnessSettings())
    assert "Sat 2026-01-10 | 0.0 | — | Synthetic day / Synthetic night | 0% / —" in text
