# Datenquellen & Steuerung — die Grundlagen

> Wie psapi an die Fahrzeugdaten kommt und wie Befehle ans Auto gehen.
> Stand: v1.52.0 (2026-08). Es gibt **keine offizielle Polestar-API und kein SDK** —
> alles Folgende ist reverse-engineered über zwei unabhängige Community-Bibliotheken.

---

## Das Grundprinzip: zwei getrennte Backends, ein Account

Polestar/Volvo betreiben **zwei verschiedene Cloud-Backends** für dasselbe Auto.
psapi spricht beide an — mit je einer eigenen Bibliothek, eigenem Login, eigenem
Datenmodell und eigenem Rate-Limit. Sie teilen sich aber denselben Account
(`POLESTAR_EMAIL` / `POLESTAR_PASSWORD`).

| | **Consumer-API** (pypolestar) | **C3 / Volvo Cloud Connectivity** (unofficial-polestar-api) |
|---|---|---|
| Bibliothek | `pypolestar>=1.12` | `unofficial-polestar-api>=0.3` (import als `polestar_api`) |
| Transport | GraphQL + gRPC | gRPC |
| Login | OAuth2/PKCE, Token-Refresh in-process | OAuth2, eigener Token |
| Daten | Akku-SoC, Reichweite, Verbrauch, Odometer, Service-Health | GPS, Türen/Schlösser, Klima, Kabinen-Luftqualität, On-Car-Wetter, Licht/Reifen-Warnungen, Software-Stand |
| **Schreiben (Befehle)** | ❌ nein, nur lesen | ✅ **ja** — Lock, Klima, Fenster, Laden, Hupe/Licht |
| Im Code | `polestar/client.py`, `polestar/fetcher.py` | `polestar/c3.py` (lesen), `polestar/c3_control.py` (schreiben) |
| Poll-Loop | Produktiv (`scheduler.poll_once`), 10 min / 2 min beim Laden | Eigener Loop (`scheduler.c3_poll_once`), 5 min / 2 min beim Fahren |

**Wichtig:** Beide Bibliotheken treffen denselben Account und teilen sich ein
Rate-Limit. Deshalb sind die zwei Poll-Loops **gegeneinander versetzt** (staggered)
und dürfen nie gleichzeitig feuern. Drosselung zeigt sich als `"not authorized"`
(kein echtes Auth-Problem!), Cooldown ~150 s.

---

## 1. Daten lesen

### 1a. Consumer-API via `pypolestar` — die Produktiv-Quelle

`pypolestar` erledigt OAuth/PKCE-Login und Token-Refresh selbst. Wir halten nur
die Instanz (`PolestarClient` in `polestar/client.py`) und pollen pro VIN.

- **Abruf:** `api.update_latest_data(vin, update_vehicle=True, update_telematics=True, update_grpc=True)`
  - `update_vehicle` → `CarInformationData` (Akku-Spezifikation, Torque)
  - `update_telematics` → GraphQL `CarTelematicsV2` (Health, Battery, Odometer)
  - `update_grpc` → gRPC-Telemetrie (Verbrauchsvarianten, Ladeleistung, Meilen)
- **Was rauskommt:** SoC %, Reichweite km/mi, Ø-Verbrauch (kWh/100 km, mehrere
  Varianten), Odometer, Lade-Status/-Typ/-Leistung, Ziel-SoC, Service-Warnungen,
  Tage/Distanz bis Service. Felder, die das Auto nicht meldet, sind `None`.
- **Persistenz:** Jeder erfolgreiche Fetch schreibt **zwei** Zeilen — `VehicleSnapshot`
  (getypt) **und** `RawSnapshot` (volles JSON). Das Roh-JSON wird nie verworfen.
  Fehlschläge landen in `FetchError` (sichtbar unter `/errors`).

