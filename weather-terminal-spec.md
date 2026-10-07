# Weather Terminal — Version 1 Specification

**Author:** Element0, Elemental Informatics  
**Prepared for:** Jake Jenkins  
**Date:** October 7, 2026  
**Status:** Implementation-ready specification; program not yet implemented

## 1. Purpose and agreed requirements

Build an advertisement-free, continuously updating weather dashboard that runs in Windows Terminal using Python 3.12 and Textual. The user runs `python main.py` from a virtual environment. This is a personal application, not a distributed product; no executable packaging, application server, subscription, or cloud account is required.

The user enters a U.S. town or city name and sees:

1. Current conditions from a nearby reporting station.
2. The next twelve hours of forecast conditions.
3. Official active weather alerts affecting the selected forecast point.
4. A seven-day forecast.

All **weather observations, forecasts, and alerts must come from the National Weather Service (NWS)**. City-name lookup may use a separate geocoding service because the NWS API accepts coordinates and does not provide name-to-coordinate lookup.

When a request fails, the application retains the last successfully received data, including across restarts. Every panel makes its retrieval time visible. Cached, outdated, missing, or unverified information must not look freshly retrieved.

### Version 1 scope

- One active location at a time, with the last selected location remembered.
- Town/city search and explicit selection when a name is ambiguous.
- Automatic refresh, manual refresh, persistent cache, and graceful recovery.
- U.S. customary units: Fahrenheit, mph, miles, percent.
- Dark, keyboard-driven Textual interface with scrolling and responsive layout.
- Full official alert text accessible from the dashboard.

### Out of scope

Radar, maps, international forecasts, historical graphs, personal weather stations, favorite-location management, IP/GPS location detection, Windows notifications, sounds, background operation after exit, and standalone executable packaging. Snapshot/CLI-only output and metric-unit switching are also deferred.

## 2. Platform and application structure

### Supported environment

- Python 3.12 on Windows 10/11.
- Windows Terminal is the supported terminal host. PowerShell or Command Prompt may be used as the shell inside it.
- Internet access is needed for new data and uncached location searches, but not to display previously cached information.
- Recommended terminal size: at least 120 columns by 40 rows. At 80 columns by 24 rows, all features remain available through a stacked, scrollable layout. Smaller windows show an unobtrusive resize suggestion without crashing.

### Libraries

| Library | Role |
|---|---|
| Textual | Full-screen terminal UI, controls, scrolling, timers, background workers |
| httpx | Asynchronous HTTP requests and connection reuse |
| tzdata | IANA timezone database for Python `zoneinfo`, including on Windows |
| pytest and pytest-asyncio | Development/test dependencies |

Use standard-library dataclasses, JSON, `tomllib`, `pathlib`, logging, and `zoneinfo` where practical. A separate geocoding library is unnecessary; use the HTTP client directly. Pin compatible dependency versions after implementation and testing rather than guessing version numbers in this specification.

### Separation of responsibilities

- **UI:** Textual screens/widgets; no API parsing or HTTP requests embedded in rendering logic.
- **Services:** NWS and geocoding clients; request policy and endpoint discovery.
- **Models/normalization:** Typed internal representations, units, timestamps, station selection, forecast grouping, alert ordering.
- **Storage:** Configuration, remembered location, persistent response/component cache, atomic writes.
- **Refresh coordinator:** Per-component schedules, retry/backoff, cancellation, and state changes.

Suggested implementation layout:

```text
main.py
requirements.txt
requirements-dev.txt
README.md
weather_terminal/
    app.py
    models.py
    config.py
    storage.py
    refresh.py
    services/
        nws.py
        geocoding.py
    widgets/
        location.py
        conditions.py
        hourly.py
        daily.py
        alerts.py
    styles.tcss
tests/
    fixtures/
```

The entry point starts the dashboard and reports actionable startup/configuration problems. This document does not create any of the implementation files above.

## 3. Location entry and resolution

### User experience

1. On first launch, show a location-entry dialog. Examples: `Oxford, MS`, `Memphis, Tennessee`, or `Springfield`.
2. Search only when the user submits the completed query. **No network-backed autocomplete or requests on every keystroke.**
3. Display matching settlements with town/city, state, and county when available. Deduplicate obvious duplicate representations of the same place.
4. If several plausible matches remain, require the user to select one. Never silently choose the largest city or a particular state.
5. A unique usable result may be accepted directly, with its resolved name prominently shown.
6. Resolve coordinates through NWS `/points`. A place without supported forecast endpoints gets a clear coverage message, not weather for a substitute location.
7. Save the confirmed selection and associated coordinates. Do not assume Oxford is the default.

