"""Submit-only U.S. settlements; no autocomplete or periodic geocoding."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
from typing import Any

from weather_terminal.models import ErrorKind, Location
from weather_terminal.services.http import HttpService, ServiceFailure, decode_json
from weather_terminal.storage import CacheStore, CachedSearch, normalize_query

# Fifty states plus D.C.; territories deliberately excluded.
STATE_NAMES = """Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|District of Columbia|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming""".split("|")
STATE_CODES = "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
STATES = dict(zip((s.casefold() for s in STATE_NAMES), STATE_CODES))
SETTLEMENTS = {"city", "town", "village", "hamlet", "municipality", "borough", "locality"}


def parse_candidates(body: Any) -> tuple[Location, ...]:
    if not isinstance(body, list) or len(body) > 40 or any(not isinstance(item, dict) for item in body):
        raise ServiceFailure(ErrorKind.INVALID_RESPONSE, "Location provider returned an invalid result list.")
    results: list[Location] = []
    for item in body:
        address = item.get("address")
        if (not isinstance(address, dict) or not isinstance(address.get("country_code"), str)
                or address["country_code"].lower() != "us"):
            continue
        category = item.get("category", item.get("class"))
        kind = item.get("addresstype", item.get("type"))
        if (not isinstance(category, str) or not isinstance(kind, str)
                or category not in {"place", "boundary"} or kind not in SETTLEMENTS):
            continue  # County/state-only, businesses, and arbitrary addresses are not cities.
        state_name = address.get("state", "")
        code = STATES.get(str(state_name).casefold())
        iso = address.get("ISO3166-2-lvl4", "")
        if code is None and isinstance(iso, str) and iso.startswith("US-") and iso[3:] in STATE_CODES:
            code = iso[3:]
        if code is None:
            continue
        name = item.get("name") or next((address.get(key) for key in ("city", "town", "village", "hamlet", "municipality", "borough") if address.get(key)), None)
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            continue
        county = address.get("county")
        if county is not None and (not isinstance(county, str) or len(county) > 200):
            county = None
        try:
            if isinstance(item.get("lat"), bool) or isinstance(item.get("lon"), bool):
                continue
            location = Location(name.strip(), code, float(item["lat"]), float(item["lon"]), county=county)
        except (ValueError, TypeError, KeyError):
            continue
        # A boundary and a place node often represent the same settlement.
        # Only collapse same label/county within ~1 km; distinct towns stay choices.
        if any(previous.name.casefold() == location.name.casefold() and previous.state == location.state
               and (previous.county or "").casefold() == (location.county or "").casefold()
               and math.hypot((previous.latitude - location.latitude) * 111,
                              (previous.longitude - location.longitude) * 111 * math.cos(math.radians(location.latitude))) < 1
               for previous in results):
            continue
        results.append(location)
    return tuple(results)


@dataclass(frozen=True)
class SearchResult:
    candidates: tuple[Location, ...]
    cached: bool
    issues: tuple[str, ...] = ()


class Geocoder:
    def __init__(self, http: HttpService, store: CacheStore) -> None:
        self.http, self.store = http, store
        self.lock = asyncio.Lock()  # Serialize submissions; recheck cache after waiting.

    async def search(self, query: str) -> SearchResult:
        try:
            query = normalize_query(query)
        except ValueError:
            raise ServiceFailure(ErrorKind.MISSING_DATA, "Enter a town/city name (up to 200 ordinary text characters).") from None
        provider = self.http.config.geocoding.endpoint
        async with self.lock:
            cached = await asyncio.to_thread(self.store.read_search, provider, query)
            if cached.value is not None:
                # Reapply current filtering to raw results, rather than blindly
                # trusting normalized locations from an older software version.
                try:
                    return SearchResult(parse_candidates(cached.value.raw_body), True, cached.issues)
                except ServiceFailure:
                    pass
            reply = await self.http.get(provider, provider="geocoder", params={
                "q": query, "format": "jsonv2", "countrycodes": "us",
                "addressdetails": "1", "accept-language": "en", "limit": "10",
                "featureType": "settlement", "dedupe": "1",
            })
            raw = decode_json(reply)
            candidates = parse_candidates(raw)
            saved = await asyncio.to_thread(self.store.write_search, CachedSearch(
                provider, query, self.http.clock.now(), raw, candidates,
            ))
            return SearchResult(candidates, False, cached.issues + (() if saved.saved else (saved.issue,)))
