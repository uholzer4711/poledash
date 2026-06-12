# Changelog

All notable changes to **polestar-dash**. Format roughly follows
[Keep a Changelog](https://keepachangelog.com/); dates are UTC.

## [1.36.1] — 2026-06-12

### Fixed — vehicle-state goes blank when the C3 login degrades (re-pair needed)

- When the C3 backend loses its relation to the car (every section returns
  `PERMISSION_DENIED` / `UNAUTHENTICATED` — a throttle or a login that needs
  **re-pairing**), the scheduler keeps storing C3Snapshot rows but with *all*
  state fields `None`. The dashboard then served the newest (empty) row and the
  vehicle-state section rendered nothing — no labels, no diagram, no
  explanation — even though the last *known* state was minutes old.
- `routes/api.py` — the DB fallback now serves the **last row that still
  carried real state** (`is_locked` / `door_fl` / `lat` not `None`) instead of
  the newest empty one, and returns `connected` (is the newest poll itself
  stateful?) plus `last_poll_at`.
- `partials/c3_state.html` — when `connected` is false the section shows the
  last-known diagram/cards **and** a clear banner — *C3 not connected — showing
  last known state from HH:MM. The car account may need re-pairing.* — with the
  meta line switched to *Last known state: HH:MM · not connected*, so stale data
  is never passed off as current.

## [1.36.0] — 2026-06-12

### Fixed — vehicle-state lost its labels on a throttled C3 read

- The dashboard car diagram could lose every chip/label, the openings summary
  could falsely read *all closed*, and the lock card could show *Unlocked* when
  the state was simply unknown. Cause was in the data, not the template: a
  throttled/partial `GET /api/c3/latest?live=1` returned `ok` but with the whole
  exterior+location section empty (every door/lock/GPS field `None`), and that
  hollow live read was served over the full persisted row.
- `routes/api.py` — the live C3 read now treats a **stateless** response (all of
  `is_locked`/`door_*`/`sunroof`/`hood`/`tailgate`/`tank_lid`/`lat` `None`) as a
  miss and falls back to the persisted `C3Snapshot` row — the "degrade
  gracefully" invariant in the project conventions.
- `partials/c3_state.html` — when no part state is known at all, the openings
  summary now reads **no state data** instead of a false *all closed*; the lock
  card shows **—** for `is_locked = null` rather than *Unlocked*.

### Changed — Insights "Am I wasting energy?" shows only evidenced parked draws

- The section now renders **only what we can actually attribute**: C3-confirmed
  pre-conditioning / air-cleaning, a confirmed active draw while parked, or a
  parasitic-standby loss. Unattributable *unknown* SoC drops (mostly drives the
  frozen odometer hid) no longer appear in the headline, table or narrative —
  they live only in the collapsed **Diagnostics** view.
- Card renamed **Parked energy use** (*Verbrauch im Stand*). With nothing
  attributable it shows one honest line — *No measurable parked energy waste in
  the last N days…* — instead of a near-empty table or a misleading "kept"
  unknown row. When there are real events it shows per-cause breakdown badges
  (e.g. *Pre-conditioning · 2 · 1.3 kWh*) plus a table of just those events.
- `insights_extras.idle_loss_summary` now counts **only attributed** drains, so
  the Insights *Bottom line* no longer claims energy "lost to pre-conditioning"
  when nothing was actually attributed.

## [1.35.0] — 2026-06-11

### Added — idle-drain transparency: every SoC drop, its verdict, and what "cause" means

- The idle-drain table was a black box — and now that it's honestly near-empty,
  "what's going on?" needed a real answer. Two additions on **Insights →
  Am I wasting energy?**:
  - A **"What does the cause mean?"** legend defining each kept cause in plain
    language: *Pre-conditioning* / *Air cleaning* (C3 saw the cabin HVAC run),
    *Active draw (parked)* (kW-level battery/thermal management with climate
    idle), *Parasitic standby* (small steady 12 V background loss), and
    *Unknown* (plausible but no C3 GPS to attribute it).
  - A **Diagnostics** view listing *every* candidate SoC fall with its verdict —
    kept causes plus the exclusion reasons **Drive (not a drain)**,
    **Measurement settling**, **Implausible — over 8 kW** — the implied kW, and
    the **place** (resolved against your address geofences, e.g. *Arbeit*). A
    one-line tally shows *N checked · K kept · X excluded*, so you can see the
    detector is working and exactly why the table is short. (For this car: of 30
    SoC drops in a fortnight, all 30 are drives or noise — none a real parked
    drain.)
- `/api/idle-drain` now also returns `diagnostics` (every candidate with
  `verdict`/`place`/`kept`), a `tally`, and an `excluded` count. The detector
  was refactored to classify *all* candidates with a `verdict`
  (climate/air/conditioning/vampire/unknown, or drive/settling/implausible) and
  stamp each with its GPS location; `detect_idle_drains` returns the kept subset
  (unchanged for existing callers), `diagnose_idle_drains` returns everything.

## [1.34.1] — 2026-06-11

### Fixed — idle-drain still leaked a drive (stale park stamp) + clearer table

- A drive home was still showing as "Active draw (parked) · parked for 12 h
  18 min": the car had sat at one spot all day (so its C3 **park stamp was ~12 h
  stale**), the SoC fell 4 % in the 20 min before it drove off (odometer frozen
  at the origin, GPS frozen at the departure point), then the odometer jumped
  30 km. The stale stamp sailed through the "just parked" settling guard, and
  the 9.8 kW implied draw bypassed the power ceiling because a (stale) park stamp
  had been treated as proof of standstill.
- Two fixes: a drain whose **next snapshot advances the odometer** (`drove_after`)
  is now rejected — that trailing jump is often the only proof the SoC fall was
  the lead-in to a drive the frozen odometer hid; and the **drive-level power
  ceiling is applied unconditionally** (a long-looking `parked_for` can be a
  stale frozen stamp, so it no longer buys an exemption). C3-confirmed
  climate/air still counts as real conditioning. With these, the genuine result
  for this car is that almost every multi-percent SoC fall overlaps a drive — the
  table is honestly near-empty rather than padded with disguised trips.
