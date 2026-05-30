# Changelog

All notable changes to **polestar-dash**. Format roughly follows
[Keep a Changelog](https://keepachangelog.com/); dates are UTC.

## [1.10.2] — 2026-05-30

### Fixed — `_stations is not a function` broke the sessions list

- Root cause of the *"Could not load charging sessions"* error: the dropdown
  helper was a top-level `function _stations()` while the data was stored in
  `window._stations`. At browser **script scope** a function declaration and a
  same-named `window` property are the *same* global slot, so assigning the
  station array (`window._stations = […]`) overwrote the function — the next
  `_stations()` call threw `_stations is not a function`. (It never reproduced
  in Node because function declarations there don't attach to the global
  object; confirmed both directions with `vm.runInContext`.)
- The station list now lives in a dedicated block-scoped `let _knownStations`,
  so the function and the data can't collide. Also keeps the v1.10.1 hardening:
  stations load in their own try/catch and the panel shows a real error + retry
  instead of sticking on *Loading…*.

## [1.10.1] — 2026-05-30

### Fixed — Sessions panel could stick on "Loading…"

- `loadSessions()` had no error handling, so a single failed fetch — e.g. the
  few seconds the container is down during a redeploy — left the charging-
  sessions panel stuck on *Loading…* forever with no error and no retry. It now
  catches failures, shows a message and a **Retry →** link, and logs to the
  console. (The dropdown/data path itself was fine — verified against the live
  endpoints.)

## [1.10.0] — 2026-05-30

### Added — Location dropdown on charging sessions, GPS-optional places

- The charging-session **Location** field (renamed from *Note* / *Notiz* →
  *Ort*) is now a **dropdown of known places**. Picking one applies its saved
  price; an explicit **"+ New place…"** entry keeps free-text input and
  remembers what you type.
- Known places no longer require GPS. `ChargingStation.lat/lng` are now
  nullable, so a place can be a **name + price only** entry — add or edit them
  directly on `/settings` → *Charging stations* (new *Add place* form), or by
  saving a new place from a session. A forward-only, guarded, atomic migration
  relaxes the old `NOT NULL` constraint on existing databases.
- GPS auto-tag still works when imported-trip coordinates are available;
  `upsert_station` now also matches by name, so a name-only place gets
  **upgraded** with coordinates instead of duplicated.
- Fixes the reported case: typing a known place (`Heim`) didn't pull its known
  price. Background: the consumer API exposes **no live GPS**, so a session's
  location can only be inferred from an imported journey-log CSV within ~4 h of
  the charge — the dropdown removes that dependency for the common case.

### Fixed — Stale tests (no production impact)

- `test_tripbook.py` pinned trip data to a hardcoded 2026-05-28 date while
  `detect_trips(days=1)` filters relative to *now*; once the wall clock passed
  that date the fixtures fell outside the window and detected zero trips. Bases
  are now `utcnow()`-relative so they never drift again.
- `test_fetcher.py`'s fake API object predated the gRPC telematics rewrite and
  several `CarInformationData` fields (`internal_vehicle_identifier`, battery
  object, …); updated to match the current `fetch_snapshot` contract.

## [1.9.1] — 2026-05-29

### Fixed — ntfy notifications re-fired on every poll

- `_send_ntfy` logged `kind`/`key` that aren't in its scope, so the line raised
  `NameError` **after** a successful POST. The channel wrapper caught it and
  bailed before writing the dedup row — so every ntfy alert was actually sent
  but never marked delivered, re-firing on each poll and logging a spurious
  "send failed". Now logs the title; the dedup row records correctly. (Found by
  enabling ruff — `F821 undefined-name`.)

### Fixed — A few strings were still English

- `/raw` was never wrapped for translation (missed in the v1.9.0 sweep) and the
  vehicle-card battery label had one unwrapped branch. Both are German now.

### Changed — Lint cleanup (ruff)

- Activated the already-configured ruff lint and made the tree pass: removed
  unused imports, sorted imports, modernised `datetime.timezone.utc` →
  `datetime.UTC`, added `raise … from …` in two handlers, simplified one
  branch. Config: line length is left to the formatter (`E501` not linted), and
  FastAPI's `Query`/`File`/`Body`/… are whitelisted for `B008` (framework
  idiom). Run with `ruff check src tests`.

## [1.9.0] — 2026-05-29

### Added — German UI + notifications, with a language switch

- The whole interface and every notification/alert message are now available
  in **German (default)** and **English**, switchable on `/settings` →
  *Language*. The choice lives in the database (app-wide, no restart) and also
  governs the wording of push/email alerts; the weekly digest is German.
- Lightweight, no build step: a `t()` translator (`i18n.py`) with the English
  source string as the lookup key (missing key → English fallback) and a
  German catalog (`translations_de.py`). ~390 UI strings across all 14
  templates (incl. the Lab page) plus the transactional notifications route
  through it. New endpoint `GET/POST /api/app-settings/language`.

