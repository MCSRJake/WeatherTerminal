# Weather Terminal — Architecture

**Author:** Element0, Elemental Informatics  
**Prepared for:** Jake Jenkins  
**Status:** Phases 1–3 complete; Phase 4 station-pagination fix validated by Jake, display-precision patch awaiting validation.

The [specification](weather-terminal-spec.md) governs product behavior; the [plan](IMPLEMENTATION_PLAN.md) governs validation. Jake supplied Phase 3 clean compilation, **263 passed in 19.06s**, Oxford hourly display, and then confirmed remembered restart/R refresh, offline retention/recovery, and interactive ambiguity/cancellation checks. For the Phase 4 baseline, Jake supplied clean dependency checks/compilation and **348 passed in 20.16s**, but live conditions failed station-list validation. His PowerShell diagnostic confirmed 28 returned stations plus an ID-filtered `pagination.next` link. After the narrow redundant-pagination exception, Jake supplied clean compilation and **388 passed in 20.09s**, then confirmed the app functions well with live conditions. The subsequent display-precision patch and its new regressions are authored, unrun. No Python execution tool exists in this session; remaining manual/integrated phase gates are not implied complete.

## 1. Actual boundaries and modules

```text
main.py                         Entry/config/owned logging/instance guard
weather_terminal/
    app.py                      Textual panels, city dialog, owned operations, source-text snapshots
    styles.tcss                 Module-anchored responsive dark stylesheet
    clock.py                    UTC/monotonic/sleep seam
    config.py                   Typed defaults/validation and runtime paths
    models.py                   Identity/discovery/provenance/component state
    storage.py                  Schema-v1 raw-body atomic storage and identities
    freshness.py                Source/check/coverage/expiry/timezone helpers
    offline.py                  Selection snapshot, source reads, legacy isolated-test previews
    units.py                    Shared numbers/temperature/quantity/percentage conversion
    formatting.py               Display-only precision, missing values and negative-zero handling
    hourly.py                   Hourly normalization/current interval selection/rendering
    conditions.py               Stations/distances/observation normalization/selection/rendering
    daily.py                    Day-night periods/local-date grouping/rendering
    alerts.py                   Official identity/reference ordering, verification, complete text
    slice.py                    Supported selection and independent component commits
    runtime.py                  Named-object guard and logging-session ownership
    text.py                     Remote control-character sanitation
    services/
        http.py                 Lazy httpx, TLS/destination/deadline/retry/pacing
        geocoding.py            Submit-only cached settlement lookup
        nws.py                  Point/daily/hourly/station/observation/alert adapters
        observations.py         Bounded nearest-five evaluation and working-station reuse
tests/
    conftest.py                  Real httpx sends forbidden
    phase2_helpers.py            Temp synthetic cache seeds
    phase3_helpers.py            Hourly-only scripts/fake transport
    phase4_helpers.py            Clearly synthetic multi-URL routing responses
    fixtures/phase2-source.json  Historical test-only source bodies
    test_*.py                   Unit, persistence, policy, concurrency and Textual regressions
```

All constructors/imports are side-effect free. Weather HTTP runs only after startup/submission. `LocationPreview` retains its historical name but is the actual city dialog. Internal `network_enabled=False` preserves isolated legacy preview tests; normal offline execution is not that seam. `enabled_components=(HOURLY,)` keeps narrow historical integration tests focused, while production defaults to all four. New full-component tests exercise that production default.

Phase 4 now wires all four components into basic plain-text panels and whole-set/source-text snapshots. Selectable tables, individual alert/day selection, location details, final presentation and accessibility are Phase 5. Conditional cache orchestration, extended retry schedules, next-retry UI and grid-404 rediscovery are Phase 6. No finished-product acceptance is implied by wiring a feature.

## 2. Lifecycle and independent operations

