"""Synthetic Phase 3 data and deterministic transport; never contacts providers."""
from copy import deepcopy
from datetime import timedelta
import json

from weather_terminal.config import AppConfig, RuntimePaths
from weather_terminal.services.http import HttpReply, HttpService
from weather_terminal.slice import WeatherSlice
from weather_terminal.storage import CacheStore
from tests.phase2_helpers import LOCATION, NOW
from tests.test_app import FakeClock

GEO = AppConfig().geocoding.endpoint
POINT = "https://api.weather.gov/points/34.3660,-89.5190"
HOURLY = "https://api.weather.gov/gridpoints/MEG/1,1/forecast/hourly"
POINT_BODY = {"properties": {"timeZone": "America/Chicago", "forecastHourly": HOURLY,
                            "forecast": "https://api.weather.gov/gridpoints/MEG/1,1/forecast",
                            "observationStations": "https://api.weather.gov/gridpoints/MEG/1,1/stations"}}


def candidate(name="Oxford", state="Mississippi", lat="34.366", lon="-89.519", county="Lafayette County"):
    return {"name": name, "category": "boundary", "type": "administrative", "addresstype": "city",
            "lat": lat, "lon": lon, "address": {"country_code": "us", "state": state, "county": county}}


def hourly_body(count=15):
    return {"properties": {"updateTime": (NOW - timedelta(minutes=10)).isoformat(), "periods": [
        {"startTime": (NOW + timedelta(hours=i)).isoformat(),
         "endTime": (NOW + timedelta(hours=i + 1)).isoformat(), "temperature": 32 + i,
         "temperatureUnit": "F", "shortForecast": "Synthetic clear", "windDirection": "N",
         "windSpeed": "0 mph", "probabilityOfPrecipitation": {"value": 0, "unitCode": "wmoUnit:percent"}}
        for i in range(count)]}}


def reply(url, body=None, status=200, headers=None):
    return HttpReply(status, headers or {}, json.dumps(body).encode(), url)


class ScriptTransport:
    def __init__(self, clock, script):
        self.clock, self.script = clock, list(script)
        self.calls = []
        self.closed = False

    async def request(self, url, *, headers, params=None):
        self.calls.append((url, dict(headers), params, self.clock.monotonic()))
        if not self.script:
            raise AssertionError("Unexpected synthetic request: " + url)
        result = self.script.pop(0)
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return await result(url)
        assert result.url == url
        return result

    async def aclose(self):
        self.closed = True


def slice_for(root, script):
    clock = FakeClock()
    transport = ScriptTransport(clock, script)
    http = HttpService(AppConfig(), clock=clock, transport=transport)
    controller = WeatherSlice(AppConfig(), CacheStore(RuntimePaths(root)), http)
    return controller, transport, clock