### Fixed — Range-factor anomaly fired false alarms on a single noisy poll

- The `range_km` field from the Polestar API occasionally returns a transient
  garbage value for one or two polls and then self-corrects (e.g. 150 → 91 →
  150 km at an unchanged SoC). The low-range-factor check evaluated a **single
  snapshot**, so every alert it had ever produced was one of these spikes —
  including physically impossible readings like 0.74 km/% (40 km at 54%).
- The check now takes the **median km/% over the last 60 min** (≥5 samples)
  instead of one reading. Replayed against the full history this turns 4 false
  alarms into 0, while a genuinely sustained low factor (real cold/degradation)
  still alerts. The threshold itself (2.5 km/%) is unchanged — the bug was
  single-sample noise, not the cutoff.

## [1.8.3] — 2026-05-28

### Fixed — Tripbook consumption read far too low (10.9 vs ~15 kWh/100km)

- The detected-trips table mixed in the journey-log import's real kWh whenever a
  trip matched one. After the v1.8.2 split fix made detected trips *longer*
  (stops merged into one trip), each long trip matched only **one** of its
  several shorter journey-log legs — so a single leg's kWh was divided over the
  whole trip distance (a 119 km drive read as 7.2 kWh/100km). The blended grand
  average collapsed to ~10.9 kWh/100km against a real ~15.
- The detected-trips table (per-row kWh, monthly, and grand total) now uses a
  single consistent estimate — the car's own average consumption × distance —
  which tracks what the dashboard shows (≈ 15 kWh/100km). The real per-leg
  figures still live in their own "Journey log (Polestar app)" section with
  their own correct total. The per-row `est` marker is gone (the column is
  uniformly an estimate now) in favour of one caption above the table.

## [1.8.2] — 2026-05-28

### Fixed — One drive split into two trips by a stale odometer

- A single drive could show up as two `/tripbook` entries when the Polestar API
  delivered the odometer minutes behind the battery: the odometer *value* froze
  for several polls (looking like a >10 min idle gap) while the SoC kept ticking
  down, so `detect_trips` cut the drive in two. A real case: odometer flat
  14:58→15:23 while SoC fell 52→45 %.
- A trip now only ends when the car charges, **or** has been idle past the
  threshold **and** the SoC has gone flat. While the SoC keeps falling
  (≥ `ACTIVE_SOC_DROP_PCT` below the level at the last observed movement) the car
  is treated as still driving, so a back-filled odometer — or a short stop in
  traffic — no longer splits the trip. A `HARD_IDLE_CEILING_MIN` (90 min) cap
  stops a stuck reading from holding a trip open indefinitely. Trips are
  recomputed live from snapshots, so this applies retroactively to all history.

### Changed — Tripbook table shows per-trip energy, not a per-row rate

- The per-row "kWh/100km" column (which rendered as e.g. `14.8 est`) is gone.
  Each row now shows the trip's **absolute energy in kWh** — the real
  journey-log value when matched, otherwise the SoC-based estimate with a small
  `est` marker. The fleet kWh/100km average is shown once in the page header
  ("Energy used" card) instead of being repeated on every row.

## [1.8.1] — 2026-05-28

### Fixed — Imported trip matched to multiple detected trips

- `/tripbook` paired each detected trip with its closest imported journey-log
  trip independently, so when two detected trips fell near the same import (e.g.
  a back-to-back short + long drive) both claimed it. The import's real kWh and
  cost then appeared on both rows **and** were double-counted in the monthly and
  grand totals. Matching is now one-to-one (greedy by smallest time gap): each
  import backs at most one detected trip; the runner-up falls back to its own
  estimate.

## [1.8] — 2026-05-28

### Added — App version shown in UI

- The nav now renders the real package version (`v{{ app_version }}` from
  `polestar_dash.__version__`) instead of a hardcoded `v1`, so deployments are
  identifiable at a glance.

### Added — CSV journey-log enrichment

- **Note column on `/tripbook`.** The imported journey-log table now surfaces
  three CSV fields we previously stored but never displayed: `Category` (shown
  as a badge when not "Uncategorized"), `Trip Type` (shown when not "SINGLE"),
  and `Comments`. The trip's comment also appears in its map popup.
- **Odometer gap detection.** Consecutive imported trips are compared by
  odometer: when a later trip's start odometer exceeds the previous trip's end
  odometer, those kilometres were driven but never recorded by the Polestar app.
  A new "Untracked km (odometer gaps)" block lists each gap (only shown when
  gaps exist), giving a ground-truth check on journey-log completeness.

### Fixed — Routes collapsed by over-broad address label

- The `/routes` aggregation resolves each trip endpoint through the
  `address_labels` setting, which matches by **substring**. A label pattern of
  `"Austria"` therefore matched every Austrian address and collapsed all trips
  into a single "Arbeit → Arbeit" route. Removed the offending pattern from the
  stored setting; routes now differentiate correctly. (Data fix — the substring
  matcher itself is unchanged; see note below.)