1. Entry resolves `%LOCALAPPDATA%\WeatherTerminal`, acquires the logon-session guard, owns rotating diagnostics, creates absent configuration and validates it.
2. Textual composes shell, loads selected point/source bodies off the event loop, constructs the lazy HTTP/coordinator boundary, and semantically restores each valid component independently. Station metadata is normalized before observations. Reads never alter successful times.
3. No selection: await modal mount before startup readiness. Saved selection: render cache immediately, start four owned component tasks. No startup geocoding.
4. Basic timers: alerts 60s, conditions 300s, hourly/daily 900s. Metadata refreshes on dependent operations after 24h. One-second display tick recomputes source/verification age, dates, expiry and header with no I/O.
5. `start_component` coalesces its in-flight operation. `start_refresh` launches/joins all enabled components without turning them into one HTTP transaction. One failed expected operation returns false; another component's validated result can still commit. Alert operations are independent of point/forecast/station metadata.
6. Supported city commit increments generation and installs only its cache/states. Replacement refresh cancels/joins obsolete component operations. Generation checks guard all component/metadata applications and writes, even if transport ignores cancellation.
7. Unmount marks shutdown, invalidates intent, cancels/joins owned tasks (including station probes), closes HTTP, then entry closes logging/releases guard.

`LocationPreview.lookup_task` owns only submission/support work. Never assign Textual's read-only `task` or internal `_task`: they belong to its message pump. Application tracked `tasks`, `component_tasks` and aggregate `refresh_task` are separate from framework ownership. Unexpected background errors are collected/reported, never silently lost. Expected errors update their component/dialog rather than replacing the dashboard with traceback.

## 3. Selection, metadata and commit safety

Query normalization/cache, 50 states/D.C. settlement filtering/deduplication, explicit ambiguity selection, and configurable geocoder remain unchanged. No keypress/autocomplete/bulk traffic, confidential address lookup, periodic geocoding, or provider fallback. Canceling search may retain query-result cache but does not commit location.

`WeatherSlice.confirm` obtains an intent ticket, resolves fresh cached or live `/points`, validates discovered forecast/station families, constructs a supported selection, loads only its caches, then commits under mutation lock. Failed/unsupported/canceled pending selection cannot rename old weather. Support success can commit even if an individual forecast later fails.

- Selection includes label/coordinates/discovery together. Exact coordinate key stays independent of endpoint hash; point queries round to four decimals only on the wire.
- `metadata_lock` coalesces point/station rediscovery between weather operations. Mutation lock serializes actual accepted memory/disk updates. Metadata refresh does **not** replace the whole live snapshot and erase independently updated weather.
- Already-dispatched atomic writes are shielded/awaited while mutation lock is held; cancellation cannot race a new commit against an abandoned filesystem write. An accepted supported selection completes its memory/disk commit together before propagating cancellation.
- Before response application/persistence, closed/generation checks reject old operations. Last-good data for the current point may survive endpoint changes until a validated replacement arrives, but exact discovered endpoints govern cache restoration. No cross-city cache fallback.
- Write failure retains new usable memory plus storage warning; next launch may restore older disk state. Semantic cache failure drops only that component from usable memory, without deleting files.
- Discovery destinations/families/point identity are verified. Timezone fallback is saved known location metadata or explicit UTC, not OS local time.

## 4. HTTP and pacing

`HttpService` lazily owns reusable `httpx.AsyncClient` through transport seam. TLS verify enabled, redirects automatic-follow disabled, streaming response ≤8 MiB. Redirect destinations are checked before requesting (max three); operation/connect/read defaults 60/5/15s. Complete operation deadline includes waits/locks/retries/hops. NWS destination is HTTPS `api.weather.gov`, default/443 only; geocoder is configured HTTPS host/path/port. Descriptive User-Agent and optional real contact; no invented contact. Strict JSON rejects duplicate/nonfinite data; adapters validate semantics before replacement.

Pacing uses monotonic deadlines and provider lock:

