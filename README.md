# Weather Terminal

An ad-free, keyboard-driven NWS weather dashboard for Windows Terminal and Python 3.12.

**Status:** Phases 1–3 complete. Jake validated the Phase 4 station-pagination fix with clean compilation, **388 passed in 20.09s**, and a functioning live dashboard. The subsequent display-precision patch and its new regressions await execution. Remaining phase gates are described below; this is not finished-product acceptance.

**Author:** Element0  
**Prepared for:** Jake Jenkins

## Install and start

From the project directory in Windows Terminal:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

Skip venv creation if it already exists. In an activated venv, use `python main.py`. Direct interpreter use avoids PowerShell execution-policy changes. **No dependency changes for Phase 4.** Target: Windows 10/11, Python 3.12, Windows Terminal. Recommend 120×40; 80×24 stacks and scrolls, with long text wrapping. Smaller windows get a resize suggestion. Selectable table/layout refinement is Phase 5.

Only one copy runs per Windows logon session, even with different data directories. The OS releases the guard on exit/crash. The stylesheet and normal runtime root are independent of the working directory.

## Choose a city

First launch asks for a U.S. town/city: **Oxford, MS**, **Memphis, Tennessee**, or **Springfield**. Enter/Search submits once; typing never sends requests. A unique usable match goes through NWS support validation. Multiple matches require explicit choice: Tab to the selector, Enter to open, arrows/Enter to select, then Tab/Enter on **Use selected city**.

A supported selection becomes the active city even if an individual weather request subsequently fails. No cache for that city means unavailable/loading, not old-city weather. L opens search again; Escape/Cancel during pending lookup/support validation retains the previous dashboard. Once a supported commit has been accepted, it finishes persistence and in-memory identity together.

Search covers the **50 states and D.C.**, subject to NWS endpoint support. Territories, businesses, state/county-only results, and irrelevant features are excluded. No silent largest-city choice or substitute weather provider. A representative city point does **not** guarantee the same forecast/alerts across an entire municipality; coordinates are shown above the panels.

### External city lookup