`L` opens the location dialog. Escape cancels and keeps the current location. A failed search or unsupported selection must not rename the existing dashboard while leaving its old weather underneath.

### Geographic boundary

Version 1 searches the 50 states and District of Columbia. U.S. territories are deferred. Coverage is ultimately determined by NWS endpoint availability; recognize and explain unsupported locations rather than promising complete coverage everywhere.

A town/city lookup supplies a representative point, not an exact street address. Forecasts and alerts apply to that point, **not necessarily every part of a large city**. State this briefly in help and show coordinates in a location/details view.

### Proposed geocoder: OpenStreetMap Nominatim

Use the public Nominatim search service for this single-user, manually initiated city lookup, subject to its [usage policy](https://operations.osmfoundation.org/policies/nominatim/). This is a non-weather dependency and should be identified as such in the UI/help.

**Important operating restrictions:** the public service has limited donated capacity, permits at most one request per second, requires an identifying User-Agent, requires suitable OpenStreetMap attribution, disallows client-side autocomplete, and expects caching and the ability to change providers. It is not an unrestricted general-purpose lookup backend. Recheck its policy before implementation; using it is a deliberate provider choice, not an invisible dependency.

Implementation requirements:

- Submit HTTPS requests to the configured search endpoint with `countrycodes=us`, an explicit JSON output format, English results, address details, and a small result limit, such as ten.
- Restrict/filter results to towns, cities, villages, and comparable inhabited places; exclude businesses and irrelevant geographic features. Do not accept a state or county alone as a city match.
- Enforce at least one second between all outgoing geocoder attempts, including retries, across this application process. This personal app is intended to run as one instance; multiple simultaneous instances must not defeat the provider's aggregate limit.
- Cache normalized query results locally. Repeated searches reuse the cache; no periodic geocoding during weather refresh or startup for saved coordinates.
- Set a descriptive application User-Agent; allow a real operator contact to be added in configuration. Never invent an email or web address.
- Display `Location lookup: © OpenStreetMap contributors` in the location dialog and provide [the attribution/license link](https://www.openstreetmap.org/copyright) in help.
- Make the geocoder base URL configurable without editing code; isolate it behind a service interface.
- Do not send confidential addresses or personal information. Explain that submitted place names go to the external lookup service.
- If the geocoder is unavailable, keep the current dashboard usable; cached searches may still work. Do not silently switch providers.

## 4. NWS integration and data meaning

### Request discovery

Start with `https://api.weather.gov/points/{latitude},{longitude}`. Follow the returned `forecast`, `forecastHourly`, and `observationStations` URLs. Use the returned location timezone. Do not assume grid coordinates or station choices remain permanent.

| Purpose | Endpoint/use |
|---|---|
| Point metadata | `/points/{latitude},{longitude}` |
| Daily day/night periods | Discovered `forecast` URL |
| Hourly periods | Discovered `forecastHourly` URL |
| Candidate observation stations | Discovered `observationStations` URL |
| Current station observation | `/stations/{stationId}/observations/latest` |
| Active alerts for selected point | `/alerts/active?point={latitude},{longitude}` |

Use an identifying User-Agent and appropriate JSON/GeoJSON Accept headers. NWS currently documents free public access without an API key and unpublished rate limits; do not assume that policy can never change. Follow HTTPS redirects within the expected NWS API service. Do not scrape NWS web pages or unrelated weather websites.

### Current conditions

Show, when supplied:

- Conditions description.
- Temperature and feels-like temperature.
- Sustained wind direction/speed and gusts.
- Relative humidity and dew point.
- Observation timestamp.
- Station name/identifier and approximate distance from the forecast point.

Observations are from a reporting station, not guaranteed conditions at the exact town center. Calculate station distance from coordinates; do not assume the API's station list is distance-sorted.

**Station selection:** rank candidate stations by distance and check up to the nearest five, with bounded concurrency. Prefer the nearest station with a temperature and an observation no more than 90 minutes old. If none qualifies, use the most recent timestamped, usable observation among those checked, label it stale when applicable, and explain the limitation. If no usable observation is available, retain cached conditions or show unavailable; never use a forecast temperature as an unlabeled observation. Reuse a working station on routine refresh and re-evaluate when it becomes unusable/stale or station metadata is refreshed.

Prefer NWS wind chill for feels-like when available, otherwise heat index; if neither is supplied, show `Unavailable`. Do not present a made-up apparent-temperature estimate as NWS data. Unavailable optional fields do not invalidate an otherwise usable observation.

Parse units from NWS unit identifiers and forecast field shapes rather than assuming everything is Fahrenheit/mph. Support documented scalar and quantitative-value forecast representations as needed by the current API. A null value is missing, not zero. Zero wind, zero precipitation probability, and zero temperatures remain valid values.

### Next twelve hours

Show twelve hourly periods starting with the period containing the current instant, or the next future period if there is no current one. Include:

- Local date/time, with date separators across midnight.
- Temperature.
- Short conditions description.
- Precipitation probability, explicitly labeled `Precip %`.
- Wind direction/speed.

Use forecast validity intervals, not array offsets alone. The containing hour may have already partially elapsed; document that this is twelve hourly slots rather than a guarantee of exactly twelve future elapsed hours. Display fewer periods, with an explanation, if the source provides fewer. Do not interpolate or fabricate missing hours.

A percentage is probability of precipitation, not rainfall amount. Do not label it only as "rain" because the forecast may involve snow or other precipitation. Hourly forecasts must not be presented as current observations.

### Seven-day outlook

Show one row for each of seven local calendar dates, starting today, using NWS day/night forecast periods:

- Day/date.
- Daytime forecast high, if available.
- That date's overnight forecast low from its nighttime period, if available.
- Separate concise daytime and nighttime conditions.
- Daytime/nighttime precipitation probabilities when provided.

Group by each period's local start date and `isDaytime` value. Clearly label the low as the overnight forecast, not an observed minimum for that calendar day. Do not treat fourteen periods as automatically equivalent to seven dates or infer a missing high from hourly data. Today's daytime period may be absent after it ends; show `—` for the high rather than inventing one. Dates beyond available coverage show `Unavailable`. Full NWS `detailedForecast` text should be accessible in a selected-day details view.

### Alerts

The alert panel covers official active watches, warnings, advisories, and other returned actual alerts, not just items whose severity happens to equal `Severe`.

- Query by the selected point; do not substitute a statewide feed or discard valid alerts because their geometry is null. The NWS service handles point/zone matching.
- Exclude Test/Exercise/Draft/System status messages; show actual operational alerts.
- Display event name, headline, official severity, urgency, certainty, affected area, onset/effective time, and expiration when supplied.
- Deduplicate by official identifier and replace superseded versions appropriately. A successfully retrieved active set is authoritative for the current list.
- Order by official severity, then urgency, with deterministic tie-breaking. Unknown values remain visible and explicitly labeled; never drop them because a mapping is missing.
- Allow selection of each alert and scrolling through its complete official description and instructions. Do not rewrite safety instructions or summarize away important content.
- Use text labels as well as color. Red/amber should draw attention without making every advisory look like a tornado warning.
- Provide the official source/API link supplied by NWS where available. Do not manufacture a human-readable alert URL.

**Safety:** cached alerts are previously retrieved alerts, not proof that they are still active. On refresh failure, retain them under `Previously retrieved alerts — current status unverified`, with retrieval/expiration times. Known-expired cached entries must be labeled expired and not presented as current warnings. A cached empty set means `No alerts at last check`, not an unconditional current all-clear. Only a successful, sufficiently recent empty response permits `No active alerts for this point`.

Help must state: this is a convenience dashboard, **not a guaranteed emergency-notification system**; use official warning channels for safety-critical notification. Point-based coverage does not guarantee coverage across the entire named municipality.

## 5. Interface and controls

### Default layout

```text
 WEATHER TERMINAL                      Selected town, State
 Local time / timezone                 Overall refresh status

 ALERTS                                [details available]
 Retrieved: date/time | Last checked: date/time | status

 CURRENT CONDITIONS                    NEXT 12 HOURS
 temperature, conditions               time | temp | precip | wind
 feels-like, wind/gusts, humidity       ...
 station + observation time            ...
 retrieved/checked times                retrieved/checked times

 NEXT 7 DAYS
 day/date | high | overnight low | day/night conditions | precip
 retrieved/checked times + forecast update time

 [L] Location  [R] Refresh  [A] Alerts  [D] Day details
 [?] Help  [Q] Quit                     NWS weather data
```

At wide sizes, current conditions and hourly forecast are side by side. At narrower sizes, stack them. Permit vertical scrolling and horizontal scrolling or a compact hourly view where necessary. No essential information is permanently lost offscreen. Alerts stay near the top; a compact alert-count/status indicator remains visible in the header when the body scrolls.

Use readable dark styling with restrained accent colors. Do not rely on emoji or weather-icon font support; words and ordinary symbols carry the information. Escape remote text before rendering so it cannot inject Rich markup or terminal control sequences.

### Controls

| Key | Action |
|---|---|
| `L` | Enter/change location |
| `R` | Request refresh, subject to provider throttling |
| `A` | Open alert list/details |
| `D` | Open selected-day/full forecast details |
| `?` | Help, data definitions, sources/attribution |
| `Q` or `Ctrl+C` | Exit cleanly |
| Tab / Shift+Tab | Navigate controls |
| Arrows / Page Up / Page Down | Navigate and scroll appropriate views |
| Enter | Submit/select/open |
| Escape | Close modal/cancel |

Letter shortcuts must not steal normal typing while the location input has focus. Selecting a day/alert must be possible entirely by keyboard. Mouse support is welcome but not required.

Network work runs in asynchronous workers without blocking typing, scrolling, resizing, or exit. On location changes, cancel obsolete tasks and also check a location-generation token before applying results. A late response for the previous city must never populate the new city's panels.

## 6. Refresh, freshness, and persistent cache

### Default schedule

| Component | Normal refresh/revalidation interval |
|---|---:|
| Alerts | 60 seconds |
| Current observation | 5 minutes |
| Hourly forecast | 15 minutes |
| Daily forecast | 15 minutes |
| Point/grid and station-list metadata | 24 hours; also on relevant endpoint failure |
| UI clock and age labels | Every second, without network traffic |

Load cached data immediately at startup, then fetch/revalidate due components. Components refresh independently; a failed station observation must not block alerts or erase a successful forecast update. Avoid overlapping requests for the same component.

Honor HTTP caching headers and validators, including ETag/Last-Modified when supplied. A still-valid HTTP cache entry does not need another request just because a UI timer fires. Conditional requests may receive `304 Not Modified`; preserve the body and update its successful validation time. Do not use cache-busting query parameters.

Manual refresh requests all components but must not bypass rate limits, Retry-After, or in-progress work. In particular, never query the alerts service more often than once every 30 seconds, even through repeated key presses. Show throttling/queued status rather than silently flooding the API.

### Required time metadata, per component

- **Retrieved at:** when a complete response body was successfully received and validated.
- **Last checked at:** latest successful network retrieval/revalidation, including a valid 304 response.
- **Source time:** observation timestamp or forecast update/generation timestamp, where supplied; alert event times remain separate.
- **Last attempted at:** latest attempt, successful or not.
- **Last error:** actionable summary, if the latest attempt failed.

Never advance `retrieved_at` merely because a timer fired, disk cache was read, a request failed, or a 304 validated an unchanged body. Never equate retrieval freshness with observation freshness. Show explicit dates and times for persistent-cache data, not only relative labels such as "five minutes ago."

Store timestamps as timezone-aware UTC values and render them in the selected location's IANA timezone, using `zoneinfo` and `tzdata`. Across daylight-saving transitions, repeated local hours must include an offset/zone indicator sufficient to distinguish them. If point timezone metadata is unavailable, retain known cached metadata or display explicit UTC; never silently assume the computer's timezone is the location's timezone.

### Status and age rules

Use distinct states such as `Loading`, `Current`, `Cached`, `Refresh failed`, `Stale`, and `Unavailable`. "Current" means recently checked, not that NWS observations are instantaneous.

Default warnings:

- Observation source time older than 90 minutes: `Observation stale` even after successful retrieval.
- Conditions not successfully checked for 15 minutes: retrieval/cache freshness warning.
- Alerts not successfully checked for 3 minutes: `Alert status unverified/stale`; any failed current check immediately shows the failure/unverified notice.
- Forecasts not successfully checked for 45 minutes: retrieval/cache freshness warning.
- Forecast periods no longer covering the present: `Forecast coverage expired` regardless of recent retrieval.

Thresholds are configurable. They must not suppress an immediate failed-refresh notice. A successful revalidation of an old observation does not make the observation new.

### Cache design

Store data under `%LOCALAPPDATA%\WeatherTerminal\`, independent of the working directory:

```text
config.toml
state.json
cache/
    locations.json
    <location-key>/...
logs/
    weather-terminal.log
```

- Separate component payloads and metadata, keyed by location coordinates and endpoint identity; never share data between towns accidentally.
- Include a cache schema version. Retain enough source data and forecast intervals to rebuild panels correctly; do not cache only terminal-rendered text.
- Preserve last good data until a replacement is validated. Write temporary files and atomically replace destination files.
- An unavailable/corrupt/incompatible component cache does not prevent other panels from loading. Explain the problem and attempt normal retrieval.
- Cache write failure does not destroy the in-memory dashboard; notify and log it.
- Successfully retrieved empty alert lists are valid data and replace the previous active list. HTTP errors and malformed responses do not.
- Retain last good data during long outages rather than deleting it solely due to age. Label it honestly.

Render still-applicable forecast periods from cached data using their actual dates/times. If no currently applicable forecast coverage remains, retain the last available forecast view under an explicit outdated-data heading. Never roll old dates forward, relabel yesterday as "Today," or invent a new twelve-hour window. Include dates prominently whenever cached content spans another day.

Switching locations loads only that location's cache; absent cache means loading/unavailable panels, not another city's retained weather.

## 7. Failure handling, configuration, and privacy

### HTTP behavior

- Reuse an asynchronous client, with TLS certificate verification enabled.
- Default connect timeout: 5 seconds; read timeout: 15 seconds. Bound the full request/retry operation so it cannot remain pending indefinitely.
- Retry only transient connection failures, rate limiting, and selected server errors, at most twice per operation with jittered backoff. Honor Retry-After and schedule longer waits without freezing the UI.
- Do not repeatedly retry invalid queries, authorization failures, unsupported points, or schema-invalid successful responses.
- After repeated failures, extend scheduled retries up to five minutes for alerts and fifteen minutes for other weather components. Resume normal cadence after recovery; show next retry status.
- On grid-endpoint not-found responses, refresh `/points` once and rediscover URLs before declaring persistent failure. Do not loop indefinitely.
- Distinguish `provider unavailable`, `network error`, `unsupported location`, `missing data`, and `invalid response` where possible. Do not diagnose the entire Internet as down from one failing endpoint.
- Expected operational errors produce readable status messages, not a traceback in place of the dashboard.

Validate responses before replacing cached data. Handle unknown extra fields and missing optional fields. Required structural failures should retain the previous valid component and log a diagnostic.

### Configuration

Create a documented `config.toml` with defaults. Provide settings for refresh intervals, freshness thresholds, timeouts, identifying User-Agent/contact, and geocoder endpoint. Reject invalid values or values that violate minimum provider request spacing. Remember the selected location in `state.json`, not by modifying the user's configuration on every selection.

No API secrets are required by the initially selected services. There is no telemetry, advertising, analytics, remote logging, or hidden IP-based location request. The NWS receives selected coordinates; the geocoder receives submitted town/city text. Local cache/state can reveal selected locations, which should be documented.

Use a bounded rotating local log. Log component failures and diagnostics, not complete alert/forecast payloads or unnecessary search history. Do not put contact details in publicly shared examples. Help should offer the paths for configuration, cache, and logs.

### Installation documentation

The eventual README must provide:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

Using the venv interpreter directly avoids requiring PowerShell execution-policy changes. With an activated venv, `python main.py` works as requested. Document keyboard controls, cache behavior, service attribution/policy, limitations, and test execution.

## 8. Acceptance criteria and testing

Version 1 is complete when these behaviors are demonstrated:

1. **Launch and selection:** starts on Python 3.12 in Windows Terminal; first launch asks for a town/city; later launches remember the confirmed location.
2. **Ambiguity:** a query such as `Springfield` offers distinct city/state choices rather than silently selecting one. City-plus-state queries resolve correctly when available.
3. **Required display:** current conditions, twelve hourly periods where supplied, seven dated daily rows, and official point alerts are available.
4. **Provenance:** station identity/distance and observation time are visible; every weather panel shows retrieval and last-check times. Forecast update times are distinguished from retrieval times.
5. **Outage after success:** simulate failures after a successful retrieval. Last valid data stays visible with its original retrieval times and a failed/stale status; other successful panels continue updating.
6. **Offline restart:** with an existing cache, launch without network access and display the saved location/data with explicit cached/offline indicators. With no cache, show a usable location/error screen rather than crashing.
7. **Alert safety:** failed alert requests never become an all-clear. Cached alerts become unverified, expired cached alerts are clearly expired, and successful empty responses correctly clear the active list.
8. **Refresh correctness:** automatic schedules and manual refresh work without duplicate requests or policy violations. Valid 304 responses update check time but not body-retrieval/source times.
9. **Location race:** switching towns while a slow request is pending cannot mix location labels and weather data.
10. **Units/nulls:** Celsius/meter-per-second observations convert correctly; nulls show unavailable, while real zeros are preserved. Gusts and sustained wind remain distinct.
11. **Time boundaries:** midnight, DST changes, partially elapsed hours, and missing daytime periods group correctly. Cached expired forecasts retain their original dates.
12. **UI:** keyboard-only operation, normal input typing, resize handling, full alert/day details, scrolling, and clean exit work at wide and 80×24 sizes.
13. **Storage resilience:** corrupt cache, incompatible schema, failed writes, and malformed responses do not erase unrelated usable data or crash the app.
14. **Lookup policy:** searches occur only on submit, cached queries avoid new calls, outgoing attempts obey the one-second spacing, and attribution is present.

Use deterministic mocked HTTP responses and fixed clocks for automated tests. Cover independent endpoint failures, timeouts, 429/Retry-After, 5xx, 404 grid rediscovery, conditional requests, station fallbacks, empty/malformed payloads, alert updates, and forecast shapes. Use Textual's test facilities for UI navigation and slow-worker/race tests. Automated tests must not depend on live provider availability or repeatedly query public APIs.

Perform a small manual live smoke test during implementation using Oxford, MS and at least one location in another timezone. Validate alert behavior with fixtures rather than waiting for actual severe weather. Verify complete workflow on the user's Windows environment.

## 9. Implementation sequence

1. Establish dependency pins, models, configuration, storage, and mock fixtures.
2. Implement deliberate city lookup and NWS point/endpoint discovery.
3. Implement weather clients, normalization, station selection, and per-component metadata.
4. Build the Textual layout, details screens, keyboard navigation, and responsive behavior.
5. Add independent refresh schedules, HTTP caching, persistent startup cache, and failure recovery.
6. Complete automated acceptance tests, README, and Windows/live smoke checks.

No additional user decision is required before starting. The specified geocoder, refresh intervals, units, layout, and freshness thresholds are first-version defaults; they can be revisited without changing the agreed product scope.

## 10. Sources and references

Checked October 7, 2026. These are implementation references, not a claim that the application has already been built or tested.

- [NWS API documentation](https://www.weather.gov/documentation/services-web-api) — API access, User-Agent, endpoints, discovery, caching, and observation limitations.
- [NWS API general FAQs](https://weather-gov.github.io/api/general-faqs) — coordinate lookup, absence of geocoding, hourly forecasts, HTTP caching, and identifying requests.
- [NWS API OpenAPI definition](https://api.weather.gov/openapi.json) — authoritative endpoint/parameter/schema reference to check during implementation.
- [NWS alerts web service documentation](https://www.weather.gov/documentation/services-web-alerts) — active point alerts, CAP concepts, and recommended request spacing.
- [NWS alerts geolocation guide](https://www.weather.gov/media/documentation/docs/NWS_Geolocation.pdf) — polygon, county, and forecast-zone considerations; consult when validating alert coverage.
- [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/) — mandatory service restrictions, identification, caching, attribution, and provider-switching requirements.
- [Nominatim search API](https://nominatim.org/release-docs/latest/api/Search/) — query parameters, country restriction, result details, and place filtering.
- [OpenStreetMap attribution and license](https://www.openstreetmap.org/copyright) — attribution requirements for geocoding data.
- [Textual getting started](https://textual.textualize.io/getting_started/) — Python/platform requirements and Windows Terminal support.
- [Textual workers guide](https://textual.textualize.io/guide/workers/) — responsive asynchronous work, cancellation, errors, and result-ordering considerations.