- Geocoder ≥1 second between attempts, including retries and redirects.
- NWS conservative one-second wire spacing, an app choice rather than a claimed published global limit.
- **All alert paths** additionally share a process-wide ≥30-second deadline, across points/retries/manual calls/redirects. Reserve eligibility before wire attempt, including failed/canceled attempts.
- Provider lock is released while awaiting eligibility and rechecked on wake. Waiting for the alert-specific deadline does not reserve the entire provider for 30 seconds; eligible forecasts/observations can proceed.
- Wire attempts themselves remain conservatively serialized by provider lock. Station probe concurrency is bounded at two logical operations; this is not a promise of parallel on-wire NWS transfers. A slow in-progress wire request can delay other NWS requests, but cannot prevent already validated component commits or UI actions.
- At most two transient retries with jitter; Retry-After extends provider eligibility and is never clipped to a short retry cap. Invalid schema/nontransient errors/stray 304 are not accepted/retried as success. A station/observation 404 is missing observation data, not evidence that the city is unsupported; status remains available for later rediscovery logic.

Still unconditional requests; validators persist but are not yet sent. Cache-Control freshness, conditional 304 application, extended failure scheduling, explicit next eligibility and 404 rediscovery remain Phase 6. Individual operations are bounded; checking five stations can take more than one request deadline. There is no background traffic after shutdown.

## 5. Conditions and station policy

`normalize_stations` requires a feature/property collection, rejects partial paginated metadata, validates safe station identifiers and coordinates, calculates haversine distance in miles, deduplicates IDs, sorts by distance/ID. NWS can supply a redundant continuation even after returning every station: accept it only for a safe HTTPS `api.weather.gov/stations` URL whose contiguous indexed ID filter exactly matches the returned station identities, with one nonempty opaque cursor and no other parameters. No cursor decoding or continuation requests; unknown/generic/incomplete pagination remains rejected. This check uses returned identities before coordinate filtering, so an unusable coordinate can still produce the existing skip note. The same normalizer validates live and raw cached bodies; persistence format and successful provenance are unchanged. Individual records missing usable identity/coordinates are skipped with a note for ordinary unpaginated lists; unreadable identities cannot establish the redundant-pagination exception. Structural collection failure retains last-good metadata. Station endpoints are constructed only from validated station IDs. Observation station URL must match requested/saved endpoint.

`Observation` requires actual numeric temperature and aware timestamp; unavailable temperature is missing data, never replaced by forecast. `units.py` handles Celsius/Fahrenheit/Kelvin, mph/km-h/meters-sec/knots, percent and degrees. Null values remain missing; real zero stays zero. Optional malformed units/values become unavailable with notes, not discarded otherwise usable observations. Wind/gusts stay separate. Feels-like is NWS wind chill first, then heat index, otherwise unavailable—no local estimate.

`retrieve_conditions` evaluates at most the nearest five, at most two probe operations at once, with joined cancellation. Choose nearest station whose temperature/time is usable and age in [0,90min] (configurable). Otherwise newest usable non-future observation, distance/ID tie-break; explain stale/failure limitations. Future source timestamps cannot win the stale fallback. No usable result is an operational failure retaining old conditions.

Routine refresh reuses a working nearest-five station if its new observation remains fresh. Stale/unusable result triggers remaining probes; refreshed metadata forces reranking. Cache restoration triggers a conservative first-session re-evaluation. Matching cached metadata can support new observation checks during station-metadata failure; preserve its old provenance and include metadata-failure note.

`Conditions` combines observation + optional Station + selection note. Cache persists raw observation body, not synthesized display strings. On restore, station ID/name/distance is reconstructed from cached station list, with observation geometry/ID fallback if needed. Unknown distance stays unavailable. Station selection note is session-specific; restored state explicitly says ranking unverified. Conditions source time equals observation timestamp; retrieval time comes from the chosen response, not completion of all probes.

## 6. Hourly and daily forecasts