- **Clearer table:** the energy cell reads `3.25 kWh · Ø 9.8 kW` (unit + an
  explicit *average*), the parked context reads *began after 12 h 18 min parked*
  (so it can't be mistaken for the loss duration), and a one-line legend spells
  out that **Duration** is how long the loss ran while the parked time is how
  long the car had already stood when it began.

## [1.34.0] — 2026-06-11

### Changed — idle-drain detection rebuilt (no more drives mislabelled as standby)

- The Insights idle-drain detector was reporting **drives as "Standby loss"** —
  e.g. a 70 → 63 % SoC fall over 30 min (≈ 11 kW) shown as a parked standby
  loss. Two telemetry quirks fooled the old odometer-equality gate: the
  pypolestar **odometer freezes** for hours (its value *and* `event_ts` stop
  advancing), so "odometer unchanged" never actually meant "stood still"; and
  the C3 **GPS freezes at the departure point** mid-drive. Both made a moving
  car look parked.
- Movement is now decided from **C3 GPS displacement** in a tight ±10 min
  window — a continuous drive's arrival fix lands there and gets rejected —
  backed by a **physics ceiling**: with no GPS to confirm standstill, an implied
  draw above drive level (8 kW) is a missed drive, not a drain.
- A **settling guard** drops any SoC fall that starts < 30 min after the car
  parked (`parked_for` from the C3 park stamp): that is post-drive open-circuit
  voltage re-estimation or a mid-drive GPS freeze, not real consumption — it was
  the dominant remaining false positive.
- What survives is a genuine parked draw, split by implied power into
  **active draw while parked** (`conditioning` — battery/thermal management or
  pre-departure pre-conditioning, which C3's cabin-only climate flag misses) vs
  **parasitic standby** (`vampire`); C3 cabin climate/air still win when active.
  The misleading "standby" catch-all is gone.
- `/api/idle-drain` now carries **`implied_kw`** and is ordered **newest-first**;
  the Insights table shows the implied kW beside each kWh so every figure is
  plausibility-checkable. Tests rewritten to lock in drive rejection (GPS +
  power backstop), the settling guard, and the conditioning/vampire/unknown
  split.

## [1.33.0] — 2026-06-11

### Changed — charging hero tells the truth about AC/DC, phases and energy

- **AC/DC + phase count.** The charging hero dropped the wrong **"DC"** badge:
  the Polestar gRPC `charging_type` enum mislabels ordinary 11 kW 3-phase AC as
  DC. The badge is now derived from C3 (authoritative) or physics
  (`util.charge_electrical`), and a **phase-count badge** (`P ≈ phases·V·I`,
  e.g. 3-phase) sits beside it. The voltage gauge gains a **`≈ 400 V
  line-to-line`** caption for 3-phase, since the API reports the ~230 V
  per-phase value. The corrected AC/DC is also written to the stored snapshot so
  History/labels stop reading "DC" for an AC charge (the raw payload keeps the
  original enum).
- **Live charged energy.** A new **"Charged"** stat shows the AC energy drawn so
  far (power integration since plug-in) plus a **self-calibrated projection to
  target** — the stored session energy is only computed at close
  (`sessions.live_charge_energy`).
- **Amperage headroom.** The Current stat shows an *of N A* hint when the
  connection offers more amps than the car's onboard charger pulls.

### Added — faster charge detection

- The C3 loop (≤ 5 min, independent of the idle-stretched main poll) now
  **nudges the main pypolestar poll forward** on a fresh charge-start
  transition, so the dashboard's charging hero appears within the C3 cadence
  instead of waiting up to a full (idle-stretched, ~20 min) interval. The two
  libraries stay spaced apart so they never hit the shared rate limit together.

## [1.32.1] — 2026-06-11

### Fixed — recurring Open-Meteo weather timeouts

- The current-weather lookup (`weather.current_at`) was called on **every
  dashboard tick (60 s)** and every C3 poll with a window open, with **no
  cache** and a single tight 10 s timeout — and Open-Meteo's free endpoint
  regularly answers in 5–9 s (and soft-throttles a chatty IP), so it sat right
  at the timeout edge and threw `ConnectTimeout`/`ReadTimeout` with a full
  traceback in the log. Now: a **10 min TTL cache** keyed by ~1 km coordinates
  (Open-Meteo's "current" only refreshes ~every 15 min anyway), so almost every
  call is instant and we stop hammering the API; a **split timeout** (5 s
  connect / 20 s read) with **one retry**; **serve-last-good-value on failure**
  (up to 1 h old) so a transient timeout no longer blanks the widget or skips
  the rain alert; and **quiet one-line logging** (no traceback) for expected
  network timeouts. The trip-enrichment backfill shares the same timeout +
  quiet logging.

## [1.32.0] — 2026-06-11

### Changed — charging card shows the car's own time estimate

- While charging, the dashboard's **time-to-target / time-to-full now prefers
  the car's own C3 estimate** (persisted since 1.31.0, fresh ≤ 10 min) over
  pypolestar's value and the static linear interpolation. The interpolation
  remains as fallback when C3 has no estimate.

### Added — idle drains get a cause

- The Insights idle-drain table has a new **"Cause" column**: each drain is
  attributed from the C3 rows overlapping its window — **Vorklimatisierung**
  (climate active, incl. spin-up states), **Luftreinigung** (cabin
  pre-cleaning), or **Standby-Verlust** (C3 saw the window, nothing ran).
  Drains predating C3 history stay "—". Where the parked fix allows it, the
  row also shows **how long the car had been parked** when the drain started
  (`parked_ts`) — conditioning clusters around departure, vampire drain shows
  up hours later. `/api/idle-drain` carries `cause` + `parked_for_min`.

### Fixed — dev hygiene

- `requires-python` is now **>= 3.12** (the C3 lib requires it; the Docker
  image always ran 3.12) and the repo `.venv` is a uv-managed 3.12 venv with
  the `dev` extra, so `pytest` runs on the host again (documented in
  the project conventions). Tests no longer use the deprecated `datetime.utcnow()`.

## [1.31.0] — 2026-06-11

### Changed — GPS fix timestamps (`gps_ts`) drive position honesty

The car stamps a GPS fix when it **parks** and freezes it during a drive
(`gps_ts == parked_ts` in practice) — a fix's age means "parked since", and
the position only lies mid-drive. Three consumers now use that:

- **Dashboard**: a fix older than ~15 min is labelled **"Position von HH:MM"**
  (location line + map-pin popup) instead of being presented as current; the
  weather card shows the car's own measurement time (`weather_ts`).
- **Drive detection** (`c3_poll_once` + `_c3_rest_state`): *SoC falling +
  unchanged `gps_ts` + not charging* now counts as driving — previously a
  locked car mid-drive looked "at rest" (frozen GPS defeats the movement
  heuristic) and could get night/idle-throttled on the road.
- **Enrichment guard** (`nearest_c3_env`): the GPS stamp for charge sessions /
  trips is picked by **fix time** (newest fix recorded no later than the
  window end), not by poll time. A row polled mid-drive can no longer stamp a
  session with the *previous* parking spot; windows whose only fixes were made
  after the event yield no GPS rather than a wrong one.

### Added — C3 data that used to be thrown away is now persisted

- **`C3SoftwareEvent`** — one row per car-reported OTA *(version, state)*
  transition (download/install/failed/installed …). Previously this lived only
  in the volatile C3 cache, so every restart erased the history. Shown on
  `/software` as "Update-Verlauf (vom Auto gemeldet)".
- **`LightWarningEvent`** — per-bulb fault/cleared transitions, diffed from
  `failed_lights` each C3 poll, so "which lamp failed first, and when" stays
  reconstructible for the workshop. A failed health fetch can't fake a
  "cleared" event.
- **`C3Snapshot.time_to_full` / `time_to_target` / `time_to_min_soc`** — the
  car's own charge-time horizons (minutes; 0 = "no estimate" stored as NULL)
  instead of existing only in the live cache.

### Removed — dead fields the API never populates for this car

- Dropped from model + extraction (all-NULL across the entire history):
  `total_energy_consumption_wh` (all 3 variants),
  `avg_consumption_kwh_per_100km_automatic`/`_since_charge`,
  `trip_manual_km`, `range_miles`, `image_url` (VehicleSnapshot) and
  `altitude` (C3Snapshot). Existing DB columns stay in place, just unmapped;
  the raw payloads and the `/api/lab/grpc-battery-raw` diagnostics keep
  exposing the underlying gRPC fields. This also closes the hope of "real
  per-trip kWh without CSV" — the journey-log import remains the only source
  of real trip energy.

## [1.30.0] — 2026-06-10

### Added — Journey-log import: format/unit auto-detection + result popup

- **The journey-log importer now auto-detects both the CSV delimiter and the
  consumption unit.** Besides the official Polestar app export (comma,
  `Consumption in Kwh` decimals), it now accepts CarStatsViewer-style exports
  (tab-delimited). Those sometimes mislabel the consumption column as kWh
  while it's actually **Wh** — and, in the wild, **mix units per row**:
  integer cells are Wh, decimal cells are already kWh. The importer detects
  the file-level unit from the implied kWh/km over trips with usable distance,
  then converts **only the integer-valued cells**, leaving already-correct
  decimals untouched. Each row is stamped with a new `consumption_scaled`
  column for provenance.
- **Re-importing a file now repairs bad data instead of skipping it**: a
  duplicate trip (same start/end timestamps) whose stored consumption differs
  from the freshly parsed value is corrected in place. `/api/journey-import`
  now returns `inserted` / `updated` / `skipped` counts.
- **Implausible or inconclusive files are rejected (HTTP 400)** instead of
  silently guessing: fewer than 5 km of trips with both distance and
  consumption, or any single trip exceeding 150 kWh after normalisation,
  aborts the import with an explanatory error.
- **New "Import complete" popup on `/settings`** after a journey-log upload,
  summarising what was parsed/inserted/corrected/skipped, whether a Wh→kWh
  unit fix was applied, and what got enriched as a result (battery
  calibration, CO₂ tagging, C3 on-car temperature + cabin PM2.5, weather) —
  extracted as a reusable partial (`partials/journey_import_modal.html`).

## [1.29.0] — 2026-06-09

### Changed — Insights: answer-first redesign

- **The Insights page now answers questions instead of dumping charts.** Five
  themed sections — *What is the car costing me? · How efficiently am I driving?
  · How healthy is the battery? · Am I wasting energy? · Impact & patterns* —
  each leading with a headline number, **auto-generated plain-language insight
  sentences**, and the charts demoted to collapsible `<details>` evidence. A
  **Bottom line** band surfaces the 2–3 most useful takeaways, and a **time-range
  selector** (30/90/180/365 d) drives the whole page.
- **New insight engine** (`insights_narrative.py`) builds localised sentences
  from the existing `compute_*` data plus new derived facts in
  `insights_extras.py`: cold-weather efficiency penalty, the most-efficient speed
  band (using the previously-unused per-trip speed data), range-now-vs-then,
  cold-charging throttle, EV-vs-petrol cost, month-over-month cost, AC/DC price
  split, and idle-loss totals. `/api/insights` returns the narrative.
- **Petrol comparison is configurable** — `PETROL_PRICE_EUR_PER_L` (1.75),
  `PETROL_L_PER_100KM` (7.0), `PETROL_CO2_G_PER_KM` (150), editable on /settings;
  feeds both the cost saving and the CO₂ comparison (the latter was a hardcoded
  150 g/km).
- **New chart**: Wh/km vs speed. Redundant battery charts (range factor /
  capacity / SoH) are consolidated under one "battery health" section.

### Changed — Dashboard SoH is compact

- The dashboard battery-health card is now a small **"SOH"** line; the measured
  kWh, sample count and explanation expand on click (same `<details>` pattern as
  the Vehicle card), with a link to the trend on Battery & Charging.

## [1.28.0] — 2026-06-09

### Added — SoH persisted + trend chart, log filters remembered

- **Battery State-of-Health is now persisted** (`SohSnapshot`, one row/day,
  written by a daily scheduler job + seeded ~30 s after start) and **charted on
  Battery & Charging** (`/api/soh-history`, line chart, theme-aware, with the
  same "estimate" caveat as the dashboard card).
- **The Log tab remembers your toolbar choices** (level, source, search, group,
  range, errors-only) across visits via `localStorage`, like the theme/view
  toggles.

### Fixed / polish (UI review follow-ups)

- **Dashboard SoH wording** no longer reads as a confusing ratio
  ("≈ 90.9 kWh measured · 78 kWh nominal · 7 samples").
- **Mobile browser-chrome colour follows the theme** (`<meta name=theme-color>`
  updates on toggle + load).
- **No emoji in the UI**: dropped the `⚠` glyph in the settings address-label
  warning (the orange `.accent` already signals it).
- **Chart colours read from tokens**: session-detail tooltip (`#000` →
  `--ujh-bg-3` + token text colours) and the Lab probe status colours
  (`--ujh-ok`/`--ujh-alert`) — no more hardcoded hex, light-theme-safe.
- **Two more tables fold into cards on mobile** (software version history,
  insights idle-drains) via `class="index"` + per-cell `data-label`.
- **Accessibility**: `aria-label` on the tripbook timeline scrubber.

## [1.27.1] — 2026-06-09

- **Log tab defaults to "By day"** grouping (hourly/flat still one click away).

## [1.27.0] — 2026-06-09

### Added — battery State-of-Health estimate on the dashboard

- **A "Battery health" card on the dashboard.** The Polestar/C3 API exposes no
  SoH field, so it's the TeslaMate-style estimate: each charge implies a usable
  pack capacity (`energy_kwh / ΔSoC`), each month of driving the same from
  consumption; we take the median of recent samples (SoC span ≥ 12 % to limit
  rounding noise) against the nominal 78 kWh pack, **capped at 100 %**. Computed
  at page load (not on the 30 s card refresh), failure-isolated. Clearly
  labelled an estimate — AC charging losses bias it high, so a healthy pack
  reads ~100 % until real degradation appears. Shows "collecting data" until a
  few qualifying charges/trips exist. (Note: this does **not** require 100 %
  charges.)

### Fixed / Cleanup

- **#16** — settings.html no longer hardcodes `#1a1c20` (7 Grafana cards); uses
  `var(--ujh-bg-3)`, which also fixes the wrong shade under the light theme.
- **#17** — `co2_g_per_kwh(when)` no longer defaults `when` to "now": the
  argument is required (every call site passes the event timestamp), so a row's
  CO2 stamp is deterministic regardless of when it's computed.

## [1.26.0] — 2026-06-09

### Added — software "since" date

- **The /software "reported by the car" card now shows a date** ("first seen
  …"). The Polestar/C3 API exposes no release or install date, so we stamp the
  first time each car-reported version is observed (write-once in an
  `AppSetting`, set from the C3 poll and on first page load) and display it.
  Captured going forward — existing versions get today's date once this build
  starts watching.

### Security

- **#11 GraphQL injection in the Lab probe is closed.** `POST /api/lab/probe`
  now validates each field against a strict allowlist (`name` or
  `name { sub1 sub2 }`) *before* touching the client, so a crafted field can no
  longer break out into arguments/directives/aliases/extra operations.
- **#12 Leaflet popup XSS surface hardened.** The tripbook map escapes all
  geocoder/user strings (addresses, comments) with an `escHtml()` helper before
  building popup HTML.

### Notes

- **SoH stays unavailable from the car** — confirmed the C3 library exposes no
  state-of-health/capacity field, so a headline value would have to be computed
  (and that estimate does *not* require 100 % charges). Not built.

## [1.25.0] — 2026-06-09

### Added — log search/filter/export, error alerts, richer /health

- **/log filtering**: full-text **search** over the message, a **Source**
  dropdown (distinct loggers via `GET /api/log/loggers`), an **Errors only**
  toggle (floors the level at WARNING), and a **Time range** selector (Live /
  24 h / 7 d / 30 d) that bounds history paging.
- **Plain-text export** of the persisted log honouring the active filters
  (`GET /api/log/export`, chronological, downloadable from the toolbar).
- **Opt-in error notifications** (`NOTIFY_LOG_ERRORS`, default off): an
  ERROR/CRITICAL log line fires a notification, deduped to one per source per
  hour via `NotificationLog`.
- **/health is now observable**: returns `version`, `last_poll_age_s`,
  `log_rows`, `log_pending` (flush backlog) and `db_bytes` — always 200 so the
  container healthcheck stays green.

### Changed / Fixed

- **No scroll-jump on the /log feed**: the stream stays pinned to the newest
  row at the top, or holds your place when you've scrolled into history and new
  lines arrive (fixes a regression from the grouped rewrite).
- **Gap-free history paging**: the page boundary is inclusive and the client
  de-duplicates by content key, so entries sharing a timestamp at the edge are
  never skipped. Singular/plural group counts ("1 entry" vs "N entries").
- **Leaner log indexes**: dropped the low-selectivity `levelno` index, added a
  `logger` index for the new Source filter (forward-only migration).

## [1.24.0] — 2026-06-09

### Added — durable log history

- **Activity log is now persisted to the DB**, not just the in-memory ring. A
  new `LogEntry` table is flushed in batches by a `log-persist` scheduler job
  (every `LOG_PERSIST_INTERVAL_MIN`, default 2 min) and on graceful shutdown,
  so log history survives restarts and reaches far beyond the ring's last-2000
  lines. The ring stays the live tail; a separate unbounded-until-flushed
  pending queue guarantees no line is lost to the ring's `maxlen`.
- **"Load older" paging on the /log tab** — a new `GET /api/log/history`
  endpoint serves older entries from the DB (by timestamp, level-filtered), and
  the page pulls one page of history on load plus on demand via the button.
  History merges seamlessly into the same hour/day grouping.
- **Retention**: `LOG_RETENTION_DAYS` (default 90) prunes old `LogEntry` rows in
  the existing daily retention job, keyed separately from the raw-snapshot
  window.

### Fixed

- Import ordering in `scheduler.py` (ruff `I`); the tree is `ruff check`-clean.

## [1.23.0] — 2026-06-09

### Changed — Log tab: consolidated, collapsible, with a column header

- **The Log tab now loads the full in-memory buffer** (up to 2000 lines, was a
  500-line fetch) instead of just the newest handful, so the whole activity
  history is visible.
- **Consolidation by hour / day** — a new *Group* selector buckets entries into
  collapsible `<details>` groups (default *By hour*; also *By day* or *Flat
  (live)*). Each group header shows the bucket time, entry count, and per-level
  WARN/ERR tallies (orange/red). The newest bucket auto-expands as it appears;
  expand/collapse state survives the live 4 s refresh. Regrouping happens
  client-side — no refetch.
- **Sticky column header** (Time / Level / Source / Message) above the stream,
  with sticky group headers underneath it; hidden on mobile where rows stack.

## [1.22.0] — 2026-06-09

### Changed — Raw / Lab / Test consolidated into one Lab tab

- **Raw, Lab and Test are now a single `/lab` tab** with collapsible,
  lazy-loading frames (live C3 status, raw API payload, write controls, field
  discovery) — one section per frame, nothing fetched until you expand it. The
  old `/raw` and `/test` URLs **307-redirect** to the matching frame
  (`/lab#frame-raw`, `/lab#frame-live`) so existing links keep working.
- **Nav slimmed accordingly** — top nav and the mobile bottom-nav "More" sheet
  drop the separate Raw/Test entries and keep only Lab (still demo-aware).
- German catalog covers the new consolidated-Lab strings; the standalone
  `raw.html` / `test.html` templates are removed.

### Fixed

- **Port-465 email (implicit TLS / SMTPS) now works.** `email_notify` treated
  465 like 587 — plain socket + `STARTTLS` — which made every 465 send fail.
  It now uses `SMTP_SSL` for 465 (TLS from the first byte, no `STARTTLS`),
  keeps `STARTTLS` for 587/other non-25 ports, and plaintext for 25.

## [1.21.0] — 2026-06-08

### Added — mobile-first overhaul + Mobil⇄Web switch

- **Bottom tab bar on mobile** (`partials/bottom_nav.html`) — fixed, safe-area
  aware, with a minimal set of geometric line icons (the one owner-approved
  exception to the no-icons rule, recorded in the project conventions) + text labels. 4
  primary tabs (Dashboard / Charge / Tripbook / Insights) + a pure-CSS "More"
  sheet for the rest (Routes / Software / Log / Settings, demo-aware Raw/Lab/Test).
  Replaces the cramped horizontal-scroll top nav on phones.
- **Mobil ⇄ Web view switch** in the topbar (mirrors the theme toggle:
  `localStorage['view-mode']`, pre-paint IIFE, no FOUC). Unset = auto by
  viewport width; forcing **Mobil** on a desktop renders a centred ~440px
  phone-width column with a subtle frame; forcing **Web** on a phone is the
  escape hatch to the full desktop layout.
- **Tables become cards on mobile** — one CSS pattern turns every `table.index`
  into stacked cards with per-cell labels (`data-label` + `::before`). Fixes the
  unreadable 8–10-column tables on Tripbook, Routes and Errors (no more
  horizontal scrolling).

### Changed

- **Mobile CSS is now class-driven** (`html.view-mobile`) instead of a bare
  `@media (max-width:640px)` query, so the switch can force either layout on any
  device. A slim media-query net keeps the essentials (gutter, single-column
  grids, 16px inputs) working with JS disabled.
- **Larger, more readable mobile type scale** (body 12→14px, stat 19→26px,
  h1 17→22px), relaxed settings config rows, 2-column quick-action grid for the
  control-panel buttons (≥44px tap targets), and capped map heights on phones.

## [1.20.0] — 2026-06-08

### Added

- **Log tab** (`/log`) — a live activity feed of everything the app's own
  loggers emit (polls, persistence, sessions, notifications, automations,
  errors). Backed by an in-memory ring buffer (`logbuffer.py`, last 2000 lines,
  no DB growth) attached to the `polestar_dash` logger; `GET /api/log` serves it
  with level filter + incremental `since_id`. Page auto-refreshes (4 s),
  level-filterable, pause/clear, WARNING orange / ERROR red.

### Fixed

- **Automations + one-tap confirm reported "not confirmed" on commands that
  actually worked.** Verification ran a single read immediately after the
  command, but the car needs ~10 s to physically actuate (e.g. close windows),
  so it read the old state. Both paths now use `verify_command_with_retries`
  (waits 6/7/9 s, returns success as soon as the post-state is reached) —
  mirroring the dashboard's client-side verify backoff.

## [1.19.0] — 2026-06-08

### Added — controls in the tabs, verified feedback, alert config, automations

- **Vehicle control on the dashboard, charging control on Battery & Charging.**
  The C3 write commands (lock/unlock, windows, climate, pre-cleaning, flash/honk,
  wake; and charge start/stop, amp limit, target SoC) now live as reusable panels
  next to the live car diagram and the charging charts — not just on `/test`,
  which stays as raw debug. Shared `static/control.js` (`window.poleControl`,
  same plain-IIFE pattern as `map.js`), included via `partials/control_vehicle.html`
  and `partials/control_charging.html`. Mobile-friendly (existing 640px grid).
- **Verified command feedback.** Every command shows `sending → verifying… →
  ✓ verified / ✗ not confirmed`. A new `polestar/control_verify.py` maps each
  command to its expected post-state and `GET /api/control/verify` forces one
  fresh C3 read to confirm it (e.g. windows actually became `closed`). Commands
  with no C3 read-back (`set_target_soc`, flash/honk/wake) honestly report
  "sent — can't verify" instead of a false ✗. Verify polls 3× (3/6/9 s backoff)
  with a server-side floor, to stay gentle on the shared C3 rate limit.
- **Generic command endpoint** `POST /api/control` (reuses the `run_command`
  allowlist); `/api/test/control` stays as the legacy alias.
- **Settings: alert configuration frame.** Each alert can be toggled and given a
  local-hour quiet-window (`GET`/`POST /api/app-settings/alert/{kind}`). The
  previously hardcoded windows (door/unlock 20→6, low-SoC 17→23) now live in
  `AppSetting` with those values as defaults; centralized in
  `app_settings.in_alert_window` and applied across `anomalies.py` /
  `c3_anomalies.py`. Settings page regrouped into labelled sections.
- **Automation engine** (`automations.py`) — generic rule registry run from
  `c3_poll_once` after the fresh fetch, each rule per-rule opt-in with a mode
  toggle: *autonom* (act, verify, then notify) or *vorschlag* (one-tap suggestion
  — nothing touches the car until you confirm). Two rules ship:
  **rain → close windows** and **windows still open in the evening → close**
  (the latter with a configurable "act from this local hour" dropdown, default
  21:00). Rules default to disabled + suggest (they move real hardware).
- **One-tap confirm flow** for suggestions, behind the existing reverse-proxy
  auth (no in-app auth added): a short-lived single-use `PendingAction` token,
  an ntfy `view` action button / Telegram URL button / email link, a `/confirm/{token}`
  page and `POST /api/confirm/{token}` that consumes the token, runs and verifies
  the command. `PUBLIC_BASE_URL` builds the links (degrades to a text hint if unset).
- **`/test`-only data moved into the normal UI**, leaving `/test` as the raw
  debug read-out: extended health (12V battery, washer fluid, per-bulb failure
  list) added to the dashboard health strip; Connector + Ø-consumption added to
  the charging hero; live availability + car trip-meters as a dashboard extras
  line; the **car-reported pending OTA** surfaced on `/software`. The
  `/api/c3/latest?live=1` response gained `trips` + `software` (live-only).

### Changed

- **Control panels restyled** — inputs and action buttons on separate rows
  (`.control-fields` / `.control-actions`), even-width ghost buttons, readable
  status line; no more labels jammed against buttons.
- **Alert quiet-hours** use **hour dropdowns** (00:00–23:00 + "any") instead of
  bare number fields; automations get a "Mode" + "Close after" dropdown.
- Health badges render an unknown reading (`—`) as neutral, not red.
- `windows_close` verification = "no window is open" (tolerant of a window that
  reports `—`) rather than requiring every field to read exactly `closed`.

## [1.18.1] — 2026-06-08

### Fixed — multi-agent review pass

- **Stale state after a control command** — `c3_control.run_command` now
  invalidates the read cache (`c3._cache = None`) *inside* the shared lock, so a
  concurrent `get_c3_snapshot` can't repopulate it the instant after; the
  post-command refresh was occasionally serving pre-command door/lock state.
- **Event-loop stall on the rain-&-windows alert** — `check_rain_windows` now
  offloads its terminal `notify()` via `asyncio.to_thread`. It was the one C3
  check awaited directly on the loop, so a fired alert blocked everything (main
  poll, dashboard, MQTT) for up to ~35 s on the blocking ntfy/SMTP/Telegram send.
- **`service_due` notification spam** — the dedup key was `…-d{days_to_service}`,
  which minted a fresh key every day and produced up to ~30 alerts for a single
  upcoming service. Now bucketed by week (`…-service-w{days // 7}`), capped to a
  handful and self-re-arming after the service is done.
- **i18n** — `"Current"` now translates to **Strom** (electrical current; was the
  wrong word "Aktuell"). The charging *control* group header gets its own key
  `"Charging actions"` → **Laden**, so it no longer collides with the `"Charging"`
  status verb (Lädt).
- **Latent XSS on the Test control page** — the command result's
  status/message/error (backend free-form text) is now HTML-escaped before it
  reaches `innerHTML` (low risk behind the mandated reverse proxy, but a real
  reflected-injection pattern).

## [1.18.0] — 2026-06-08

### Added — remote control on the Test page

- **The Test page can now send write commands to the car** via the same C3
  backend the Polestar app uses (`polestar/c3_control.py`, `POST
  /api/test/control`). Climate start/stop with target temperature + seat and
  steering-wheel heating, lock / unlock / trunk, flash / honk+flash, open /
  close windows, charge start/stop + amp limit + target SoC. An explicit
  command allowlist (never a raw `getattr`), reusing the read client's lock so
  a command can't race the poll snapshot, with one reset+retry. Confirm-dialogs
  guard the hardware-moving commands. Verified working on Polestar 2 (MY2026);
  the C3 backend has no `wakeup` (commands go through without it).

### Added — richer C3 data + car-reported software update

- **The Test page now surfaces data the C3 payloads already carried but we
  never decoded** (`polestar/c3.py` `_collect`): the car's own average
  consumption, live power/current/voltage while charging, three charge-time
  horizons, the car's trip meters (auto + manual), extended health (12V
  battery, washer fluid, engine-hours-to-service), and per-bulb light diagnosis
  (which lamp is out, not just *a* lamp).
- **OTA-update notification from the car itself** — `get_software_info` exposes
  a pending `new_sw_version` + changelog; we notify once per version when it is
  in a not-yet-installed state (`c3_software_pending`). More reliable than the
  manual-scrape path, which can't compare when pypolestar reports a null
  version. Shown on the Test page; the changelog's pseudo-HTML is cleaned.

### Added — 12V battery & fluid-level alerts

- **Health warnings now alert** (mail / ntfy / Telegram) on transition into a
  bad state, deduped per day: **12V battery**, washer / brake / oil / coolant
  levels (`c3_anomalies._check_health`, `_LEVEL_WARNINGS`). The bulb-failure
  alert now names the failed lamp. New `C3Snapshot` columns
  (`low_voltage_battery`, `washer_fluid`, `engine_hours_to_service`,
  `failed_lights`) via the forward-only `_ensure_columns` migration. Note: the
  C3 API exposes no numeric 12V voltage anywhere — only this warning flag.

### Changed — adaptive idle / night poll throttling

- **Polling now slows when the car is at rest**, to spare the shared account
  and the 12V battery (`scheduler._adaptive_interval`, `config.py`). A hard
  night window (default 23:00–05:00 local) drops both loops to 30 min; a
  locked-and-parked car during the day drops the main poll to 20 min and C3 to
  15 min. It **never** slows while charging or driving — "in use" is detected
  from the latest C3 snapshots (unlocked, or moved > 0.3 km, or speed ≥ 3, which
  also covers cars that auto-lock at speed). Dashboard/Test live reads still
  fetch fresh on demand. All thresholds are env-tunable; disable with
  `ADAPTIVE_IDLE_ENABLED=false`.

## [1.17.0] — 2026-06-08

### Changed — open/unlocked nags only overnight

- **"Door/window left open" and "left unlocked" notifications now fire only
  between 20:00 and 06:00 local time** (Europe/Vienna). During the day a
  briefly-open door (loading, parking lot) is normal and just noise; overnight
  the car is parked at home and the owner can actually act on it. The `notify()`
  daily dedup still applies, so an overnight-open window alerts once, not every
  poll. Tyre/light warnings and the rain-&-windows alert are unaffected — those
  stay active around the clock as genuine safety/weather warnings
  (`c3_anomalies.py` `_in_night_window`, gating `_check_openings` /
  `_check_unlocked`).

## [1.16.0] — 2026-06-03

### Changed — honest per-trip energy

- **Per-trip kWh/100km now prefers the SoC drop** on long-enough trips
  (≥ 10 km and ≥ 2 % drop) instead of always using the car's reported average
  consumption. The API's `avg_consumption_kwh_per_100km` is a slow-moving
  lifetime average — multiplying it by distance reprinted the *same* efficiency
  for every trip (e.g. a flat 14.8 across an entire driving day) and hid real
  variation. Above the length/drop threshold the whole-percent SoC step
  (~0.8 kWh at the calibrated capacity) is only a small fraction of the trip
  energy, so SoC × usable-capacity is the honest figure; short trips still fall
  back to the average, where SoC quantization would dominate. The day's total
  is unchanged; the breakdown finally reflects cold starts, climbs, etc.
  (`tripbook.py` `Trip.kwh_used`).
- Note: the exact per-trip Wh counter (`total_energy_consumption_wh_since_charge`)
  is carried in the schema but the Polestar gRPC backend returns it `NULL`, so
  it is not usable as a source yet; the SoC method is the best live signal short
  of an imported journey-log CSV.

## [1.15.0] — 2026-06-02

### Changed — UI cleanup: topbar, settings, dashboard, insights

- **Topbar**: removed the "Design" (v2) toggle and its now-unreachable
  `ujh-v2.css` link (the design is a single line again). The "Theme" button now
  shows the mode it switches **to** — `Light`/`Dark` (EN) / `Hell`/`Dunkel`
  (DE). Added a **DE/EN language switch** in the topbar (active language in
  orange); dropped the redundant Language card from `/settings`.
- **Settings**: the seven Grafana boards are folded into a collapsible
  `<details>` to shorten the page.
- **Dashboard streamlined**: the vehicle spec sheet is collapsed into a
  `<details>` (model + year stay visible). Service status is no longer doubled —
  the day/km countdown lives only in the Service card; the C3 health strip is
  now tyres/lights/fluids only.
- **Insights pruned**: dropped the noisy *Wh/km by weekday*, *by hour of day*
  and *Wh/km vs speed* charts plus the weekly cost table (the cost chart stays).
  Range-factor kept.

### Added

- **Insights · cabin air & on-car temperature (daily)**: a new chart plots the
  daily mean cabin PM2.5 and on-car outside temperature straight from
  `C3Snapshot` (`compute_c3_env_timeline`), so it carries data as soon as C3
  polling runs — independent of trip/session enrichment.
- **Hourly C3 enrichment catch-up** (`c3-enrich` scheduler job): re-flows on-car
  temperature + cabin PM2.5 into imported trips and charge sessions as the C3
  history grows, via `enrich_journey_imports_from_c3` and the new
  `backfill_session_c3_env`. Only fills empty fields; never clobbers.

## [1.14.0] — 2026-06-02

### Changed — Dashboard vehicle state goes live

- **Live vehicle state**: the dashboard's vehicle-state section now fetches
  **fresh** from the C3 backend via `GET /api/c3/latest?live=1` (30 s cache,
  shared with `/test`) so it stays consistent with the Test page; a
  throttled/failed live read degrades gracefully to the persisted `C3Snapshot`.
  The GPS trail (`/api/c3/track`) and all other readers stay DB-only.
- **Faster C3 cadence**: idle `C3_POLL_INTERVAL_MIN` 10 → **5** min and drive
  `C3_DRIVE_POLL_INTERVAL_MIN` 3 → **2** min. The car often reports `speed: 0`
  mid-drive, so drive detection leans on the movement heuristic — the idle base
  must stay short enough to sample a drive at all.
- **Car overlay recalibrated**: the open door/window orange overlay (plus the
  status chips and leader lines) now targets an uploaded **straight, symmetric
  top-view render** (479×759, `viewBox="0 0 479 759"` mapping 1:1 to image
  pixels) instead of the old angled stock photo. The built-in green/red SVG
  diagram remains the fallback when no image is uploaded.

### Removed — dashboard de-duplication

- Dropped the redundant Polestar studio image from the Vehicle card — the
  functional top-view is now the only car image on the page.
- Oil / Brake / Coolant warnings render **only** in the C3 *Health & warnings*
  block when C3 is enabled (it's a live superset that also covers tyres +
  lights); the Service card keeps just the service interval (days / km).
- Removed the duplicate "· Locked" eyebrow — the photo frame colour and the
  Locked stat card already convey lock state.

### Fixed

- **i18n**: wrapped two untranslated Insights strings (*No idle drains
  detected.*, *Insights failed to load*) and added German entries.
- **Refactor**: extracted the duplicated nearest-by-timestamp lookup in
  `stations.py` into a single module-level `_nearest_by_time` helper.

## [1.13.0] — 2026-06-01

### Added — C3 data feeds Tripbook / Routes / Charging, plus a rain alert

- **C3 enrichment** (`c3_enrich.py`): GPS, on-car temperature and cabin PM2.5
  from the C3 poll stream are now attached to other objects.
  - **Charge sessions** are stamped at close with `lat`/`lng`/`outside_temp_c`/
    `air_pm25` (`sessions._stamp_c3_env`) and backfilled in the History recalc;
    `stations.locate_session` uses the session's own GPS first.
  - **Detected trips** get a real GPS breadcrumb + average temp/PM2.5 via the
    new `GET /api/trips`; the tripbook map draws these trails — works with **no
    CSV upload**.
  - **Imported journeys** prefer the on-car C3 temperature over the Open-Meteo
    lookup and gain an `air_pm25` field
    (`enrich_journey_imports_from_c3`, run on import / `…/enrich`).
- **Insights**: *Cold charging* (peak kW vs on-car temperature) and *Cabin air
  quality* (monthly PM2.5) charts.
- **Routes**: average temperature and PM2.5 per repeated route.
- **Drive-aware C3 cadence**: polls every `C3_DRIVE_POLL_INTERVAL_MIN`
  (default 3 min) while the car is moving so the GPS trail is dense enough to
  draw a route; idle stays slow. Set high to disable.
- **Rain + windows-open alert** (`c3_anomalies.check_rain_windows`): one
  notification/day on the active channels when a window/sunroof is open and
  Open-Meteo reports rain at the car (`weather.current_at`). Small
  current-location weather on the dashboard (`GET /api/weather/current`).
- **Vehicle image**: upload a real Polestar 2 image on `/settings` (stored in
  the data dir, never committed); falls back to `CAR_IMAGE_URL`, then the
  built-in SVG top-view. `POST/GET/DELETE /api/car-image`.
- **Prometheus**: full C3 exposition — per-opening binaries
  (`poledash_c3_opening_open{part}`), lock, door/tyre/light warnings, climate,
  cabin PM2.5/AQI, on-car temp, speed/heading, lat/lng, and the C3 battery view.
- **Grafana**: new *C3 Command Center* dashboard (`grafana-dashboard-c3.json`,
  UID `poledash-c3`) — 27 panels: openings state-timeline, lock/security,
  climate, cabin air, on-car weather, live-location geomap, charging, SoC,
  range & service.

## [1.12.0] — 2026-06-01

### Added — C3 (Volvo Cloud Connectivity) goes live on the dashboard

- **C3 is polled and persisted.** A separate, staggered scheduler job
  (`c3_poll_once`, default 10 min / 5 min charging) writes `C3Snapshot` +
  `RawC3Snapshot`: live GPS, door/window/lock/alarm state, climate, cabin air,
  on-car weather, extended health. Isolated from the main pypolestar loop;
  disable with `C3_ENABLED=false`.
- **Dashboard vehicle-state section**: a Polestar 2 top-view diagram with
  green = closed / red = open per door, window, frunk, boot, sunroof and charge
  flap; lock state as the body frame. Status cards for lock, climate, cabin air
  and on-car weather, health badges, last-poll + interval, and a satellite
  mini-map with the live GPS pin + recent trail. `GET /api/c3/latest`,
  `/api/c3/track`.
- **Maps**: shared `static/map.js` with an Esri-satellite / Carto-dark layer
  toggle; tripbook and the C3 test page now use it.
- **C3 notifications**: door/window/frunk/boot left open, car left unlocked,
  tyre/light warnings (deduped via `NotificationLog`).
- **MQTT**: C3 entities for Home Assistant — `device_tracker` (GPS),
  binary_sensors (doors/windows/locked) and sensors (PM2.5/AQI/temp/climate).
- **Design**: functional state colours `--ujh-ok` (green) / `--ujh-alert` (red)
  — a documented, scoped exception to the orange-only signal rule, used only as
  thin contours on the car diagram and the matching badges.

## [1.11.0] — 2026-06-01

### Added — Geofence labels, journey-import analytics, and a Trip-flow map

- **Geofence labels.** Places can now be defined by a centre point + radius
  (AppSetting `address_geofences` = `{label: {lat, lng, radius_m}}`,
  `geo_label_for()`), not just address substrings. Far more robust where
  geocoding varies street by street — e.g. *Arbeit* = everything within 150 m
  of the office. The route resolver checks string patterns first, then
  geofences, then GPS clustering. New `/settings` card + `PUT
  /api/address-geofences`.
- **Broad-pattern guard.** `/settings` now warns when an address-label pattern
  matches more than 5 distinct imported addresses (a city/postcode token that
  would collapse "Arbeit → Arbeit"-style routes), so the long-standing
  substring footgun surfaces before it bites.
- **CO₂ footprint** on `/insights`: per-trip grid CO₂ (`consumption_kwh ×
  co2_g_per_kwh`) summed to total kg, g/km, and a savings figure vs a 150 g/km
  petrol car.
- **Real trip cost** on `/insights`: imported real kWh × blended price → € and
  €/100 km / ct/km for the window.
- **Usable-battery-capacity trend** on `/insights`: monthly implied capacity
  (energy ÷ SoC-drop) to surface degradation over time (needs several months of
  data before it's meaningful).
- **Route efficiency leaderboard** on `/routes`: most- and least-efficient
  repeated routes.
- **Trip-flow map** on `/settings` (under the address map): every imported
  drive drawn as an animated, fanned-out arc, coloured by a switchable lens
  (kWh/100 km · CO₂ · °C · €), thickened by frequency, with geofence rings and a
  time-scrubber that replays trips with a moving car marker. Theme-aware tiles;
  no new JS bundle (Leaflet + CSS-keyframe flow). New `GET /api/trip-paths`.

### Fixed — Duplicate imported trips and a VIN-sensitive dedup

- Importing the same journey CSV once *with* a VIN and once *without* used to
  slip past the `(started_at, ended_at, vin)` dedup (SQL never matches `'VIN' =
  NULL`) and accumulated duplicate rows. `upsert()` is now **VIN-agnostic**
  (a trip is keyed on its exact start+end timestamps; `vin` stays as metadata),
  and a forward-only startup migration collapses any existing duplicates.

## [1.10.8] — 2026-06-01

### Changed — "History" page renamed to "Battery & Charging"

- The `/history` page (battery/SoC/range/charging-power time-series plus the
  charging-sessions list) is renamed from **History / Verlauf** to
  **Battery & Charging / Akku & Laden** — "Verlauf" read as generic browser
  history and didn't convey the battery/charging focus. Nav label, page title,
  the `02 / …` section tag and the settings "no stations yet" hint all follow;
  the German catalog gains `"Battery & Charging"` → `"Akku & Laden"`. The
  `/history` route itself is unchanged, so links and bookmarks keep working.

## [1.10.7] — 2026-06-01

### Changed — Tripbook trips grouped into collapsible months

- The detected-trips list is now grouped by month into native
  `<details>/<summary>` accordions (newest month first, newest trip first
  within each); the most recent month is expanded, older ones collapsed, so a
  long window no longer renders one endless table. Each month header shows its
  trip count, km, kWh and cost even while collapsed. No JS, no build step —
  UJH-styled (border-radius 0, mono labels, typographic `→` disclosure mark).

## [1.10.6] — 2026-06-01

### Changed — Session energy from integrated charging power

- A session's `energy_kwh` is now measured by trapezoidal integration of
  `charging_power_w` over the session window (with a head/tail correction for
  the poll-cadence blind spot at plug-in/unplug), falling back to the old
  `ΔSoC × capacity × loss` estimate only when a session has no power samples.
  Power integration measures the AC-side energy the operator bills and avoids
  the integer-percent SoC quantization that made short charges read far too
  low — two billed top-ups went from −36 %/−8 % off to −5 %/+11 %. Shared by
  the live close path and the History recalc. Trade-off: very long charges read
  marginally lower than the SoC method; short charges are dramatically better.
- All existing complete sessions were recalculated in place (DB backed up to
  `data/polestar.pre-power-integration.*.bak` first).

## [1.10.5] — 2026-05-30

### Docs — README

- Document the session **energy & cost model** (ΔSoC × calibrated usable
  capacity × loss factor, no double-counting) and that picking a place from the
  Location dropdown fills the price field. No code change.

## [1.10.4] — 2026-05-30

### Fixed — Dropdown options unreadable in the dark theme

- The session Location `<select>` was `background: transparent`, so the native
  option popup fell back to white and the light `--ujh-ink` text vanished
  against it. The control and its `option`s now get an explicit themed surface
  (`--ujh-bg-2`), readable in both themes.

### Fixed — Session energy/cost inconsistent after battery calibration

- `_recalc_session_energy` hardcoded `DEFAULT_BATTERY_KWH` (78 kWh) while the
  close-time formula uses the **calibrated** usable capacity. After a CSV import
  re-calibrated the pack (to 82.9 kWh here), only newly-closed sessions used the
  new figure — older ones stayed at 78, so their kWh and cost were ~6 % low and
  History mixed two capacities. Recalc now uses the calibrated capacity, and a
  CSV-import re-calibration re-runs it across all complete sessions. The formula
  itself is correct and does **not** double-count losses: calibration derives
  *net* usable capacity from driving consumption, and the loss factor (×1.12) is
  applied only on top for grid draw.

## [1.10.3] — 2026-05-30

### Changed — Picking a place fills the price field

- Selecting a known place from a session's Location dropdown now puts that
  place's saved price straight into the **Price (ct/kWh)** field, instead of
  only filling it when the field was empty. A place with no saved price leaves
  the field untouched; `0 ct` is treated as a real price.

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