[Nominatim's public service](https://operations.osmfoundation.org/policies/nominatim/) is a deliberate city-lookup dependency with limited donated capacity, not an unrestricted general lookup backend:

- Manual submission only, identifying User-Agent, cached queries, no confidential information or network autocomplete.
- At most one outgoing geocoder attempt per second across retries/concurrent requests.
- Configurable endpoint; no hidden provider fallback or periodic re-geocoding.
- **Location lookup: © OpenStreetMap contributors**, [license/attribution](https://www.openstreetmap.org/copyright).

Uncached submitted town/city text goes to the configured service. Repeated normalized queries—including empty results—reuse local cache. Saved coordinates are used directly at startup/weather refresh. Reset searches only while closed by removing `cache\locations.json`.

## What the dashboard shows

Numeric weather displays use one decimal place for temperatures and wind speeds, and whole numbers for percentages and wind direction. Rounding affects presentation only: source bodies, normalized numeric values, provenance, and cache schema are unchanged. Missing values remain distinct from zero; full official forecast/alert text is not reformatted.

### Current conditions

Nearby **station observations**, not forecast values passed off as measurements:

- Description, Fahrenheit temperature, feels-like when NWS supplies wind chill or heat index.
- Sustained wind, gusts, direction in degrees, relative humidity, dew point.
- Station name/ID, approximate distance from the city point, observation time.
- Download/check/attempt timestamps and separate source-age warnings.

Stations are ranked by calculated distance, not API list order. Check up to the nearest five (at most two probe operations concurrently; wire requests are conservatively serialized/paced). Prefer the nearest usable observation with temperature and a non-future timestamp no older than **90 minutes** by default. Otherwise show the newest usable past observation among those checked, with stale/fallback explanation. If none is usable, retain cached conditions or show unavailable—never substitute an hourly forecast.

Routine refresh reuses a working station. If stale/unusable, recheck other nearest candidates; refreshed station metadata also triggers re-evaluation. Matching cached station metadata may be used if its update fails, with a visible explanation. Metadata remains old, not falsely revalidated. Null optional values remain unavailable; invalid optional observation fields do not discard an otherwise usable temperature/time observation. Zero values remain zero.

### Next twelve hours

Up to twelve supplied hourly validity slots, including a partly elapsed current slot or the next supplied future one. Dates, local timezone/offset, °F, wind, description, and **Precip %** remain explicit. Precipitation probability is **not rainfall amount** and is not necessarily rain. Missing hours are not interpolated; fewer supplied periods and gaps are labeled. Twelve slots do not promise exactly twelve future elapsed hours.

### Seven-day outlook

Seven local calendar-date rows starting today, with daytime high, that date's **overnight forecast low**, separate day/night descriptions, and day/night precipitation probabilities. Local start date and `isDaytime` determine grouping; fourteen periods are not assumed to equal seven dates. Today's daytime period may be absent after it ends; the missing high stays `—`, never inferred from hourly data. Missing dates/periods say Unavailable. If the whole cache is expired, original dates remain under **OUTDATED DATA**, not relabeled as today.

**D** opens a scrollable read-only snapshot containing complete supplied day/night `detailedForecast` text for the displayed dates. Per-day selection comes in Phase 5.

### Official point alerts

All actual operational alerts returned by NWS, including watches, warnings, advisories—not only `Severe` records. Server-side point/zone matching is authoritative; null geometry is not a reason to drop an alert. Known Test/Exercise/Draft/System records are excluded. Unknown severity/urgency/certainty stay visible and labeled. Unknown status is not classified as operational and prevents a clean all-clear.

Official identifiers deduplicate updates; explicit Update/Cancel references supersede older records. A valid successful complete active set replaces the previous set. A malformed, partial, or failed response does not. Ordering is severity, urgency, then deterministic sent/identifier tie-breaking.

Each alert shows event/headline, official severity/urgency/certainty, area, effective/onset and expiration/ends. **A** opens a scrollable read-only snapshot with complete descriptions/instructions and supplied safe HTTPS source/API links. No instructions are paraphrased; no human-readable URL is manufactured. Individual alert selection is Phase 5 work.

**Safety and freshness:**

- Disk-restored alerts are always **current status unverified** until a successful network check.
- Any failed check immediately marks retained alerts unverified, regardless of how recent the previous check was.
- Known-expired entries say **Expired**, not current warnings. Expiration of all previously returned records does not itself produce a current all-clear.
- Cached/failed/stale empty sets say **No alerts at last check**, not an unconditional all-clear.
- Only a successful sufficiently recent operational empty set permits **No active alerts for this point**. The default successful-check threshold is three minutes.
- Header status stays visible while scrolling and reflects verified count versus **NOT VERIFIED**.
- Details are explicitly snapshots. Close/reopen to see newer source data; their text/status does not live-update while open.

This is a convenience dashboard, **not a guaranteed emergency-notification system**. Use official warning channels for safety-critical notification.

## Refresh and offline behavior

Load the remembered city and usable component caches first, then independently request all four components. No startup geocoding. A failed component retains its last good data and successful timestamps without blocking another component's commit.

| Component | Basic automatic interval |
|---|---:|
| Alerts | 60 seconds |
| Conditions | 5 minutes |
| Hourly/daily | 15 minutes each |
| Point/station metadata | 24 hours, on a dependent operation |
| Clock/age display | 1 second; no HTTP/disk |

**R** requests all four. Repeated requests coalesce per component. Alerts cannot send attempts more often than once every **30 seconds**, including retries, redirects, manual requests, and different city points in this process. Waiting for alert eligibility releases the provider lock so other eligible requests can proceed. HTTP deadlines/TLS/destination validation, bounded transient retries, and Retry-After apply. A manual attempt may wait or time out rather than bypass provider restrictions.

**Still deferred to Phase 6:** conditional GET/304 and HTTP freshness orchestration, extended failure cadences/next-retry presentation, and endpoint-404 rediscovery. Current requests are unconditional. This basic per-component timer layer is not the finished scheduler.

Cache reads never advance successful times, repair/delete files, or mix towns. Old dates stay old. Corrupt semantic/structural cache entries fail independently; usable in-memory data survives failed disk writes. Tests use synthetic weather only in temporary directories, not your normal cache. Internal networking-disabled tests keep legacy source previews; that seam is not a user-facing offline mode. Normal offline launches use the same semantic normalization as live data and show failed refresh/unverified status honestly.

## Controls

| Key | Behavior |
|---|---|
| L | Search/change city |
| R | Request all weather components, subject to coalescing/provider gates |
| A | Complete official alert text snapshot |
| D | Complete seven-date forecast text snapshot |
| ? | Help, meanings, paths, attribution/privacy |
| Q | Quit dashboard; close help/details when open |
| Ctrl+C | Quit, including while typing/searching/in details |
| Tab / Shift+Tab | Navigate controls |
| Enter | Submit, select city, activate button |
| Arrows / Page Up / Page Down | Navigate/scroll focused view |
| Escape | Close dialog/expanded selector; second Escape may close city dialog |

Input letters are ordinary text, not dashboard shortcuts. Network work is asynchronous; resize, scroll, cancel, and exit remain available. Remote text is plain text with control characters sanitized, not executable Rich markup.

## Times and local files

- **Retrieved:** complete body downloaded and validated.
- **Last checked:** successful network retrieval/revalidation. Current accepted 200 updates both; real 304 support is deferred.
- **Observed / forecast updated:** NWS source time, separate from download. Missing source time stays Unavailable.
- **Last attempted:** latest session attempt, including failure; never restored as a successful check.

UTC internally; selected point's IANA timezone on screen, with explicit UTC fallback. Repeated DST hours include offsets. Stale observation source age (90 minutes), conditions check age (15 minutes), alert check age (3 minutes), and forecast check age (45 minutes) are separate. Future times produce warnings. A successful download of a stale observation does not make the observation new.

```text
%LOCALAPPDATA%\WeatherTerminal\
    config.toml
    state.json
    cache\locations.json
    cache\<coordinate-key>\<component>\<endpoint-hash>.json
    logs\weather-terminal.log
```

Schema remains **v1**; observations retain raw source bodies and provenance, while station names/distances are rebuilt from station metadata (or ID/geometry fallback). Logs rotate at approximately 1 MiB with three backups. Unexpected diagnostics may include paths—review before sharing. Local files/backups disclose selected coordinates and cached queries.

**Close first and back up data you wish to retain.** Reset configuration by removing only `config.toml`; forget city by removing only `state.json`; reset weather by removing a damaged component file/directory; reset searches by removing only `cache\locations.json`. Search documents that are wholly corrupt are not automatically overwritten. `.pending-*.tmp` leftovers are ignored. No Windows lock-file deletion is needed. Do not reset normal data merely because a corruption test logs a warning.

Isolated live runtime:

```powershell
python main.py --data-dir "$env:TEMP\WeatherTerminalLiveCheck"
```

## Configuration

Created only if absent. Changes apply next launch. Invalid settings/unknown fields/unsafe headers produce readable errors. Existing configuration already contains all Phase 4 fields; no reset is necessary.

```toml
[refresh]
alerts_seconds = 60
conditions_seconds = 300
hourly_seconds = 900
daily_seconds = 900
metadata_seconds = 86400

[freshness]
observation_seconds = 5400
conditions_check_seconds = 900
alerts_check_seconds = 180
forecast_check_seconds = 2700

[http]
connect_timeout_seconds = 5
read_timeout_seconds = 15
operation_timeout_seconds = 60
user_agent = "WeatherTerminal/0.1 (personal terminal weather dashboard)"
contact = "" # Optional real operator contact.

[geocoding]
endpoint = "https://nominatim.openstreetmap.org/search"
min_interval_seconds = 1
```

All refresh settings now drive the basic layer. Minimums: geocoder 1 second, alerts 30 seconds, conditions/hourly/daily 60 seconds, metadata 3,600 seconds. Refresh/freshness maximum seven days; freshness minimum one second. Connect/read 1–120; operation 1–300 seconds and at least each individual timeout. HTTP operation limits apply to individual requests/retries; a bounded five-station evaluation can take longer than one request. User-Agent/contact printable ASCII ≤200 each. Geocoder HTTPS without credentials/query/fragment/whitespace; no foreign redirect. NWS requests stay on `https://api.weather.gov`.

## Tests and validation

```powershell
python -m pip check
python -m compileall -q main.py weather_terminal tests
python -m pytest -q
```

A new development venv also needs `requirements-dev.txt`. Mock tests forbid real httpx sends, use synthetic responses/fixed clocks/temporary paths, and do not require severe weather. Old hourly-only UI tests restrict their internal component seam; new full-component tests exercise production's four-component default.

**Evidence supplied by Jake:** Python 3.12.10; Phase 1 93 tests and interactive/single-instance checks; Phase 2 178 tests and interactive confirmation; Phase 3 clean compilation and **263 passed in 19.06s**, Oxford hourly screenshot, remembered restart/R refresh, offline recovery, ambiguity/cancellation and UI checks confirmed. Phase 4 baseline: clean dependency/compilation checks and **348 passed in 20.16s**. After the live station-pagination fix: clean compilation and **388 passed in 20.09s**, followed by Jake's confirmation that the app functions well with live current conditions. **The display-precision patch is authored, unrun.** Element0 cannot execute Python/shell commands in this session; the 388-test result predates that patch.

After the precision-patch suite is green, launch the remembered point (no repeat lookup) and check rounded temperatures/winds/percentages and unchanged missing-value labels. Remaining Phase 4 manual checks include full D text, A text when applicable, narrow scrolling, and offline restart/R recovery; failed alerts must become unverified immediately, including historical empty sets. Do not wait for severe weather or repeatedly search cities just to test fixtures.

Pins unchanged: Textual 8.2.8, httpx 0.28.1, tzdata 2026.5; pytest 9.1.1 and pytest-asyncio 1.4.0. [requirements-validated.txt](requirements-validated.txt) is the supplied freeze, not artifact hashes. Fresh-venv recreation remains Phase 7.

## Sources, privacy, troubleshooting

NWS receives selected point coordinates and station/weather requests; the geocoder receives uncached submitted city text. Identifying contact, if configured, goes to requested providers. No telemetry, ads, analytics, remote logging, IP/GPS lookup, silent fallback, or traffic after exit.

- [NWS API documentation](https://www.weather.gov/documentation/services-web-api)
- [NWS API OpenAPI schema](https://api.weather.gov/openapi.json)
- [NWS alerts service and request pacing](https://www.weather.gov/documentation/services-web-alerts)
- [Nominatim policy](https://operations.osmfoundation.org/policies/nominatim/), [search API](https://nominatim.org/release-docs/latest/api/Search/), [OSM attribution](https://www.openstreetmap.org/copyright)

| Symptom | Action |
|---|---|
| Missing package | Install using the same venv interpreter |
| No supported city | Add state; coverage/settlement restrictions apply |
| Station stale/unavailable | Check observed time and fallback notice; forecasts are not substitutes |
| A component fails | Last good body stays; inspect successful times/error and retry later |
| Alerts unverified | Consult official channels; cached empty/expired sets are not all-clear |
| Provider retry window closed | Respect Retry-After; R cannot bypass it |
| Storage warning | Other data stays usable; recover only damaged files while closed |
| Another copy running | Close it; do not delete a lock file |
| Old forecast dates | Intentional OUTDATED DATA, never rolled forward |
| Detail text old | Snapshot; close/reopen after dashboard refresh |
| Wrapping awkward | Enlarge window and report; table polish comes in Phase 5 |
| Unexpected failure | Review local log; share redacted error/environment |

## Project documents

- [ARCHITECTURE.md](ARCHITECTURE.md) — actual modules, state flow, safety, station/daily/alert rules.
- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) — phase gates and evidence.
- [weather-terminal-spec.md](weather-terminal-spec.md) — unchanged product requirements.

Phase 5 adds selectable tables/details/navigation; Phase 6 completes caching-aware refresh/recovery; Phases 7–8 cover Windows/integrated acceptance and handoff. Radar, metric switching, notifications, packaging, and maps remain out of scope.