Shared `units.py` temperature/precipitation semantics now back hourly and daily adapters. Hourly retains scalar/quantitative winds, finite fields, ordered nonoverlapping validity intervals and current/next twelve-slot selection; original expired dates stay dated, gaps/fewer periods explicit. No interpolation or rain-amount inference.

`formatting.py` supplies consistent presentation: temperatures and wind speeds have one decimal; percentages and wind directions have zero decimals. Conditions/hourly/daily renderers use it, including forecast wind scalar/range/quantitative representations (the existing hourly wind field is display text). Rounded negative zero displays as zero. Missing conditions values remain Unavailable; missing forecast numbers retain an em dash. Raw source/cache bodies and normalized numeric fields retain their original precision; full official text is untouched. No schema, provenance, unit-conversion, or dependency changes.

Daily `DailyPeriod` retains start/end, actual bool `isDaytime`, °F, probability, short and complete detailed text; `DailyForecast` retains NWS update/generation time. `select_days` groups local **start date** and day/night type, detects duplicate local-date/type periods rather than fabricating merged highs, then creates seven dated `Day` rows beginning local today. Missing today's day/missing night/missing future dates stay unavailable; overnight low isn't an observed minimum. Entirely expired data anchors on original first date, not current date. Tests cover midnight, UTC fallback, spring/fall DST, fewer periods, missing daytime, zero/null/quantitative fields and outdated dates.

Semantic local-date validation runs before persistence and on restore using the selected point timezone. Full detailed text is retained; current D view renders all seven dates in an explicitly read-only snapshot. Per-row selection/final table are Phase 5.

## 7. Alerts and safety state machine

`NwsService.alerts` uses saved point query, not statewide/marine substitution. Null geometry is valid. `normalize_alerts` requires complete feature/property set; `pagination.next`, malformed Actual required ID/event/sent or optional nontext/malformed time structures fail the operation rather than turning it into all-clear. Known Test/Exercise/Draft/System are excluded. Unknown status records are noted, not classified Actual, and set `uncertain_status` to prohibit verified all-clear.

`Alert` preserves official ID, sent/effective/onset/expires/ends, severity/urgency/certainty, area, headline, full description/instruction, safe supplied HTTPS link, messageType and references. Same ID newer sent wins; conflicting same-version duplicates fail. Explicit Update/Cancel references remove supplied older versions; Cancel records are not displayed as active warnings. Successful active set is authoritative; empty set clears previous active list. Unknown severity/urgency/certainty are preserved/labeled with deterministic ordering fallback, not dropped.

Verification requires usable successful set/provenance, not disk-origin, no current error, no unknown-status ambiguity, and check age ≤configured 3min with nonfuture time. Cached or failed sets are **Previously retrieved alerts — current status unverified**. Empty unverified data is **No alerts at last check**, never unconditional all-clear. Known-expired records remain labeled Expired. When all prior records expire, header awaits a new authoritative set instead of manufacturing a verified empty set.

`alert_status` supplies persistent header count/NOT VERIFIED; changes update immediately after success/failure and on each display tick. Event times are separate from successful provenance. Full text A view includes read-only snapshot disclaimer, set retrieval/check state, exact multiline instructions and supplied links; it is not live-updating while open. No automatic link fetch or manufactured web URL. Final per-alert selection/visual severity styling remain Phase 5.

## 8. Provenance, persistence and local lifecycle

Schema **v1 unchanged**. Source envelopes remain exact coordinate/component/endpoint identities. Station-list body under STATIONS, selected observation body under its station latest endpoint/CONDITIONS, discovered daily/hourly and point alerts each independent. `state.json` is location/discovery; `cache/locations.json` is provider/normalized query; no new user settings required.

`ComponentState`: identity/generation, normalized data, raw body, endpoint, successful provenance, disk origin, in-flight, error and session attempt. Disk reads preserve retrieved/check/source times. Errors/attempts/generations/monotonic gates are not persisted as successful checks. Current valid 200 updates both successful times; validators/freshness fields reserved for Phase 6.