**Eigenheit / Footgun — Schema-Patch:** Polestar hat am 2026-05-21 das Feld
`chargingStatus` aus der BatteryV2-Query entfernt, was jeden Poll brach. `client.py`
patcht deshalb beim Import die GraphQL-Query (`_patch_pypolestar_schema()`): wir
werfen `chargingStatus` raus, machen das Model tolerant gegen das fehlende Feld,
und **leiten den Lade-Status selbst aus Ladeleistung/Stecker-Status ab.** Zusätzlich
zieht der Patch Akku-Felder aus dem Proto, die pypolestar fallen lässt
(Energieverbrauchs-Varianten, Meilen, Charger-Power-Status).

### 1b. C3 / Volvo Cloud Connectivity via `unofficial-polestar-api`

Liefert genau das, was die Consumer-API **nicht** hat. Read-Pfad in `polestar/c3.py`,
nur `get_*`-Accessoren:

- `get_location` / `get_parked_location` → **Live-GPS** (Lat/Lng, Heading, Speed)
- `get_exterior` → Schlösser, Türen, Fenster, Schiebedach, Haube, Heckklappe
- `get_climate` → Klima aktiv?, Heizen/Kühlen, Restzeit
- Kabinen-Luftqualität (AQI, PM2.5), **On-Car-Wetter** (Außentemp am Auto)
- Erweiterte Health: pro-Lampe-Lichtwarnungen, Reifenwarnungen, Service, Brems-/
  Kühlflüssigkeit, 12V-Batterie, Software-Stand + Changelog
- **Persistenz:** eigener Loop schreibt `C3Snapshot` (getypt) + `RawC3Snapshot` (JSON).

**Live vs. persistiert:** Das Dashboard ruft `/api/c3/latest?live=1` → holt **frisch**
vom C3-Backend (30 s Cache, geteilt mit der Test-Seite). Schlägt der Live-Read fehl
(Drosselung), degradiert es sauber auf den letzten `C3Snapshot` aus der DB.
GPS-Trail (`/api/c3/track`) und alles andere lesen nur aus der DB.

**Fahrt-Eigenheit:** Während der Fahrt liefert C3 den SoC live, friert aber GPS +
Odometer bis zur Ankunft ein → der Trail hat dann nur 2 Punkte. Die Fahrt-Erkennung
lehnt sich daher an die Bewegungs-Heuristik (>0,3 km seit letztem Fix), nicht an
`speed` (das ist oft 0 mitten in der Fahrt).

### 1c. Zusatzquellen (nicht vom Auto)

- **Open-Meteo** (`weather.py`, `https://api.open-meteo.com`) — Wetter/Regen an der
  GPS-Position. Speist u. a. den „Regen + Fenster offen"-Alarm und Trip-Anreicherung.
- **Journey-Log-CSV** (`journey_import.py`) — manueller Upload aus der Polestar-App.
  Das ist die **einzige** Quelle für **echte kWh pro Fahrt** + offiziell geokodierte
  Adressen + History-Backfill. Wird bewusst neben C3 behalten (C3 = Live/GPS, CSV =
  echte Energie & Adressen).
- **Statischer Strompreis** (`default_price_ct_per_kwh`, Default 50 ct/kWh) — eine
  Einstellung, **kein** Live-Spotpreis-Feed. (Die früheren `prices.py`/`planner.py`
  mit aWATTar/EPEX existieren im aktuellen Code nicht mehr.)

---

## 2. Befehle ans Auto schicken (Steuerung)

**Nur** über C3 möglich (die Consumer-API ist read-only). Implementiert in
`polestar/c3_control.py` — bewusst noch vorsichtiger als der Read-Pfad, weil jeder
Call eine echte Wirkung am Auto hat. Verifiziert auf Polestar 2 (MY2026).

### Befehls-Allowlist (`_COMMANDS`)

Eine **explizite Allowlist** — nie ein beliebiges `getattr()` auf das Fahrzeug.