### Added — Software version tracking + update watch

- **New `/software` tab.** Shows the car's currently installed software
  version and *since when*, a full history of every detected version change,
  and the release-notes changelog scraped from the Polestar online manual.
  Linked from the main nav.
- **Manual version entry.** The Polestar consumer API returns
  `software_version: null` for many cars (incl. MY2026), so the tab has a small
  form to enter the version read in-car (Settings → System → Software update).
  It is stored as a version-change record and drives the "since when" /
  update-available logic; auto-detection from the API takes over automatically
  if it ever starts reporting a value. `POST /api/software/installed`.
- **Readable release notes.** The manual embeds the changelog as both rendered
  HTML and a JSON blob; the parser reads only the HTML titles and converts
  paragraphs / sub-headings / lists into clean line-broken text with bullets.
- **Installed-version history.** New `SoftwareVersionChange` table; the poll
  pipeline records every change of the car-reported `software_version`
  (`record_installed_version` in `software.py`) and notifies (`software_installed`)
  on each OTA install. The first version observed per VIN is logged as a
  silent baseline.
- **Update watch (2× daily).** New `software-update-check` scheduler job scrapes
  `SOFTWARE_MANUAL_URL` (default the Polestar 2 MY2027 manual) at 06:00/18:00
  UTC, caches the release catalog (`SoftwareRelease`), and **emails** when a
  version newer than the installed one is listed (`software_update_available`,
  deduped per version via `NotificationLog`).
- **`GET /api/software`** — JSON: installed version, since, latest available,
  `update_available`, change history, and the release catalog.
- **Config:** `SOFTWARE_MANUAL_URL` env var (overridable).

### Fixed — Email deliverability

- Outgoing mail now sets **`Date`** and **`Message-ID`** headers and is sent as
  **multipart/alternative** (plain text + HTML) instead of HTML-only. Missing
  Date/Message-ID and HTML-only bodies are strong spam signals — Gmail was
  filing alerts/digests into Spam (or dropping them). Affects all channels that
  email (software alerts + weekly digest).

## [1.7] — 2026-05-28

### Fixed — Robustness pass (project review HIGH/MED)

- **Migration that never ran**: the `chargingstation.price_ct_per_kwh`
  `ALTER TABLE` was appended to the plan *after* the execution loop, so it was
  never applied to existing DBs. Moved before the loop.
- **Session energy used the wrong battery capacity**: completed-session
  `energy_kwh` was computed against the hardcoded 78 kWh while the rest of the
  app uses the calibrated / car-reported pack (~83 kWh) — per-session kWh were
  systematically ~7 % low. Now uses the same capacity basis as the tripbook.
- **Silent charges shredded into many tiny sessions**: a heuristically-opened
  (inferred) charge has no terminal API state — its status is `Idle`
  throughout — so a flat-SoC poll closed it mid-charge. Inferred sessions are
  now flagged (new `ChargeSession.inferred` column) and close only on a SoC
  drop, the car driving off, or the 24 h timeout.
- **SoC-drop anomaly spammed**: its dedup key embedded the poll minute, so a
  persistent drop re-alerted every poll. Now keyed per day like the sibling
  checks.
- **Notifications lost on partial failure**: a single channel succeeding marked
  the whole event delivered, so a transient failure on another channel dropped
  it permanently. Dedup is now per channel — each records its own row only on a
  successful send, and a failed channel backfills on the next call.
