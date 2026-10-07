# Weather Terminal — Implementation Plan

**Author:** Element0, Elemental Informatics  
**Prepared for:** Jake Jenkins  
**Date:** October 7, 2026  
**Status:** Phases 1–3 complete; Phase 4 station-pagination fix validated by Jake with clean compilation, 388 passed in 20.09s, and a functioning live dashboard. Display-precision patch awaiting execution; remaining manual/integrated gates stay open.

## 1. Purpose and governing documents

Implement the personal Windows/Python 3.12 weather dashboard described in [weather-terminal-spec.md](weather-terminal-spec.md). This plan adds sequencing, implementation gates, test strategy, and documentation responsibilities; it does not expand the version 1 product scope.

Required project documentation:

- [README.md](README.md): user-facing installation, startup, controls, configuration, cache behavior, troubleshooting, data sources, privacy, and limitations.
- [ARCHITECTURE.md](ARCHITECTURE.md): system boundaries, module responsibilities, data flow, state/time semantics, request policy, storage, concurrency, and testing approach.
- This implementation plan: phase completion and acceptance evidence.
- The specification: product requirements. Change it only for an explicit requirement change, not merely because implementation is inconvenient.

Initial README and architecture drafts accompany this plan. Both distinguish planned behavior from implemented behavior. Update them during every phase; do not defer documentation until the end.

## 2. Working rules and completion criteria

### Implementation approach

1. Build a small, working vertical slice early, then add complete weather coverage and resilience.
2. Keep HTTP, normalization, persistence, scheduling, and rendering separate. Inject clocks, HTTP transports, and storage paths for deterministic tests.
3. Use mocked HTTP responses for automated tests. Do not make public API traffic a prerequisite for tests.
4. Preserve last good data independently for each component. A failed component never clears another component's valid data.
5. Treat alert freshness, timestamp correctness, and location isolation as release blockers, not presentation polish.
6. Resolve dependency versions against Python 3.12 on Windows and then pin them. Do not insert speculative version numbers into documentation.
7. Keep the implementation personal and local: no installer, executable packaging, server, telemetry, or provider fallback outside the specification.
8. Maintain a short record of noteworthy design decisions in ARCHITECTURE.md, including alternatives rejected and why.

### Definition of done for every phase

A phase is complete only when its deliverables exist, its tests/checks pass, its documentation step is complete, and remaining issues are recorded here. Record test commands and results without claiming a check that was not performed. Commits, if the project uses Git, should group code, tests, and the corresponding documentation changes.

At each checkpoint, verify that README instructions describe available behavior. Planned functionality may remain in a clearly labeled section until implemented. Architecture descriptions should distinguish target design from the modules and behavior actually delivered.

## 3. Phases

### Phase 1 — Project foundation and application shell

**Depends on:** Approved specification and this plan.

**Implementation steps**