| Gruppe | Befehl-ID | Wirkung | Bestätigung nötig |
|---|---|---|---|
| **Security** | `lock` / `unlock` | Verriegeln / Entriegeln | ✅ |
| | `unlock_trunk` | Kofferraum entriegeln | ✅ |
| | `flash` | Lichthupe | — |
| | `honk_flash` | Hupe + Lichthupe | ✅ |
| **Klima** | `climate_start` | Vorklimatisieren (Temp + Sitz-/Lenkrad-Heizung 0–4) | — |
| | `climate_stop` | Klima stoppen | — |
| | `precleaning_start` / `_stop` | Kabinen-Luftreinigung | — |
| **Fenster** | `windows_open` / `windows_close` | Fenster auf/zu | ✅ |
| **Laden** | `charge_start` / `charge_stop` | Laden starten/stoppen | ✅ |
| | `set_amp_limit` | Ladestrom-Limit (A) | ✅ |
| | `set_target_soc` | Ziel-SoC % (DAILY) | ✅ |
| **Sonstiges** | `wakeup` | Aufwecken (auf diesem Backend **nicht** implementiert; das Auto nimmt Befehle auch ohne an) | — |

### Ausführung & Sicherheit

- **Endpunkte:** `POST /api/control` (kanonisch) und `POST /test/control`. Beide
  rufen `run_command(command, params)`. Unbekannte Befehle werden in der Allowlist
  abgewiesen.
- **`run_command` wirft nie** — Fehler landen im Rückgabe-Dict. Ein sauberer
  Reset+Retry fängt abgelaufene Tokens ab.
- **Kein Race mit dem Read:** Befehl und Snapshot-Read teilen sich `c3._lock` und
  dieselbe Client-Instanz — sie treffen das C3-Backend nie gleichzeitig. Nach jedem
  Befehl wird der Read-Cache **innerhalb** des Locks invalidiert, damit der nächste
  Refresh nicht den Vor-Befehl-Zustand serviert.
- **Verifikation:** `control_verify.py` (`verify_command` / `verify_command_with_retries`)
  erzwingt einen frischen C3-Read und prüft, ob der Befehl tatsächlich gewirkt hat —
  statt blind auf die `InvocationStatus.SUCCESS`-Antwort zu vertrauen.
- **Deferred / „Suggest"-Modus** (`pending_actions.py`): Die Automatik-Engine kann
  einen Befehl unter einem unrätselbaren Einmal-Token parken und per Push-Nachricht
  einen 1-Tipp-Bestätigungs-Button schicken. `POST /api/control/confirm` konsumiert
  das Token **atomar** (Doppel-Tipp → 409, feuert nicht erneut). Dafür ist
  `PUBLIC_BASE_URL` nötig.

---

## 3. Wo die Daten rausgehen (Ausgabe-Transporte)

Kein Datenabruf, aber Teil der „Grundlage": Was psapi mit den Daten macht.

- **Dashboard** — HTMX + Chart.js, Live-Refresh alle 30 s.
- **Prometheus** — `/metrics` (`metrics.py`).
- **MQTT** — Home-Assistant-Discovery + State (`mqtt.py`, `aiomqtt`).
- **Notifications** — ntfy.sh + Telegram + SMTP, dedupliziert über `NotificationLog`.
- **iCal** — Tripbook-Feed `/api/tripbook.ics`.
- **CSV** — `/api/export.csv`.

---

## Merksätze

1. **Lesen = beide Backends, Schreiben = nur C3.** Die Consumer-API kann keine
   Befehle.
2. **Ein Account, zwei Rate-Limits** → Polls staggern, sonst `"not authorized"`.
3. **Jeder Poll schreibt Roh + getypt** — das Roh-JSON nie wegwerfen.
4. **Steuerung ist eine Allowlist** — nie dynamisch auf Fahrzeug-Methoden dispatchen.
5. **Befehle gegenlesen** (`control_verify`), nicht der Backend-Antwort vertrauen.