- **Blocking I/O on the event loop**: `notify()` (ntfy HTTP, SMTP up to 15 s,
  Telegram) ran on the scheduler's event loop. It and the anomaly checks now
  run via `asyncio.to_thread`, and Telegram is a synchronous send (was
  fire-and-forget and could be GC'd before completing).
- **Weekly digest email bypassed dedup**: the HTML email was sent directly,
  so a re-run in the same week re-sent it. Now routed through `notify()`; the
  week key uses `isocalendar()` consistently with the title.
- **Polling wedged after a failed startup login**: `connect()` left the client
  empty and was never retried, so the loop polled-and-failed until a manual
  restart. `poll_once` now reconnects when the client isn't connected.
- **Non-atomic backup**: a crash or same-day re-run could leave a truncated
  file under the canonical name. Now writes to `.tmp` and atomically renames.

### Fixed — Tripbook trip duration collapsed to a few minutes

`detect_trips()` only treated an increase in the odometer *value* as
motion. Polestar refreshes the odometer minutes after the fact and in
batches, while `odometer_event_ts` (the car's own reading timestamp)
advances on every poll the car is moving. So a drive whose odometer
ticked once read as a 3-minute trip even though the event timestamps
spanned ~20 minutes.

- An advancing `odometer_event_ts` now counts as motion in its own
  right: trip start anchors to the first movement event, and the trip
  stays open while the event timestamp keeps advancing (even if the
  odometer value has plateaued).
- Idle time is measured in `fetched_at` space so the poll cadence and
  the event-timestamp lag no longer fragment one continuous drive into
  a string of sub-threshold hops.

### Fixed — Tripbook per-trip energy read up to 2× too high

SoC is reported in whole percent, so on a short trip a single 1% step
(~0.8 kWh on this pack) dwarfs the real energy — a 7.8 km hop showed
32 kWh/100km against the car's ~15. Trip energy now derives from the
car's own `avg_consumption_kwh_per_100km × distance`, falling back to
the SoC-delta estimate only when the car reports no consumption figure.
(The cumulative `total_energy_consumption_wh` counter, which would give
an exact per-trip delta, is never populated by this car's API.)

### Changed — Tripbook kWh/100km column is now uniform

The rate column previously mixed two methods — journey-log-matched
trips showed the real per-trip rate (orange) while unmatched ones showed
the estimate — so old and new rows read inconsistently. It now always
shows the trip's own estimate (`est`). The absolute **kWh (real)** column
(total trip energy from the imported journey-log CSV) and the
real-preferred cost/totals are unchanged.

## [Unreleased] — 2026-05-27

### Added — Smart automations

**Session auto-tagging**: when a charging session closes, the app
automatically looks up the GPS location (from nearby imported journey
endpoints) and matches it against known charging stations (500 m radius).
If a match is found and no manual note was set, `note` and
`price_ct_per_kwh` are filled automatically. No more manually tagging
every home charge.

**Weekly digest** (Sundays 17:00 UTC): summarises the past 7 days —
trips, km, kWh charged, cost, efficiency (kWh/100km), and range factor.
Sent via all configured notification channels (email, ntfy, Telegram).

**Battery health trend alert**: runs alongside the weekly digest.
Compares the 4-week rolling average range factor against the 12-week
baseline. If it drops more than 10%, fires a warning notification
(possible causes: cold weather, tyre pressure, degradation).

**Trip-to-session cost linking**: the tripbook now matches each detected
trip to the charging session that started within 2 hours after the trip
ended. When matched, the "Cost" column shows the actual charge cost
(from the session's effective price) instead of just the global estimate,
and a new "Charged at" column shows the station name.

### Added — Email notifications (SMTP)

New notification channel: email via SMTP with STARTTLS. Compatible with
Gmail App Passwords, Fastmail, any standard SMTP relay. Configure via
`SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`,
`SMTP_TO` environment variables.

`notify()` is now the single hub for all channels — ntfy, email, and
Telegram are each sent if configured, with the same dedup key. Anomaly
alerts, charge-complete, low-SoC, service-due, weekly digest, and
battery-trend alerts all go through this unified path.

### Changed — Notification architecture

All callers (`scheduler._maybe_notify`, `anomalies._alert`,
`digest.weekly_digest`) now go through a single `notify()` call that
handles ntfy + email + Telegram in one shot. Removes duplicate
`telegram_send()` calls that were scattered across callers.

## [Unreleased] — 2026-05-26

### Added — Charging Hero dashboard mode

When the car is actively charging, the dashboard replaces the normal SoC +
Charging cards with a full-width **Charging Hero** panel:

- **Animated battery bar**: filled portion up to current SoC with a glowing
  orange leading edge, energy-flow ticks and a sweep animation through the
  uncharged zone. The animation stops at the target SoC (not 100%).
- **Target SoC marker**: vertical line + label on the battery bar at the
  configured charge limit.
- **VU-meter gauges** for charging power (kW) and voltage (V) — horizontal
  gauge bars with orange fill, needle indicator and min/mid/max scale.
  Ranges adapt automatically (0–22 kW for AC, 0–150 kW for DC).
- **Smart time estimate**: shows "To target" (estimated from `minutes_to_full`
  × remaining-to-target ratio) when target < 100 %, with the full-charge
  time as a secondary badge. Falls back to "To full" when no target is set.
- **Minutes → hours/minutes**: all time displays formatted as `Xh Ym`.
- Stats row: current (A), range (km), target (%) — all at a glance.
- Header badges: charging status, type (AC/DC).
- Ambient orange glow on the hero card, pulsing subtly.
- Full mobile responsiveness (VU meters stack vertically on small screens).
- `prefers-reduced-motion` disables all animations.

When charging stops, the dashboard reverts to the normal 4-card grid.

### Fixed — correctness and robustness pass (16 fixes)

**Bugs:**
- **Operator precedence bug** in session detail (`/api/sessions/{id}`):
  `end_ts` calculation parsed as `(A or B) if C else D` instead of
  `A or (B if C else D)`, producing wrong duration when `ended_at` was
  set but `snaps` was empty.
- **Falsy-vs-None checks** in `fetcher.py`: `odometer_meters == 0` and
  `charger_power_status_raw == 0` were silently mapped to `None` because
  `if val` is false for zero. Changed to `if val is not None`.
- **Detached SQLModel object**: `prev` snapshot was loaded in a session
  that closed before `update_session` accessed its attributes. Added
  `expunge(prev)` to make it safe across session boundaries.

**Robustness:**
- **Stale charge sessions**: sessions stuck open forever when the API
  returned `None` (not a terminal state) for `charging_status`. Now
  auto-closed after 24 h without a charging signal.
- **Backup connection leak**: `sqlite3.connect()` handles were not closed
  on exception in `backup.py`. Wrapped in `try/finally`.
- **Journey CSV upload**: file was fully read into memory *before* the
  5 MB size check. Now streams in 64 KB chunks and bails early.
- **`s.exec()` on Core DELETE** in raw-snapshot pruning: replaced with
  `s.execute()` (the correct method for DML statements).

**Performance:**
- **Composite index** `(vin, fetched_at)` on `VehicleSnapshot` — the
  hot query (`previous_snapshot`) runs on every poll and could not use
  the two individual indexes efficiently. Added to model + migration.
- **`_battery_kwh()` cached** (5 min TTL): was opening a new DB session
  on every `trip.kwh_used` property access (~730 round-trips for a year
  of trips). Now a single query per 5 minutes.
- **CSV export streams** rows instead of materialising the entire result
  set in memory (could be 260k+ rows for multi-year exports).
- **Trip↔import matching** uses `bisect_left` (O(n log n)) instead of
  a nested loop (O(n×m)).

**Housekeeping:**
- **`datetime.utcnow()` replaced** across 13 files with a central
  `util.utcnow()` that returns naive UTC without Python 3.12+
  deprecation warnings.
- **Idle drain** now uses the calibrated battery capacity instead of the
  hardcoded `DEFAULT_BATTERY_KWH` constant.
- Removed unused `session_scope()` from `db/session.py`.

### Changed — route clustering radius parameter
- `/routes` accepts a `radius_km` query parameter (default 1.0 km) to
  control the clustering tolerance for repeated routes.

## [Unreleased] — 2026-05-23

### Added — Address map in `/settings`
- New Leaflet/OSM map card on `/settings` shows every unique start/end
  address from imported trips. Colored dots = already labeled, grey =
  unassigned. Click a dot to assign it to an existing label or create a
  new one — one label can hold many addresses, so e.g. `Arbeit` can cover
  multiple parking spots near the office. Optional 500 m radius overlay
  and a filter to show only unassigned points within 500 m of a labeled
  one. Pattern is editable in the popup before saving.
- Backend: `GET /api/address-points` (unique address+lat+lng with visit
  count and current label) and `POST /api/address-labels/assign`
  (`{pattern, label}` — moves a pattern to a label or unassigns it).
- Dedup tolerance set to ~11 m so GPS jitter on the same physical
  location collapses to a single marker.

## [Unreleased] — 2026-05-21

### Removed — aWATTar / EPEX spot prices and `/planner`
- `prices.py`, `planner.py` and the `/planner` page deleted; `/api/plan` and
  `/api/prices` endpoints gone; `AWATTAR_COUNTRY` removed from config. Costs
  now come exclusively from the per-session price overrides and the default
  ct/kWh set in `/settings`.

### Fixed — Tripbook merged parked time into trips
- After detection, any trip whose implied average speed is below 8 km/h
  gets its `started_at` trimmed using a realistic 40 km/h average. Catches
  the case where Polestar back-fills an old odometer reading and bumps the
  event timestamp to "now", which sneaks past the phantom-moved guard.
- Phantom-moved guard added in the main loop: an odometer jump whose
  `odometer_event_ts` doesn't advance past the last one we saw is treated
  as a delayed reporting, not a fresh trip.

### Added — kWh fallback for short trips
- Trips with km > 0 but no SoC tick (rounded to 1%) now show an estimated
  kWh in the tripbook based on a fleet-average kWh/km derived from your
  imported journey-log CSV (or `0.18 kWh/km` baseline if no imports yet).

### Fixed — Polestar API outage (chargingStatus field removed)
- **Polestar removed `BatteryV2.chargingStatus`** from their GraphQL surface
  around 2026-05-21 08:16 UTC. Every telematics call returned
  `FieldUndefined: Field 'chargingStatus' in type 'BatteryV2' is undefined`
  and `pypolestar` 1.12.2 (latest on PyPI) still asks for it, so polling
  stopped feeding the DB.
- **Hotfix**: `polestar/client.py` now monkey-patches pypolestar at import
  time — drops `chargingStatus` from the `QUERY_TELEMATICS_V2` document and
  makes `CarBatteryData.from_dict` tolerant of the missing key.
- **Charging status now sourced from gRPC** (`grpc_battery.charging_status`,
  which still works) with a final fallback derived from `charging_power_w`
  and `charger_connection_status`.

### Added — widened gRPC battery decode (7 new fields)
- `battery.proto` defines 26 fields; pypolestar's `_parse_battery` only
  mapped 13. We now monkey-patch the parser to attach the extra fields to
  the returned dataclass via `object.__setattr__` (bypassing `frozen=True`).
- New columns on `VehicleSnapshot`:
  `range_miles`, `avg_consumption_kwh_per_100km_automatic`,
  `avg_consumption_kwh_per_100km_since_charge`,
  `total_energy_consumption_wh` (+ `_automatic`, `_since_charge`),
  `charger_power_status_raw`.
- Forward-only migration in `db/session.py` picks them up on existing
  SQLite files without losing history.

### Added — Polestar Lab (`/lab`)
- New page for live API field discovery, all panels auto-load on entry:
  - **Latest snapshot grid** — every field of the most recent poll with
    populated values in orange and null/zero in grey; click-to-copy.
  - **GraphQL probe** — bulk-tests candidate field names against a chosen
    parent type and classifies each as `ok` / `undefined` /
    `shape_hint` (= the field exists but needs sub-selection) / `error`.
    Hits the gql session directly so the original
    `TransportQueryError.errors[].message` is preserved (pypolestar's
    `_query_graph_ql` wraps everything in a generic `PolestarApiException`).
    Default candidate lists per parent so the page is useful without typing.
  - **gRPC battery — raw proto dump** — shows the full protobuf repr of the
    last C3 response so you can see every field Polestar returned, even
    the ones pypolestar drops.
  - **Proto catalog** — parses `pypolestar/proto/*.proto`, lists every
    message and field; "used by us" marker for what flows into our DB.
  - **Upstream pypolestar** — installed version vs PyPI latest vs GitHub
    HEAD commit, plus most-recently-updated open issues.
  - **Volvo Developer API** reference — table summarising which Volvo APIs
    work with Polestar 2 VINs (only Energy v1; Connected/Extended are
    legacy-Volvo only) with documentation links and endpoint shapes.
  - **Known queries** — pypolestar's GraphQL source dumped inline.
  - **Reasoning log** — persistent markdown at `data/lab_notes.md`,
    appendable from the UI, survives container rebuilds (in the mounted
    `/data` volume).
- Backend endpoints: `/api/lab/probe`, `/api/lab/grpc-battery-raw`,
  `/api/lab/proto-catalog`, `/api/lab/upstream`, `/api/lab/known-queries`,
  `/api/lab/notes` (GET + POST).

### Changed — sessions and history merged
- `/history` now renders the charging-sessions block at the bottom of the
  page (charts on top, sessions below) — fewer page hops for the typical
  "skim the day's data" flow.
- `/sessions` becomes a `307` redirect to `/history#sessions`; the nav
  drops the standalone "Sessions" entry. `/sessions/{id}` (per-session
  charging curve) is unchanged.
- Sessions tile padding tightened from 24 px to 16 px and the stat font
  shrunk from 28 px to 24 px so the merged page stays compact.

## [Unreleased] — 2026-05-20

### Added — mobile + Grafana v2
- **Mobile / iPhone layout.** New responsive rules in `ujh.css`: single-column
  grids below 640 px, topbar wordmark hidden, nav becomes a horizontally
  scrollable strip with a fade hint, stat tiles shrink from 44→32 px,
  inputs forced to 16 px (prevents iOS auto-zoom on focus), `env(safe-area-inset-*)`
  honored for the iPhone notch / home indicator. Add-to-Home-Screen via Safari
  launches it as a standalone PWA — manifest and apple-touch-icon were already
  in `base.html`.
- **Grafana dashboard v2.** New panels added to the drop-in JSON:
  - 4-up gauge cockpit (SoC / power / voltage / current) as the top row
  - State timeline of `poledash_charging` (orange = charging, grey = idle),
    full-width — the day/week story at a glance
  - Heatmap of charging-power distribution over time (Oranges color scheme)
  - XY-scatter "Charging curve · power vs SoC" — reveals where the car
    throttles as SoC approaches the target
  - Dashboard-level annotations on charge start (orange) and stop (grey),
    auto-detected from `poledash_charging` via PromQL edge detection
    (`(metric == 1) unless (metric offset 2m == 1)`)

### Fixed (later that day)
- **Insights page crashed silently** with
  `TypeError: Cannot convert undefined or null to object` because a local
  helper `function P()` was being shadowed by `window.P` from the date-fns
  CDN bundle — date-fns exports one-letter globals (their long-date format
  helper is `P`). Renamed to `palette()`; one-letter top-level helpers on
  chart pages are now off-limits.
- **Light theme: chart lines invisible.** `history.html` hardcoded
  `INK = '#e8e8ea'` etc. — white-on-white on the light palette. Chart pages
  now resolve `--ujh-*` CSS variables fresh on every refresh.
- **Theme toggle now repaints charts.** The topbar Theme button dispatches a
  `themechange` event; `history` re-runs `refresh()` (cheap), `insights` and
  `session_detail` do a clean `location.reload()`.

### Added — insights, anomalies, Grafana

- **Per-session charging curve** (`/sessions/{id}`, `GET /api/sessions/{id}`):
  professional dual-axis chart with power (kW) and SoC (%) over time, plus
  a second plot for current (A) and voltage (V). Eckdaten als Stat-Karten
  (SoC, Dauer, Peak/Avg kW, Energie · €). Auto-reload alle 30 s solange
  die Session noch live ist. Verlinkt von jeder Session in `/sessions`.
- **Insights page** (`/insights`, `GET /api/insights`):
  - Wh/km vs. avg-speed Scatter pro Trip
  - Avg Wh/km pro Wochentag und pro Stunde des Tages
  - Wöchentliche €/100 km mit Bar (€) + Linien-Overlay (€/100 km) und
    Tabelle (kWh / km / € / €/100 km)
  - **Idle-Drain-Detector** (`idle_drain.py`, `GET /api/idle-drain`):
    SoC-Drop ohne Fahrt ohne Laden → Pre-conditioning / Standheizung /
    Vampire-Loss. Tabelle mit Start, Dauer, Drop, geschätzte kWh.
- **Anomaly watchdog** (`anomalies.py`): pro Poll laufen Heuristiken,
  Treffer feuern ntfy + Telegram über die bestehende `notify()`-Dedup.
  - SoC-Drop ≥ 8 % zwischen zwei Snapshots ohne Fahrt + ohne Laden
  - Lade-Leistung kollabiert: von > 30 kW auf < 3 kW innerhalb einer Session
    (Charger-Fault / Thermal-Throttle), eine Alarmierung pro Session-Tag
  - Range-Faktor < 2.5 km/% (Kälte, Reifendruck, Degradation), eine
    Alarmierung pro Tag
- **Grafana drop-in dashboard** (`deploy/grafana-dashboard.json`,
  spiegelt auf `/static/grafana-dashboard.json` für Download via UI):
  12 Panels — SoC-Gauge mit Thresholds, Range / Power / Charging-State
  als Stat-Karten, Zeitreihen für SoC / Power / Range / Verbrauch /
  Odometer, kombinierte Current+Voltage-Spur, Minutes-to-full,
  Service-due. Orange nur für Live-Lade-Signale, sonst neutrale Palette.
  Download-Button auf `/settings`.

### Changed
- **`poll_once` reschedules unconditionally.** Jeder Schritt (persist,
  session-update, notify, MQTT, anomaly-checks) ist isoliert und logged
  + recovered eigenständig; `_reschedule` läuft in `finally`. Vorher hat
  ein Crash zwischen Fetch und Reschedule den Scheduler dauerhaft
  abgewürgt (Vorfall: Logging-Bug in `sessions.update_session` hat ~1 h
  Poll-Stillstand verursacht).
- **Trip-Detection nutzt `odometer_event_ts` als Trip-Fenster**, wenn das
  Poll-Intervall ein unplausibles km/h impliziert. Vorher wurde die ganze
  Probe verworfen → bei sparse polling (10 min idle) wurde kein Trip
  erkannt, wenn 30 km zwischen zwei Polls zurückgelegt wurden.

### Fixed
- **`sessions.update_session` Logging-Bug**: griff bei Öffnen einer
  neuen Lade-Session auf `snap.soc_start` zu — das Feld existiert nicht
  am `Snapshot` (heißt `soc_percent`; `soc_start` ist `ChargeSession`).
  AttributeError beim ersten Charging-Snapshot. Jetzt `snap.soc_percent`.
- **History-Chart blieb leer**: `parsing: false` in `templates/history.html`
  ist nicht kompatibel mit dem `time`-Axis-Adapter, wenn `x` ein
  ISO-String ist → Chart.js zeichnete nur leere Achsen. Entfernt.

### Added — pricing model
- **Editable default price** in the web UI (Settings page). Starts
  at 50 ct/kWh. Persisted in a new `AppSetting` key/value table.
- **Per-session price override + note** on the Sessions page — every
  charging session can carry its own ct/kWh (and a free-text note like
  "IONITY A4") which beats the default for that session.
- Cost surfaces everywhere: Sessions totals (kWh + €), Tripbook (per-trip
  Cost column, monthly Cost column + grand total), Planner cost line
  now uses the user's tariff. Spot price stays visible as the cheapest-
  window indicator (timing only), separately from what you pay.
- `GET /api/app-settings`, `POST /api/app-settings/price`,
  `PATCH /api/sessions/{id}` (price / note).

### Added (later that day)
- **gRPC battery fields surfaced**: `charging_type` (AC/DC),
  `charging_voltage_v`, `minutes_to_minimum_soc`, plus
  `target_soc_percent` and `target_soc_type` (Daily / Long Trip /
  Custom). The new fields are pulled from
  `pypolestar.get_grpc_battery()` and `get_grpc_target_soc()`. gRPC
  values are also used as a fallback when GraphQL omits charging
  power/current and average consumption.
- Idempotent SQLite mini-migration in `db/session.py` (forward-only
  `ALTER TABLE ADD COLUMN`) so existing databases pick the new
  columns up on startup.
- Topbar live indicator: pulls `/api/latest` every 30 s and shows
  charging power / SoC + range / plugged state with an orange
  pulsing dot **only** while charging.
- Dashboard "Charge limit" panel (read-only) showing the target SoC
  from the car; Planner pre-fills target SoC from that value.

### Fixed
- **Service-warning enum mismatch.** pypolestar v1.12 returns the
  warning literals with a space (`"No Warning"`), whereas the
  templates were comparing against the old `"NoWarning"` form. As a
  result the "Service approaching" banner was triggered constantly
  and the Oil/Brake/Coolant badges always rendered in warning color.
  Both checks now tolerate either spelling.
- **Trip plausibility.** `detect_trips()` previously attributed
  multi-kilometer odometer jumps between sparse polls to the entire
  poll window, producing trips at 180+ km/h average. The detector now
  caps implied speed at `MAX_PLAUSIBLE_KMH = 180`; when exceeded, it
  reconciles the start time against `odometer_event_ts` (the car's
  own timestamp for the odometer reading) or drops the sample.
- **Idle dashboard polish.** The charging card no longer shows a
  stale AC badge / phantom `—` stat when the car is idle. New
  `.stat-muted` style for word-status (`Idle`, `All good`).

### Changed
- Dashboard layout: 4-card 2×2 status grid; Vehicle card promoted to
  a full-width row with a `.kv-row` metadata strip (VIN, Reg, SW,
  Battery, Torque) that hides empty fields.
- Stat sizes brought down: 44 px primary, 28 px secondary.
  Section header is now a vertical stack (num-tag above title) and
  no longer carries the 140 px fixed-width column that cramped
  narrow screens.

---

## [0.2] — 2026-05-20

### Added
- **Charge planner** (`/planner`, `GET /api/plan`): given current SoC, target
  SoC, departure time and charger power, picks the cheapest contiguous
  aWATTar window before departure, returns kWh needed, estimated cost in €,
  and savings vs. the forecast average.
- **Efficiency tracking per trip**: `tripbook.detect_trips()` now records
  `soc_start` / `soc_end`; each trip exposes `kwh_used`, `wh_per_km`,
  `kwh_per_100km`. Monthly totals aggregate kWh and a per-month
  `avg_kwh_per_100km`. Visible in the Tripbook table.
- **iCal feed** (`GET /api/tripbook.ics`): trips as `VEVENT` entries with
  km / duration / SoC / efficiency in the description. Subscribe in any
  calendar app — also linked from the Tripbook page and the footer.
- **UJH design system**: complete visual overhaul.
  - New `static/ujh.css` with design tokens (Inter Tight + JetBrains Mono,
    `#ff5b14` as the only signal color, 8-point spacing, sharp corners).
  - `base.html` redone — topbar with live indicator, wordmark with
    orange bar, persistent dark/light theme toggle (`data-theme=…`).
  - All page templates rewritten on the new tokens; Tailwind CDN removed.
  - Charts re-skinned: mono axis labels, ink/orange palette, no fills.
- This `CHANGELOG.md`.

### Changed
- Tripbook table now shows SoC delta and Wh/km per trip; monthly summary
  has kWh + kWh/100km columns.
- Dashboard navigation gains a `Planner` link.
- Footer surfaces CSV and iCal exports directly.

### Not implemented (deliberate)
- **`SetTargetSoc` write, climate, locks, horn, lights.** Confirmed
  unreachable from the consumer surface — `pypolestar` v1.12.2
  exposes only read RPCs (the `SetTargetSoc` proto exists but the
  Python client never calls it), and the consumer backend itself
  has no climate / lock / horn / light endpoints. The mobile app
  reaches those through separate Volvo PCC services that aren't on
  the public-facing GraphQL/gRPC plane.

### Notes
- Battery capacity for the kWh estimate is still hard-coded at 78 kWh in
  `sessions.DEFAULT_BATTERY_KWH`. If you have a Standard Range or Long
  Range Performance, expect the per-trip efficiency to be slightly off
  until this becomes configurable.
- `/raw` was deliberately not re-themed (debug surface, standalone HTML).

---

## [1.0] — 2026-05-19

First public version. Single commit (`1a02d78`) bundling prices, MQTT/HA,
Prometheus, Telegram, tripbook, daily SQLite backups, adaptive polling,
charging-session detection, CSV export, PWA manifest, ntfy push.

See `git log` for the path that led here.