1. Establish the target package layout, `main.py`, development/test layout, and Textual stylesheet. Use an explicit entry point with no network activity merely from importing modules.
2. Select and test compatible versions of Textual, httpx, tzdata, pytest, and pytest-asyncio; create pinned runtime and development requirements files.
3. Introduce typed models for selected location, endpoint discovery, component data/status, provenance timestamps, and errors. Separate fetch success, data age, and refresh-in-progress so one status string cannot hide stale data.
4. Add clock, HTTP transport, and storage interfaces/seams needed by later tests. Use timezone-aware UTC internally and monotonic time for elapsed scheduling.
5. Implement configuration defaults/validation and runtime paths under `%LOCALAPPDATA%\WeatherTerminal\`. Respect minimum provider intervals and reject invalid configuration with an actionable startup message.
6. Establish bounded rotating logs, single-instance enforcement on Windows, and clean startup/shutdown ownership. A second instance should explain that the first must be closed, rather than doubling geocoder traffic. Prefer a Windows named mutex with automatic OS cleanup; document the chosen implementation.
7. Build a minimal Textual shell with labeled empty/loading panels, help, quit, and location-entry placeholder. No dummy weather should appear to be real data.

**Tests/checks**

- Start the shell in the user's Python 3.12 venv and Windows Terminal; quit cleanly with Q and Ctrl+C.
- Unit-test configuration validation, UTC-aware timestamp handling, runtime path overrides, and invalid settings.
- Verify single-instance handling, log rotation configuration, and recovery after abnormal process termination without a stale lock blocking startup.
- Confirm importing application modules does not call external services.

**Documentation step**

- Update ARCHITECTURE.md with the actual module tree, lifecycle ownership, models, test seams, runtime paths, dependency choices, and single-instance decision.
- Update README.md with tested installation/start commands, supported environment, current implementation status, and startup/configuration guidance. Do not advertise completed weather retrieval yet.
- Record foundation checks and any unresolved startup issues in this plan.

**Exit gate:** A cleanly installable, responsive application shell with a passing test baseline and truthful documentation.

### Phase 2 — Persistence, provenance, and offline state

**Depends on:** Phase 1.

**Implementation steps**

1. Define versioned on-disk schemas for confirmed location state, geocoding cache, and per-location component payloads/provenance. Cache raw validated source data plus enough metadata to normalize it; do not cache only display strings.
2. Implement atomic replacement writes, independent component reads, incompatible/corrupt-cache handling, and bounded error logging. Keep in-memory data usable when disk writes fail.
3. Implement distinct `retrieved_at`, `last_checked_at`, `source_time`, and `last_attempted_at` semantics. Track persisted successful provenance separately from current request errors/status.
4. Add shared freshness/coverage calculations and timezone formatting. Distinguish observation age, verification age, forecast validity, and alert expiry. Never use retrieval time as a substitute for source time.
5. Load saved location/cache at startup, using only that location's entries. Refresh-derived labels and forecast selection from the current clock, not yesterday's stored labels.
6. Provide a useful offline/no-cache state. Synthetic test fixtures are for tests/development only, never an unlabeled substitute for actual weather.

**Tests/checks**

- Test round-trip persistence, per-location isolation, schema mismatch, truncated JSON, failed writes, and atomic replacement behavior.
- Test that a cache read never advances retrieval/check times and failed responses never overwrite good payloads.
- Test missing versus zero values, future/invalid source timestamps, UTC fallback, stale age thresholds, midnight, and DST formatting with fixed clocks.
- Restart against a temporary fixture-backed cache with HTTP disabled; verify dated, explicitly cached output without depending on live requests.

**Documentation step**

- Update ARCHITECTURE.md with actual cache schemas, identity keys, atomic-write behavior, provenance definitions, and freshness logic.
- Update README.md with runtime file locations, cached/offline behavior, timestamp meanings, and safe cache-reset guidance. Clearly distinguish configuration, remembered location, and disposable weather cache.
- Record tests and any schema changes here.

**Exit gate:** Last-good data and its original timestamps survive restart and isolated storage failures without misleading freshness labels.

### Phase 3 — Location lookup and first live vertical slice

**Depends on:** Phases 1–2.

**Implementation steps**

1. Recheck Nominatim and NWS policies and schemas against the linked official references. Record material changes before implementation decisions depend on them.
2. Implement an asynchronous, reusable HTTP layer with identifying User-Agent, TLS verification, bounded operation deadlines, request classification, and provider-aware throttling. Keep geocoder and NWS policy settings separate.
3. Implement submit-only city lookup, query normalization/cache, U.S./settlement filtering, deduplication, and ambiguous-result selection. Keep attribution visible and the provider endpoint configurable.
4. Enforce Nominatim's one-second minimum request spacing, including retries. No background geocoding, autocomplete, or geocoding on routine weather refresh.
5. Resolve a selected result through NWS `/points`; capture endpoint URLs and the IANA timezone. Reject unsupported coverage without replacing the existing active location.
6. Define the location-selection transaction: commit a confirmed, supported location and its label together; clear/load only its panels; invalidate obsolete worker generations. Retain the old dashboard while search/validation is pending or canceled.
7. Complete a thin end-to-end slice: selected city → point discovery → hourly forecast → normalized twelve-hour panel → saved cache → offline restart. The other weather panels remain explicitly unavailable until Phase 4.

**Tests/checks**

- Mock unique/ambiguous/empty queries, duplicate places, businesses/county-only results, rejected territories, unsupported points, lookup failures, and cached queries.
- Test submit-only behavior, one-second spacing across retries, repeated submissions, cancellation, and no requests on keystrokes.
- Test a late old-location response after a selection change; it must not update the new location's state or cache identity.
- Run one small, explicitly initiated live check for Oxford, MS if provider access is available. Do not repeatedly search just to exercise tests.

**Documentation step**

- Update ARCHITECTURE.md with HTTP ownership, geocoder/NWS boundaries, point discovery, supported-location transaction, and generation-token lifecycle.
- Update README.md with city search examples, ambiguity handling, attribution, third-party query disclosure, and the currently available hourly slice.
- Record provider-policy verification, live checks actually performed, and implementation limits here.

**Exit gate:** A selected city's real hourly forecast can be displayed and restored offline; failed lookup cannot relabel old weather as a new city.

### Phase 4 — Complete weather services and normalization

**Depends on:** Phase 3.

**Implementation steps**

1. Implement discovered daily forecast and station-list retrieval, plus station observations and point-based active alerts.
2. Calculate station distances; check up to the nearest five with bounded concurrency; select according to the specification's usable/fresh observation policy. Reuse/re-evaluate the selected station as specified.
3. Normalize temperature/wind units, sustained wind/gusts, humidity, dew point, and NWS wind chill/heat index without inventing missing observations.
4. Finish hourly selection using validity intervals. Support documented forecast representations, preserve missing hours, and distinguish precipitation probability from amounts.
5. Group daily day/night periods into seven local dates, including missing today's daytime period and correctly labeled overnight lows. Preserve full daily forecast text for details.
6. Normalize actual active alerts, official ordering/identity, authoritative successful empty sets, complete text/instructions, and unverified/expired cached states. Preserve valid null-geometry point-matched alerts.
7. Validate each response before replacing that component's last-good data. Preserve unknown optional fields/enums safely; classify malformed required structures as an invalid response.

**Tests/checks**

- Cover station distance/ranking, fresh/stale candidates, no usable observations, missing optional fields, and partial station-request failures.
- Cover numeric/quantitative field shapes, NWS unit conversion, real zeros/nulls, and feels-like precedence.
- Cover partially elapsed hours, fewer than twelve periods, midnight/DST, day/night grouping, missing highs, and fewer than seven supplied dates.
- Cover alert updates, successful empty sets, failures retaining old alerts, expiry, unknown enum values, actual versus test/exercise status, and point/zone responses with null geometry.

**Documentation step**

- Update ARCHITECTURE.md with the actual normalized models, station strategy, forecast grouping rules, and alert-state transitions.
- Update README.md with explanations of observations versus forecasts, station provenance, Precip %, overnight lows, full-detail access once available, and alert safety limitations.
- Record data-shape compatibility verified and remaining fixture gaps here.

**Exit gate:** All four required weather components have independently tested service/normalization/persistence paths; no fabricated values or unconditional cached all-clear.

### Phase 5 — Complete Textual dashboard and details views

**Depends on:** Phase 4.

**Implementation steps**

1. Implement the full panel layout with a prominent alert area, current conditions, twelve-hour table, seven dated forecast rows, and per-panel retrieval/check/source indicators.
2. Add header alert status, local clock/timezone, refresh status, footer controls, full alert/day details, help, and a location/coordinates details view.
3. Complete keyboard navigation and shortcuts; input focus must protect normal city-name typing. Make scrolling and selection work without a mouse.
4. Build wide side-by-side and narrow stacked layouts. At 80×24, every feature remains reachable; small windows get a usable resize message.
5. Render untrusted API text as plain text/sanitized content, not Rich markup or terminal control sequences. Do not rely on color or emoji for meaning.
6. Bind UI widgets to component state rather than HTTP responses. Network latency and detail dialogs must not block exit, resizing, or scrolling.

**Tests/checks**

- Use Textual's test facilities to cover typing, modal cancellation, alert/day selection, navigation, resize, scrolling, and clean quit.
- Exercise slow requests while interacting with the UI and changing locations.
- Verify explicit stale/failure/loading/empty states, date visibility for old forecasts, full official alert instructions, and remote text containing markup/control characters.
- Manually inspect the layout in Windows Terminal at approximately 120×40 and 80×24; automated screenshots alone do not establish local terminal readability.

**Documentation step**

- Update ARCHITECTURE.md with actual screen/widget composition, reactive state flow, safe text rendering, and worker ownership.
- Update README.md with verified key bindings, dashboard layout, detail views, terminal-size guidance, and status-label interpretation. Include a labeled illustrative layout, not a screenshot falsely presented as a live test.
- Record keyboard/resize observations and accessibility issues here.

**Exit gate:** All version 1 information is accessible through a responsive, keyboard-driven interface with unmistakable provenance and stale-data labels.

### Phase 6 — Refresh orchestration and failure recovery

**Depends on:** Phases 2–5.

**Implementation steps**

1. Implement independent refresh/revalidation schedules: alerts 60 seconds; observations five minutes; hourly/daily forecasts fifteen minutes; point/station metadata twenty-four hours. Tick the UI clock separately without HTTP.
2. Add ETag/Last-Modified handling and HTTP cache freshness. Valid 304 responses advance successful check time, not body retrieval or source time. Never synthesize an all-clear from a 304 without a usable cached body.
3. Add request coalescing, non-overlapping component work, manual refresh state, and provider minimum intervals. Alerts must never be requested more often than once per thirty seconds, including retries/manual requests.
4. Implement bounded transient retries, jittered backoff, Retry-After handling, extended failure schedules, and normal-cadence recovery. Compute the next allowed attempt from all applicable constraints; a five-minute retry cap must not override a longer provider Retry-After.
5. Rediscover point/grid URLs once after relevant endpoint 404 errors. Keep refreshed metadata isolated by selected location and do not retry rediscovery indefinitely.
6. Complete startup cache-first behavior, immediate failed-check notices, source-age warnings, expired forecast coverage, and previously retrieved/unverified alert presentation.
7. Finalize cancellation and shutdown: stop timers/workers, reject obsolete results, close HTTP clients, and release the instance guard without corrupting persisted data.

**Tests/checks**

- Use a fake clock to prove schedules, throttling, HTTP freshness, 304 semantics, retry deadlines, Retry-After, and recovery without slow real-time tests.
- Simulate connection/read timeout, partial endpoint outage, 429, 5xx, invalid successful responses, rediscovery, offline restart, and recovery.
- Hammer manual refresh in tests and prove there are no duplicate requests or policy violations.
- Prove changing location or quitting during retry/timeout cannot apply old results or leave hanging tasks.
- Verify stale observations stay stale after successful revalidation, expired forecasts retain original dates, and failed alerts never become a current all-clear.

**Documentation step**

- Update ARCHITECTURE.md with the implemented scheduling/state machine, conditional requests, retry arbitration, cancellation, and recovery paths.
- Update README.md with automatic/manual refresh behavior, freshness warnings, offline expectations, configurable intervals, and provider-throttling explanations.
- Record fault-injection results and unresolved resilience defects here.

**Exit gate:** The continuously updating dashboard remains usable and honest through outages, restart, partial failures, and recovery without provider abuse.

### Phase 7 — Integrated acceptance, security, and Windows checks

**Depends on:** Phases 1–6.

**Implementation steps**

1. Run the complete deterministic test suite and close gaps against all fourteen specification acceptance criteria.
2. Review remote-text handling, HTTP destination/redirect validation, TLS verification, configuration validation, bounded logs, location privacy, and absence of analytics/hidden calls.
3. Review offline behavior using successful real data already cached; simulate an outage rather than repeatedly hitting unavailable providers. Verify corrupt cache and unavailable disk paths do not destroy usable in-memory data.
4. Perform a small opt-in live smoke test with Oxford, MS and one supported city in a different timezone. Validate active-alert behavior using fixtures, not by waiting for severe weather.
5. Recreate a clean venv on the user's Windows system and follow README instructions verbatim. Confirm package pins, startup from another working directory, and direct venv-interpreter usage without execution-policy changes.
6. Fix release blockers and rerun affected tests. Record any non-blocking limitation explicitly; do not quietly redefine acceptance requirements.

**Tests/checks**

- Record automated test commands/results and the environment used for manual checks.
- Complete the acceptance mapping below with evidence rather than a blanket statement that everything passed.
- Verify normal exit does not leave background traffic or a lock that prevents the next launch.

**Documentation step**

- Reconcile ARCHITECTURE.md against actual code; remove obsolete design claims and identify genuine limitations.
- Validate every README command, path, key binding, configuration setting, troubleshooting step, attribution, and safety/privacy statement.
- Update this plan with test evidence and acceptance status; adjust the specification only if Jake explicitly approves a product-requirement change.

**Exit gate:** All specification acceptance criteria are demonstrated, documentation matches reality, and no critical timestamp/alert/location-isolation defect remains.

### Phase 8 — Handoff and maintenance baseline

**Depends on:** Phase 7.

**Implementation steps**

1. Confirm the intended daily workflow: create/use the venv, run `python main.py`, select a location, inspect alerts/details, refresh, and quit.
2. Provide the verified dependency-update and test workflow for future maintenance. Keep live checks opt-in; review API/provider policy changes before updating integrations.
3. Review completion with Jake and collect presentation adjustments separately from deferred feature requests. Do not introduce radar, notifications, packaging, or other out-of-scope work as a handoff prerequisite.
4. Record the delivered baseline, known limitations, and any approved follow-up work. Leave the specification and test fixtures as regression references.

**Tests/checks**

- Repeat the documented startup workflow once after final documentation edits.
- Verify a subsequent launch restores the remembered location/cache and automatically revalidates due components.

**Documentation step**

- Finalize README.md as the daily-use entry point, including troubleshooting and safe local-data reset instructions.
- Finalize ARCHITECTURE.md as the maintainer reference, including tested dependency versions, schema version, provider assumptions, and extension boundaries.
- Mark phases complete here only after their gates have passed; record handoff date and remaining issues.

**Exit gate:** Jake has a working, documented local application and a clear maintenance baseline, without unsupported claims about guaranteed weather notification.

## 4. Acceptance coverage map

Numbers refer to section 8 of the specification. All remain **not yet fully demonstrated** for the completed product. Phases 1–3 have passing automated and user-confirmed interactive evidence. Phase 3 supplied clean compilation, **263 passing tests in 19.06s**, Oxford hourly-display screenshot, and explicit remembered/R refresh, offline recovery, ambiguity/cancellation/interaction confirmation. Phase 4 has user-supplied clean compilation, **388 passing tests in 20.09s** after the station-pagination fix, and confirmation of a functioning live dashboard. The subsequent display-precision patch awaits execution. Later table/detail/conditional-refresh work and integrated product acceptance remain pending.

| Spec criterion | Primary phase(s) | Evidence required |
|---|---|---|
| 1. Launch and selection | 1, 3, 7 | Clean Windows launch; confirmed location restored |
| 2. Ambiguity | 3, 5 | Mocked multi-city selection; keyboard choice |
| 3. Required display | 4, 5, 7 | Four panels and appropriate coverage/absence labels |
| 4. Provenance | 2, 4, 5 | Timestamp/station assertions and UI inspection |
| 5. Outage after success | 2, 6, 7 | Independent fault injection; original retrieval times retained |
| 6. Offline restart | 2, 3, 6, 7 | Cached/no-cache startup tests and manual restart |
| 7. Alert safety | 4, 5, 6 | Empty/failure/expiry fixtures and visible unverified labels |
| 8. Refresh correctness | 3, 6 | Fake-clock scheduling, policy spacing, 304 assertions |
| 9. Location race | 3, 5, 6 | Delayed old-generation results rejected |
| 10. Units/nulls | 4 | Conversion, missing-field, and real-zero tests |
| 11. Time boundaries | 2, 4, 6 | Fixed-clock midnight/DST/expired-coverage fixtures |
| 12. UI | 1, 5, 7 | Textual tests and Windows wide/narrow inspection |
| 13. Storage resilience | 2, 4, 6 | Corruption/schema/write/response failure tests |
| 14. Lookup policy | 1, 3, 7 | Single-instance guard; submit/cache/spacing/attribution checks |

## 5. Initial decisions and risks

- **Single-instance runtime:** Use an application-level guard to keep this personal application from defeating aggregate geocoder limits. This is an operational safeguard, not a multi-user service design.
- **Provider coverage and policy:** NWS and Nominatim may change behavior. Verify official docs in Phase 3; reject unsupported locations honestly rather than silently substituting providers.
- **Dependency compatibility:** Pin versions only after Windows/Python 3.12 verification. If a dependency update changes APIs, adapt tests and documentation together.
- **Source-data limitations:** Nearby observations may be old or missing; forecast periods may not cover seven complete dates. Missing data is an accepted displayed condition, not permission to fabricate values.
- **Time/concurrency risk:** DST, expired cached forecasts, Retry-After, and location changes are correctness risks. Build deterministic tests before cosmetic refinement.
- **Documentation drift:** Documentation is part of each exit gate. Initial drafts are intentionally labeled planned; remove that label only as functionality is actually delivered.

## 6. Progress record

| Phase | Status | Evidence / outstanding work |
|---|---|---|
| 1. Foundation | Complete | User-supplied evidence: pip check clean, compilation clean, 93 passed in 8.32s, interactive and single-instance checks confirmed. Python 3.12.10 and full pip freeze supplied; direct requirements now pin the exact tested versions. No installed-environment change or new run claimed. |
| 2. Persistence | Complete | Jake reported compilation clean and 178 passed in 11.52s, then “Everything looks good! Let's proceed.” with a help-dialog screenshot. Patched interactive check confirmed; no live weather acceptance inferred. |
| 3. Lookup/vertical slice | Complete | Jake supplied clean compilation, 263 passed in 19.06s, Oxford screenshot, and confirmed remembered restart/R refresh, offline recovery and remaining interactive checks. Element0 did not execute Python/provider checks. |
| 4. Weather models/services | Automated/live baseline validated; remaining manual checks open | Jake supplied clean compilation, 388 passed in 20.09s after the station-pagination fix, and a functioning live dashboard. Display-precision patch authored, unrun. Remaining details/layout/offline checks not yet recorded. |
| 5. Full dashboard | Not started | UI and details pending |
| 6. Refresh/recovery | Not started | Scheduler and fault-injection tests pending |
| 7. Acceptance/Windows checks | Not started | Foundation interactive checks confirmed; integrated weather acceptance pending |
| 8. Handoff | Not started | Dependent on accepted working application |

**Documentation created at planning time:** this plan, the initial ARCHITECTURE.md, and the initial README.md. No code was written in the original planning step. Implementation has now started, as recorded below.

### Phase 1 original authoring checkpoint (before the Windows run below)

**Deliverables authored**

- `main.py`, the Textual foundation shell/style, first-mount location-input preview, help, UTC clock, and explicit unavailable panels. No synthetic weather or real provider calls.
- Typed location/endpoint/provenance/component state; injected clock, runtime paths, and an HTTP transport interface.
- Four typed configuration sections and a default TOML template, with startup validation, documented names/limits, and exclusive initial creation.
- Windows named-object single-instance guard (one logon session), POSIX development fallback, rotating diagnostics, and explicit resource ownership.
- Runtime/development requirement constraints, pytest configuration, .gitignore, and six test modules.
- README rewritten for actual preview behavior and pending verification; ARCHITECTURE updated with actual files, lifecycle, guard decision, test seams, and remaining target design.

**Verification actually performed**

- Read the governing specification/plan/architecture and inspected the existing workspace before adding code.
- Consulted Textual application/screens/testing documentation and v8.2.8 Static source; checked PyPI metadata for Textual 8.2.8, httpx 0.28.1, pytest 9.1.1, and pytest-asyncio 1.4.0. The tzdata PyPI page did not provide usable metadata, so no version was asserted.
- File-level authoring/review only. **No Python commands, installs, compile checks, automated tests, Windows Terminal checks, or live API requests have been executed.** This session's tools cannot run Python or a shell.

**Open Phase 1 work / gate blockers**

1. Resolve/install dependencies in Jake's Windows Python 3.12 venv and run `pip check`, `compileall`, and the test suite (commands in README).
2. Correct any runtime/test/API/style incompatibilities revealed by those results.
3. Complete the Windows Terminal wide/narrow/input/help/quit/single-instance checks. Guard crash cleanup and cross-directory behavior also have authored, unexecuted tests.
4. Collect installed versions, establish verified dependency pins, and update documentation with the real environment and command results.
5. Only then mark Phase 1 complete and proceed through its exit gate.

**Temporary implementation decision:** Requirements use explicitly provisional ranges rather than asserting untested pins. This does not remove the plan's tested-pin requirement; it leaves that deliverable and the gate open until execution evidence exists.

**Next implementation phase:** Phase 2 persistent state/component cache and offline provenance, after foundation verification. Phases 2–8 have not begun. The product specification is unchanged.


### Phase 1 first Windows run and correction checkpoint

**Execution evidence supplied by Jake**

- Environment: Windows PowerShell, activated Python 3.12 venv, project `C:\Users\jakej\Code\demo`. The traceback identifies Python 3.12; exact installed package versions were not supplied.
- `python -m pip check`: **No broken requirements found.**
- `python -m pytest -q`: **5 failed, 76 passed in 6.35s**.
- Failing tests: first location mount/typing, Ctrl+C from location input, valid fake-UI startup, unexpected UI failure lifecycle, and logging rotation.
- UI diagnostics identify `NoMatches` querying `Input` in `LocationPreview.on_mount`, with secondary `WinError 6` during Textual's headless error printing. Startup tests returned the generic pre-logger failure message; the final logging test found the logger configured. The transcript does not identify the remaining handler's type/origin.

**Corrections authored; not yet executed**

1. Replace the location screen's mount-time query with Textual's `AUTO_FOCUS` selector. Exclude its scroll wrapper from tab focus.
2. Introduce explicitly owned/context-managed logging. Preserve foreign handlers, reject duplicate application-owned handlers, restore prior logger level/propagation, and make close idempotent. The entry point nests this session inside the process guard on all return/exception paths.
3. Give headless UI tests live pytest-captured original streams, avoiding invalid Windows console handles during diagnostic rendering while preserving exceptions.
4. Add repeated dialog focus/reopen cases and an intentionally failing mount case that must propagate the real exception. Add foreign-handler, failed-open, exceptional cleanup, repeated-close, and failed-start-then-success regressions (config, UI, interrupt, missing dependency).
5. Update README and ARCHITECTURE with this actual evidence, fixes, and remaining uncertainty. Product scope and specification are unchanged.

**Remaining exit-gate work**

- Run compile checks and the entire patched suite in a fresh Python process, not only the previously failing tests. If startup/logging failures persist, inspect new output and the relevant temporary diagnostic log before attributing them to a particular cause.
- Record a successful rerun before manual shell checks. No new expected test count is claimed until execution; parametrized cases have been added.
- Complete the Windows Terminal checklist and collect installed package versions; verified pins and Phase 1 completion still require those results.
- Element0 has not run Python, installed dependencies, or performed a live-provider check. The run above is user-supplied evidence, not assistant execution.


### Phase 1 second Windows run and startup correction checkpoint

**Execution evidence supplied by Jake**

- `python -m compileall -q main.py weather_terminal tests`: completed without reported errors.
- `python -m pytest -q`: **5 failed, 87 passed in 3.86s**.
- The prior startup/logging failures are absent from this run.
- Four UI cases found the default screen rather than the startup modal at their first assertion. The intentionally failing mount regression hit `StylesheetError` before mounting: the test-defined App subclass resolved the inherited relative stylesheet to `tests/styles.tcss`.

**Inspection and corrections authored; awaiting execution**

1. Inspected the installed Textual application, message-pump, screen, pilot, and mount-awaitable source in the shared workspace. `call_after_refresh()` defers work through the screen refresh callback queue; a pilot pause can return before the callback runs. It is not a guarantee that required startup UI has mounted. This was a gap in the previous correction.
2. Make `WeatherApp.on_mount()` asynchronous and await `action_location()`. Make that action await `push_screen()`'s mount awaitable; it does not wait for dismissal. The modal mounts before application startup readiness rather than relying on repaint timing.
3. Anchor `CSS_PATH` to `Path(__file__).resolve().with_name("styles.tcss")` in the application module. A concrete subclass or a changed working directory no longer redirects the asset lookup.
4. Strengthen the first-screen test to assert that the modal is already mounted when `run_test()` yields. Adapt the intentionally failing subclass action to the async contract. Add a successful subclass/changed-working-directory/style-application regression. No arbitrary delays, exception suppression, or relaxed assertions were added.
5. Update README, ARCHITECTURE, and this plan with the second run, actual changes, and pending checks. No dependencies or product requirements changed.

**Remaining gate work**

- Rerun compilation and the entire suite in Jake's Windows venv; no reinstall is required for this patch. The latest patch has not been executed by Element0 or reported by Jake.
- If green, perform the README Windows Terminal checklist and capture installed versions before pinning dependencies and closing Phase 1.
- Phases 2–8 remain not started. No live weather requests or persistent weather cache exist yet.


### Phase 1 third Windows run — passing automated baseline

**Execution evidence supplied by Jake**

- Same Windows PowerShell/Python 3.12 venv workflow as preceding runs.
- `python -m compileall -q main.py weather_terminal tests`: completed without reported errors.
- `python -m pytest -q`: **93 passed in 8.32s**.
- `python .\main.py`: invoked and returned to the prompt with no printed error. The full-screen interface is not captured in this transcript; interactive behavior, checks performed, and process exit status are not inferred.

**Checkpoint action**

- Record the passing foundation automated baseline in README, ARCHITECTURE, and this plan. No application code, tests, or dependencies changed while recording this result.
- The previous startup, logging, headless diagnostic, and stylesheet regression cases now have passing user-supplied evidence. Element0 has not executed the commands.

**Remaining Phase 1 gate work**

1. Confirm the expected startup modal/dashboard actually appeared and complete README's manual wide/narrow, input/help/quit, and second-instance checks. If `main.py` returned immediately without displaying the app, investigate that separately rather than treating a quiet console as a successful interactive launch.
2. Capture the Python version and installed dependency snapshot; pin the actual tested direct dependencies and rerun dependency consistency/tests against the pins.
3. Close Phase 1 only after those checks, then begin Phase 2 persistence and offline provenance. No additional automated rerun is needed merely for these documentation-only updates.


### Phase 1 interactive confirmation

Jake confirmed: **“Everything looks great! I can only run one copy at once.”** This follows the requested startup/input/help/quit/layout and second-instance checklist. Record the foundation interactive checks and single-instance protection as user-confirmed; no separate measurements, screenshots, or unreported execution results are asserted.

The automated baseline remains **93 passed in 8.32s**, with compilation and dependency consistency previously reported successful. README and ARCHITECTURE now reflect the interactive confirmation. This checkpoint changed documentation only.

**Remaining Phase 1 housekeeping:** capture `python --version` and `python -m pip freeze`, establish tested direct dependency pins from those real versions, and verify dependency consistency/tests against the pinned environment. Do not guess versions or declare the pinned-environment check performed. Phase 2 persistence and offline provenance is the next implementation work; no provider retrieval or weather cache has been delivered yet.


### Phase 1 environment capture and gate closure

**Evidence supplied by Jake**

- `python --version`: **Python 3.12.10**.
- Complete `python -m pip freeze` from the same activated Windows venv, following **93 passed in 8.32s**, successful compilation, and interactive/single-instance confirmation.
- Direct runtime versions: Textual **8.2.8**, httpx **0.28.1**, tzdata **2026.5**.
- Direct test versions: pytest **9.1.1**, pytest-asyncio **1.4.0**.

**Actions performed**

1. Replace provisional runtime/development constraints with exact direct pins matching that validated existing environment.
2. Save the complete supplied package snapshot as `requirements-validated.txt`, including transitive packages, Python/environment context, and explicit validation limits.
3. Update README and ARCHITECTURE with the tested environment, direct-pins-versus-full-snapshot distinction, and truthful phase status.
4. Close the Phase 1 gate using the accumulated user-supplied evidence. No application code or installed environment was changed, so another run is not required merely to record matching requirements metadata. This resolves the earlier checkpoint's request to verify the pinned environment: the pins describe the already tested installed versions, rather than introducing new versions.

**Validation limits**

- Element0 did not execute Python, install packages, or run a provider check.
- A clean install from `requirements-validated.txt` has not been tested. It records exact versions, not artifact hashes; direct requirements do not freeze transitive resolution.
- Clean-venv installation remains Phase 7 work. No claim of weather-feature acceptance follows from this foundation gate.

**Next:** Phase 2 persistence, provenance, and offline state. Phase 2 code has not begun at this checkpoint; no saved location or real weather cache exists yet. Every subsequent phase continues to require tests and README/ARCHITECTURE updates.


### Phase 2 authoring checkpoint — persistence and offline provenance

**Deliverables authored**

1. `storage.py`: schema version 1 selection/discovery, provider/query search cache, and coordinate/component/endpoint-hash raw body/provenance files. Selection and endpoint identities are validated; reads preserve all original successful timestamps. Session errors/attempts/generations/elapsed deadlines are not persisted as successful checks.
2. Atomic encode-before-touch, same-directory temporary writes, flush/fsync/close/replace, best-effort cleanup. Invalid bodies or failed writes retain last-good target and usable input data. Corrupt/incompatible/oversized entries fail independently. No auto deletion, stale eviction, or implicit config reset.
3. Bounded generic storage diagnostics; `ReadResult`/`WriteResult` explicitly distinguish storage issues from network failures. Duplicate JSON keys, non-finite numbers, naive/malformed instants, endpoint/coordinate identity mismatches, and unsafe path components are rejected.
4. `freshness.py`: separate observation and verification age, future-clock/source warnings, local timezone formatting with offset/fallback, source validity selection, expired coverage retaining actual dates, and conservative cached alert expiry.
5. `offline.py`: cache-first per-location snapshot, adapter normalization seam restoring disk-origin component state, and deliberately limited cached-source/provenance previews. No complete condition/forecast normalization or official full alert detail view is claimed.
6. Textual startup loads disk off the event loop, restores the saved point without opening city entry, and independently populates available source previews. Invalid/no selection opens the usual usable dialog; no different-city cache is substituted. Display ticks recompute age/coverage/expiry without disk or HTTP.
7. Three new test modules (`test_storage.py`, `test_freshness.py`, `test_offline.py`), explicit synthetic source fixture/helper, tests package marker, and expanded import-no-I/O regression. Offline tests block provider HTTP calls; fixtures write only temporary directories.
8. README and ARCHITECTURE updated for current delivered behavior, exact schema/identity rules, provenance, atomic-write limitations, recovery procedures, and pending verification. Entry-point/config template wording updated to match the cached-source preview. Dependency pins are unchanged; product specification is unchanged.

**Not yet delivered / intentionally deferred**

- The application does not fetch any real weather, geocode a city, commit a supported selection through the UI, or automatically refresh. Phase 2 writers will be called by validated adapters/selection flow in Phase 3.
- Conditions and final hourly/daily weather tables are not normalized. Cached source previews show original intervals and provenance; alerts remain unverified. This avoids passing partial raw fields off as finished weather panels.
- Disk failures are returned as explicit storage issues; future response/selection application must retain new in-memory data and surface a write-failure notification. No fabricated response path exists to simulate live success in the production app.
- Source shape guards are structural, not complete NWS semantic validation. Phase 3/4 adapters must supply semantic validation before replacement; the storage validator extension cannot disable basic schema/shape protection.

**Verification actually performed**

- Reviewed existing plan/spec/models/app/config/tests and checked authored files for consistency.
- File-level edits/review only. No Python commands, install, compilation, pytest, manual Windows UI check, or live API request was executed by Element0. The existing 93-pass result applies to the previous Phase 1 code, not this patch.

**Next gate checks (Jake's existing activated venv; no reinstall required)**

```powershell
python -m compileall -q main.py weather_terminal tests
python -m pytest -q
```

The full suite includes fixture-backed offline restarts and faults. Optional focused rerun:

```powershell
python -m pytest -q tests/test_offline.py
python main.py
```

For a normal Phase 1 runtime directory, manual launch should still show location-input preview/unavailable panels, because no real selection or weather has been retrieved. Check help, input/cancel, resizing, and quit; do not populate normal storage with synthetic weather. All fixture-backed source-preview behavior is demonstrated only by the authored tests until they run.

**Exit gate remains open:** record real command output, fix any failures, verify the patched full suite, and reconcile docs. No expected test count, Phase 2 completion, fresh dependency install, or weather-feature acceptance is asserted yet.


### Phase 2 Windows automated validation checkpoint

**Execution evidence supplied by Jake**

- `python -m compileall -q main.py weather_terminal tests`: completed without reported errors.
- `python -m pytest -q`: **178 passed in 11.52s**, no failures.
- Commands were run from the activated Windows PowerShell venv used for the preceding baseline; no new dependency installation or provider check was reported.

The full suite now provides passing evidence for Phase 2 persistence, timestamp preservation, independent corruption/recovery paths, freshness/time-boundary helpers, and fixture-backed offline cache-first restarts. The tests use temporary synthetic caches, not live weather in the normal runtime directory.

**Actions performed at this checkpoint**

- Update README, ARCHITECTURE, and the current progress record with the actual passing result and remaining check.
- No application code, test code, or dependencies changed. Element0 did not execute the commands.

**Remaining gate check:** run `python main.py` and confirm input, help, cancel, resizing, and quit still work after the startup changes. The prior Phase 1 interactive confirmation predates this patch. A focused offline rerun is optional because the full suite already passed. Once the patched interactive check is confirmed, close Phase 2 and begin Phase 3 city lookup/NWS hourly vertical slice. Live-provider acceptance and clean-venv recreation remain later work.


### Phase 2 interactive confirmation and gate closure

Jake confirmed **“Everything looks good! Let's proceed.”** and supplied a screenshot of the Phase 2 help dialog/dashboard. Record the patched interactive smoke check as confirmed after compilation and **178 passed in 11.52s**. The screenshot confirms displayed foundation/help content, not live weather behavior or measured terminal dimensions. Phase 2 gate is closed using accumulated user-supplied evidence. Element0 did not execute Python. No dependencies/product requirements changed.

### Phase 3 authoring checkpoint — city lookup and hourly vertical slice

**Deliverables authored**

1. `services/http.py`: lazy reusable verified-TLS httpx transport, streamed response bound, destination/redirect checks, operation/connect/read deadlines, identifying headers, separate provider pacing/locks, bounded transient retries, Retry-After gate, and strict JSON. Nontransient/invalid responses are not retried as successful data. A long provider wait is retained without blocking an operation indefinitely.
2. `services/geocoding.py`: explicit-submit search, normalized provider/query cache, repeat/concurrent-query cache recheck, successful empty caching, U.S. settlement filtering/territory exclusion, conservative duplicate collapse, and write-failure preservation. No autocomplete/keystroke requests, periodic geocoding, or silent provider fallback.
3. `services/nws.py`: four-decimal point query, discovered forecast/station endpoints, endpoint-family/destination/location validation, timezone/UTC fallback, semantic hourly retrieval, and original source versus download provenance.
4. `hourly.py`: typed normalized scalar/quantitative temperatures/wind, null-versus-zero handling, precipitation probability, validity-based twelve-slot selection, source-gap/fewer-period notices, timezone/DST dates, and expired original-date retention.
5. `slice.py`: supported-location transaction, intent/generation guards, serialized persistence/state mutation, independently retained last-good hourly data, cache-first normalized restoration, and no-overlap refresh. Obsolete responses cannot apply/write hourly weather to a new selection. Write failure does not destroy usable memory. Already-started atomic writes finish under the mutation lock; accepted commits are not rolled back as if still pending searches.
6. Textual actual search/selector/confirmation flow; safe `Text` candidate labels; pending search/point cancellation; startup saved-location hourly retrieval; 15-minute/manual first-slice refresh; cancellation/client cleanup on exit. Other panels remain unavailable/legacy cached previews and alerts remain NOT VERIFIED.
7. New deterministic HTTP/geocoder/hourly/transaction/live-UI tests and synthetic helpers. Prior isolated UI/offline cases explicitly disable networking; suite-wide conftest forbids real httpx sends. Import-no-I/O coverage expanded to new modules. Test weather remains only in temporary paths.
8. README/ARCHITECTURE rewritten for actual Phase 3 scope, network disclosures, policy/attribution, timestamp/cache behavior, tests, known deferrals, and live-check workflow. Updated entry-point/default-config wording. Schema remains v1 and dependency pins are unchanged; specification unchanged.

**Provider recheck and static verification performed**

- Rechecked [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/), [Nominatim search API](https://nominatim.org/release-docs/latest/api/Search/), and [NWS API documentation](https://www.weather.gov/documentation/services-web-api) on October 7, 2026. The selected public geocoder remains a deliberate, informed, single-user weather-location dependency, not a generic lookup platform. Public use requires restrained manual traffic, identifying User-Agent, ≤1 request/second, caching, attribution, no confidential data/autocomplete, and configurable service choice.
- Inspected installed Textual 8.2.8 Select and App shutdown source. Selector's empty sentinel is `Select.NULL`; shutdown dispatches Unmount for explicit task/client cleanup. This is code inspection, not execution.
- Reviewed code/files for phase consistency and location/data/cache boundaries. **No Python/shell command, package installation, automated test, Windows interactive Phase 3 run, or program-level live provider request was executed by Element0.** Prior 178-pass result applies to Phase 2, not the new patch. No expected new test count is asserted.

**Deliberately incomplete behavior / later phases**

- No live current observations, daily forecasts, official alert sets/full details, or polished final tables. Legacy component caches remain source previews, never fake live values/all-clear.
- This phase has a simple hourly timer, coalescing, and bounded policy-aware HTTP operations. Full conditional GET/304/HTTP freshness orchestration, independent component schedules, extended failure/next-retry presentation, and grid-404 rediscovery remain Phase 6. Current requests are unconditional; validators are retained only for future support.
- Search results persist even if the subsequent location dialog is canceled; that is query-result caching, not a committed active location. Pending support failure/cancel preserves active location. A confirmed supported city may legitimately have unavailable hourly data if its forecast request fails.
- Fresh-venv installation and finished-product acceptance remain later work.

**Phase 3 gate checks — no reinstall required in Jake's existing venv**

```powershell
python -m pip check
python -m compileall -q main.py weather_terminal tests
python -m pytest -q
```

If green:

1. `python main.py`: submit **Oxford, MS** once. Confirm resolved city/point, timezone, real hourly data, retrieval/check/update distinction, and NOT VERIFIED alerts. No current/daily/alert data is expected yet.
2. Quit/restart: location/forecast restored before hourly retrieval; no repeat geocoding. R requests a coalesced hourly refresh. Inspect wide/narrow layout/scrolling and input/help/cancel/quit.
3. Check ambiguity with one deliberately submitted name (e.g. Springfield), selecting explicitly or canceling without changing the current dashboard. Avoid repeated public searches solely to test cases covered by mocks.
4. After successful weather retrieval, quit and temporarily disconnect network, then relaunch. Cached forecast retains dates/successful times; failed hourly check is visible. Restore connectivity and R to recover.
5. Supply command/live observations; fix failures before closing Phase 3. Document exact execution results without inferring complete weather acceptance from this first slice.

**Gate remains open.**


### Phase 3 first Windows run — dialog task collision and correction

**Evidence supplied by Jake**

- Full suite: **23 failed, 239 passed in 31.65s**.
- Every listed failure reports `AttributeError: property 'task' of 'LocationPreview' object has no setter` (including a subclass).
- Captured output includes the generic unreadable/corrupt local-entry warning. No Phase 3 compilation, live provider success, or interactive acceptance result is inferred from this excerpt.

**Cause verified by source inspection**

Textual's `MessagePump.task` is a read-only property exposing its message-pump task. Phase 3's `LocationPreview.__init__` incorrectly assigned `self.task`, so modal construction failed before search or forecast display. Textual also owns `_task`; replacing the property with a setter or assigning its private field would corrupt framework lifecycle ownership rather than fix the application.

**Corrections authored**

1. Rename the dialog's provider-operation reference to `lookup_task` at construction, search, candidate confirmation, and cancellation. Leave inherited `task`/`_task` untouched.
2. Update scripted live-UI waits to await `lookup_task`, not Textual's message-pump task.
3. Add construction regression and strengthen mounted-task separation and cancel-during-lookup tests. Do not read Textual's task property after dismissal: its framework reference is cleared at teardown.
4. Assert the expected generic warning in the test that intentionally corrupts its temporary state file. Keep production diagnostics enabled. No deletion/reset of normal caches is warranted by these construction failures.
5. Update README and ARCHITECTURE with actual failed-run evidence, task ownership boundaries, storage-warning interpretation, and pending validation. Product specification, requirements, and cache schema unchanged.

**Validation limit / next gate**

File edits and source inspection only; Element0 still has no Python/shell execution tool. The 239 passes describe the pre-correction run, not a passing new baseline. No reinstall is required. Run the full compile/test sequence after applying the correction:

```powershell
python -m compileall -q main.py weather_terminal tests
python -m pytest -q
```

If green, continue the existing restrained Oxford, MS live-check/restart/offline workflow. Phase 3 remains open until real patched execution and live observations are supplied.


### Phase 3 second Windows run — passing suite and Oxford hourly display

**Execution evidence supplied by Jake**

- `python -m compileall -q main.py weather_terminal tests`: completed without reported errors.
- `python -m pytest -q`: **263 passed in 19.06s**, no failures.
- `python .\main.py`: invoked; supplied screenshot captures the dashboard with **Oxford, MS**, selected forecast point **34.3663773, -89.5187664**, local CDT/UTC offset, and twelve hourly slots spanning the displayed local dates.
- Screenshot shows successful retrieval/check timestamps distinct from forecast update time and last attempt, with precipitation explicitly identified as probability. Current conditions and alerts remain unavailable; header alert status remains **NOT VERIFIED**.
- Screenshot confirms the hourly-display smoke check and task-construction correction. It does not independently establish remembered restart, manual refresh, an offline attempt/recovery, ambiguity selection/cancellation, all terminal sizes, or an authoritative numerical comparison with the provider.

**Checkpoint actions**

- Update README, ARCHITECTURE, current status/progress and acceptance summary with this actual passing baseline and observed live display.
- Documentation changes only. No application code, tests, dependency pins, schema, or product requirements changed; no rerun is needed merely to record the results.
- Element0 did not execute Python or program-level provider requests. These are user-supplied execution/screenshot results.

**Remaining Phase 3 gate**

1. Confirm remembered location/cache on quit/restart and manual hourly refresh with R; no repeat Oxford geocoding is needed.
2. Confirm offline restart preserves dates and successful timestamps while the failed check remains visible, then reconnect and R to recover.
3. Confirm ambiguity selection/cancellation and wide/narrow scrolling/input/help/quit. One deliberate query with cached reuse is sufficient; avoid unnecessary public-provider traffic.
4. Close Phase 3 after these confirmations, then begin Phase 4 observations/station policy, daily forecasts, and official alerts. Those paths are not implemented or accepted at this checkpoint.


### Phase 3 final confirmation and closure

Jake explicitly confirmed Oxford remembered and R refresh, “Everything works” for offline retention/recovery, and “Everything works” for ambiguity/cancellation/remaining interaction. Close Phase 3 using that confirmation plus prior clean compilation, **263 passed in 19.06s**, and Oxford hourly-display screenshot. No additional run is inferred or required for documentation-only recording. Begin Phase 4 at Jake's request; no spec/dependency change.

### Phase 4 authoring checkpoint — complete weather data paths

**Deliverables authored**

1. `units.py`: shared strict finite/missing/zero temperature, speed, angle and percentage conversions. Hourly now shares number/temperature/precipitation helpers while retaining its wind representation/validity-selection behavior.
2. `conditions.py`: typed station/list/observation/conditions, station identity and coordinates, calculated haversine distances, station rank/filter, required usable temperature/time, optional-field resilience, NWS feels-like precedence, source-age rendering and cached reconstruction.
3. `services/observations.py`: at most nearest five candidates, ≤two joined logical probe tasks; nearest nonfuture fresh usable station, otherwise newest usable past observation with stale/failure note. Routine fresh station reuse, stale/unusable re-evaluation, metadata-refresh reranking, no forecast substitute. Partial request failures do not discard a usable candidate; no usable candidates retain last-good conditions.
4. `daily.py`: day/night normalization and complete source text, seven local start-date rows, missing today high/night/future coverage labels, local-date duplicate semantic check, DST/UTC fallback, original expired dates retained. No extrapolation from hourly data or observed-minimum claim.
5. `alerts.py`: complete authoritative Actual sets, null-geometry retention, known nonoperational exclusion, unknown status ambiguity preventing all-clear, unknown official enums retained/labeled, official ID/latest-sent dedup, explicit update/cancel reference handling, severity/urgency order, event times and exact complete instructions. Successful empty response clears old list; malformed/partial response cannot. Disk-origin/error/stale checks immediately unverified; known expiry never becomes an unconditional all-clear.
6. NWS adapters now retrieve discovered daily/stations, constructed safe station latest observations, saved-coordinate point alerts. Responses are semantically validated before result/persistence. Raw source/metadata/provenance schema stays v1; no synthetic production payloads.
7. `WeatherSlice` independent component states/nonoverlap, shared/coalesced metadata and serialized commits, all-component semantic cache restore, per-component failure preservation and generation rejection, matching cached-station metadata fallback. Refreshing metadata no longer replaces entire snapshot and erases independently refreshed weather.
8. Basic Textual integration: all four panels, immediate alert header updates, startup/manual/independent basic timers, coalesced owned tasks, cancellation/client cleanup. A/D now provide complete whole-set/seven-date source-text snapshots with clear non-live disclaimer. Final tables/per-alert/day selection still Phase 5.
9. HTTP alert-family ≥30-second gate implemented now across points/retries/redirects/manual requests; eligibility waits release provider lock. Existing deadlines/TLS/destination/retry policy retained. Station/observation 404 is classified missing data rather than unsupported city. Conservative serialized NWS wire operations remain; logical station probes are bounded independently. Conditional HTTP freshness/304/backoff orchestration still Phase 6.
10. New synthetic helpers and station/daily/alert/full-component tests; expanded HTTP and import-no-I/O regressions. Historical hourly UI tests restrict internal enabled-component seam rather than silently exercising only one component in production. Full-component tests exercise default four-component startup, outages, cache, writes, races, details, headers and quit. No arbitrary public API-dependent test.
11. README and ARCHITECTURE rewritten for actual authored behavior, station/alert safety rules, controls, source snapshots, basic schedules, cache/recovery, pending validation and design limitations. This plan records Phase 3 closure and Phase 4 gate. Existing config gains only template wording; settings/pins/schema/spec unchanged.

**Inspection actually performed / limits**

- Reviewed spec, models, normalization/storage/HTTP/coordinator/UI and historical tests before implementation.
- Read NWS OpenAPI endpoint definitions and NWS alerts documentation. OpenAPI extraction was truncated before full component schemas; do not claim exhaustive schema conformance from that read. Alerts docs recommend requests no more often than every thirty seconds; the live adapter is protected immediately rather than waiting for Phase 6.
- File edits and static review only. **No Python/shell command, compilation, pytest, new install, or program-level live observation/daily/alert request was executed by Element0.** Existing 263-pass baseline belongs to Phase 3, not this patch. No new expected passing count asserted.

**Known deferrals / boundaries**

- All required component paths are wired into plain text; polished selectable tables, per-alert/day choice, location details and final keyboard/accessibility polish remain Phase 5. Current full source text views are snapshot-only and scrollable.
- Conditional validation, HTTP freshness, extended failure cadences/next retry, 404 rediscovery and full scheduler remain Phase 6. Thin independent timers/30-second alert gate are necessary safety/integration work, not a claim that Phase 6 is complete.
- Station metadata may skip unusable records with notes; malformed collection fails. Incomplete paginated station/alert collections fail rather than being accepted as authoritative full data. Future pagination support must preserve validation/destination semantics.
- Observation cache persists raw body, reconstructing station name/distance from metadata or station ID/geometry fallback. Missing station context stays unavailable; no invented name/distance. Session selection notes are not persisted as official source content.
- Unknown alert status is not guessed Actual; it is explicitly noted and blocks verified all-clear. Unknown severity/urgency/certainty in actual records remain visible.
- No fresh-venv or finished-product acceptance at this checkpoint.

**Phase 4 validation gate — same activated venv, no reinstall**

```powershell
python -m pip check
python -m compileall -q main.py weather_terminal tests
python -m pytest -q
```

If green, `python main.py` with the already remembered Oxford point; no repeat geocoding required:

1. Inspect current station identity/distance/observed time versus retrieval/check. Missing source/fields or stale station data may be legitimate; note any error/selection limitation. Check sustained/gust/feels-like labels.
2. Inspect seven local dates, daytime high/overnight low, separate day/night conditions/probabilities; D should show complete source text in a scrollable snapshot. Missing already-ended daytime period must not be synthesized.
3. Inspect header/alert set. Recent successful empty set may show No active alerts for this point; failed/cached sets must show unverified. A shows complete official text if supplied, without requiring severe weather to exercise the feature. Use fixtures for nonempty/expiry cases.
4. Check R all-component refresh, help/A/D snapshots, scrolling/resizing at wide/narrow sizes and clean quit. Manual provider wait must not bypass thirty-second alert spacing.
5. Optional/acceptance outage check after a successful retrieval: quit/disconnect/relaunch; all usable caches keep original dates/successful times, alerts immediately unverified, component failures independent. Reconnect/R recovery without repeated city search.
6. Supply full output and observed live behavior; fix failures before closing Phase 4. Mark only actually supplied evidence, then begin Phase 5 presentation/details.

**Gate remains open pending real execution.**


### Phase 4 follow-up — live station-pagination acceptance and display precision

**Evidence supplied by Jake**

- Baseline: `python -m pip check` reported no broken requirements, compilation was clean, and pytest reported **348 passed in 20.16s**.
- Live conditions initially failed with “NWS station list is invalid.” PowerShell confirmed 28 stations and a redundant ID-filtered continuation link.
- After the narrow pagination exception: clean compilation and **388 passed in 20.09s**, then normal launch and Jake's confirmation: “All tests passed and the app seems to function well.” Live conditions now populate.

**Display-precision patch authored; not executed**

- Shared `formatting.py` for one-decimal temperatures/wind speeds and whole-number percentages/wind directions, retaining missing/zero distinctions and suppressing rounded negative zero.
- Conditions, hourly/daily rows, and scalar/range/quantitative forecast winds use the same display rules; full official text is untouched.
- New helper/rendering regressions cover fractional, negative, zero, missing, cached/live presentation, unchanged raw source bodies and normalized numeric fields. Existing numeric-display assertions updated; import-no-I/O coverage includes the new module.
- README/ARCHITECTURE/plan updated with actual 388-test evidence and this unexecuted patch. No dependency, configuration, cache schema, source-provenance, or product-scope changes.

Next: rerun compilation/full pytest, visually check the remembered-location dashboard's precision, then review and make the first Git commit if clean. Element0 did not execute Python or Git and has not made a commit. Remaining phase gates are not closed by this presentation patch.
