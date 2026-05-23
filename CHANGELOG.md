# Changelog

All notable changes to **polestar-dash**. Format roughly follows
[Keep a Changelog](https://keepachangelog.com/); dates are UTC.

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