Atomic writes encode first, same-dir temp, flush/fsync/close/replace and best-effort cleanup. No automatic age deletion/cross-location substitution; no filesystem-wide power-loss guarantee. Structural guards plus semantic adapters prevent replacing last-good payload with malformed success. Failures independent; failed writes do not discard memory. Bounded generic diagnostics do not log whole source payloads or unnecessary search history. Local coordinates/cache/backups may reveal selected locations.

Windows guard/logging unchanged: one app per logon session, OS releases on process death, owned logging preserves foreign handlers/previous logger configuration, ~1MiB/three backups. Tested pins unchanged: Python 3.12.10; Textual 8.2.8, httpx 0.28.1, tzdata 2026.5; pytest 9.1.1, pytest-asyncio 1.4.0. Full package snapshot is `requirements-validated.txt`, not artifact hashes/fresh-install evidence.

## 9. Test strategy and actual evidence

Phases 1–3 have user-supplied passing baselines and interactive/live confirmations. The Phase 4 baseline has user-supplied **348 passed in 20.16s** plus clean dependency/compilation checks. Live conditions then exposed the station-pagination rejection. Jake subsequently supplied clean compilation and **388 passed in 20.09s**, covering the redundant-pagination regressions: exact exhausted filter acceptance, incomplete/mismatched filters, unsafe/malformed links, skipped coordinates/deduplication, one-request service behavior, and conditions persistence/cached restart with unchanged provenance. He also confirmed the live app functions well. New display-precision regressions remain **authored, unrun**: fractional/negative/zero/missing values, negative-zero suppression, converted/scalar/range winds, unchanged raw/numeric values, and live/cached renderer modes. Suite forbids real httpx sends. No synthetic data goes to normal runtime. Source docs/schema reads are not runtime integration tests.

New coverage: station ranking/nearest-five/fresh-stale/partial failures/reuse/cancellation/concurrency, observation quantities/zero/null/optional failures, local daily grouping/DST/expired dates, complete Actual alert sets/references/dedup/order/unknowns/empty-versus-failure/expiry, all-component restore/independent outages/invalid replacement/write failures/generation races, details/headers/coalescing/quit, alert retry/redirect/change-point spacing and lock release while waiting. Import-no-I/O regression includes new modules. Historical hourly tests still isolate hourly; new UI tests use production all-four defaults.

The station-pagination fix now has compilation/full-suite and live conditions evidence. The display-precision patch still requires fresh compilation/full-suite output and a remembered-location visual check; the 388-test result predates it. Remaining manual details/layout/offline checks, weather-provider correctness comparison, and finished dashboard acceptance are not claimed. Clean-venv and integrated spec acceptance remain Phase 7.

## 10. References and decisions

- [NWS API documentation](https://www.weather.gov/documentation/services-web-api)
- [NWS OpenAPI](https://api.weather.gov/openapi.json): inspected endpoint definitions for daily/stations/observations/point-alert requests; tool extraction was truncated before complete component schemas, so full source-shape verification is not claimed.
- [NWS alerts documentation](https://www.weather.gov/documentation/services-web-alerts): rechecked ≥30-second recommended pacing; implemented immediately, not deferred until final scheduler.
- [Nominatim policy](https://operations.osmfoundation.org/policies/nominatim/), [search API](https://nominatim.org/release-docs/latest/api/Search/), [attribution](https://www.openstreetmap.org/copyright)
- [Textual workers](https://textual.textualize.io/guide/workers/)

Decisions: raw source storage with reconstructed station context avoids adding undocumented synthetic station fields to official observations. Plain whole-set text snapshots make official instructions reachable now without claiming completed selectable UI. Thin independent timers/gates are needed for usable Phase 4 integration, but do not close Phase 6. Conservative serialized NWS wire requests bound load; logical probes are still capped/joined. No product requirement or dependency changed.
