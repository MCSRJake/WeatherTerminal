"""Clearly synthetic Phase 4 station/observation/daily/alert data; never public traffic."""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from weather_terminal.models import Location
from tests.phase2_helpers import LOCATION, NOW
from tests.phase3_helpers import HOURLY, POINT, POINT_BODY, hourly_body, reply

DAILY = POINT_BODY["properties"]["forecast"]
STATIONS = POINT_BODY["properties"]["observationStations"]
ALERTS = f"https://api.weather.gov/alerts/active?point={LOCATION.latitude},{LOCATION.longitude}"


def station_feature(identifier="SYN1", lat=34.37, lon=-89.52):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"@id": f"https://api.weather.gov/stations/{identifier}",
                           "stationIdentifier": identifier, "name": "Synthetic station " + identifier}}


def stations_body(count=3):
    return {"features": [station_feature(f"SYN{i + 1}", LOCATION.latitude + .01 * (i + 1), LOCATION.longitude) for i in reversed(range(count))]}


def obs_url(identifier="SYN1"):
    return f"https://api.weather.gov/stations/{identifier}/observations/latest"


def observation_body(identifier="SYN1", *, when=NOW, temp=0):
    q = lambda value, unit: {"value": value, "unitCode": "wmoUnit:" + unit}
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-89.52, 34.37]},
            "properties": {"station": f"https://api.weather.gov/stations/{identifier}", "timestamp": when.isoformat(),
                           "textDescription": "Synthetic clear observation", "temperature": q(temp, "degC"),
                           "dewpoint": q(None, "degC"), "windChill": q(None, "degC"), "heatIndex": q(None, "degC"),
                           "relativeHumidity": q(0, "percent"), "windSpeed": q(0, "m_s-1"),
                           "windGust": q(None, "m_s-1"), "windDirection": q(0, "degree_(angle)" )}}


def daily_body(count=14, *, start=None):
    zone = ZoneInfo("America/Chicago")
    first = start or datetime(2026, 1, 10, 6, tzinfo=zone)
    rows = []
    for i in range(count):
        s, e = first + timedelta(hours=12 * i), first + timedelta(hours=12 * (i + 1))
        rows.append({"startTime": s.isoformat(), "endTime": e.isoformat(), "isDaytime": i % 2 == 0,
                     "temperature": 50 if i % 2 == 0 else 20, "temperatureUnit": "F",
                     "shortForecast": "Synthetic day" if i % 2 == 0 else "Synthetic night",
                     "detailedForecast": f"Full synthetic official forecast {i}.\nSecond line retained.",
                     "probabilityOfPrecipitation": {"value": 0, "unitCode": "wmoUnit:percent"}})
    return {"properties": {"updateTime": NOW.isoformat(), "periods": rows}}


def alert_feature(identifier="synthetic-alert", *, severity="Moderate", urgency="Expected", status="Actual", sent=NOW,
                  expires=None, message="Alert", references=None):
    return {"type": "Feature", "id": "https://api.weather.gov/alerts/" + identifier, "geometry": None,
            "properties": {"id": identifier, "@id": "https://api.weather.gov/alerts/" + identifier,
                           "status": status, "messageType": message, "sent": sent.isoformat(),
                           "effective": sent.isoformat(), "onset": None, "ends": None,
                           "expires": (expires or NOW + timedelta(hours=2)).isoformat(),
                           "event": "Synthetic advisory", "headline": "Synthetic headline", "severity": severity,
                           "urgency": urgency, "certainty": "Likely", "areaDesc": "Synthetic county",
                           "description": "Full synthetic official description.\nKeep all lines.",
                           "instruction": "Full synthetic official instructions.\nDo not paraphrase.",
                           "references": references or []}}


class RoutingTransport:
    """Per-URL queues; concurrent components do not depend on scheduling order."""
    def __init__(self, clock, routes):
        self.clock = clock
        self.routes = defaultdict(list, {url: list(replies) for url, replies in routes.items()})
        self.calls = []
        self.closed = False

    async def request(self, url, *, headers, params=None):
        self.calls.append((url, self.clock.monotonic(), dict(headers), params))
        if not self.routes[url]:
            raise AssertionError("Unexpected synthetic request: " + url)
        result = self.routes[url].pop(0)
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return await result(url)
        assert result.url == url
        return result

    async def aclose(self):
        self.closed = True


def successful_routes(*, alerts=None):
    return {POINT: [reply(POINT, deepcopy(POINT_BODY))], HOURLY: [reply(HOURLY, hourly_body())],
            DAILY: [reply(DAILY, daily_body())], STATIONS: [reply(STATIONS, stations_body())],
            ALERTS: [reply(ALERTS, {"features": [] if alerts is None else alerts})],
            **{obs_url(f"SYN{i}"): [reply(obs_url(f"SYN{i}"), observation_body(f"SYN{i}"))] for i in range(1, 4)}}
