"""Track the car's installed software version and watch the Polestar online
manual for newly released versions.

Two concerns:
  * record_installed_version() runs in the poll pipeline and logs every change
    of the car-reported software version into SoftwareVersionChange, so the
    dashboard can answer "which version, since when" and notify on each OTA.
  * check_software_updates() runs twice a day: it scrapes the public manual
    page for the list of released versions and emails when a version newer
    than the car's installed one becomes available.

The manual is plain server-rendered HTML — each version is a section headed
"Updates in software version P5.1.9". We parse those headers + the text that
follows. Parsing is best-effort: on any failure we keep the last known
catalog rather than crashing the scheduler.
"""

from __future__ import annotations

import contextlib
import html as _html
import logging
import re
from datetime import datetime

import httpx
from sqlmodel import Session, select

from .app_settings import get_setting, set_setting
from .config import get_settings
from .db.models import (
    C3SoftwareEvent,
    SoftwareRelease,
    SoftwareVersionChange,
    VehicleSnapshot,
)
from .db.session import get_engine
from .notify import notify
from .polestar.fetcher import Snapshot
from .util import utcnow

log = logging.getLogger(__name__)

LAST_CHECK_KEY = "software_last_checked"

_VERSION_RE = re.compile(r"P\d+(?:\.\d+)+")
# Only the *rendered* HTML titles (they end in </h2>). The page also embeds the
# whole changelog a second time as a JSON blob far down the document; matching
# the closing tag keeps us out of that and out of inline-script copies.
_HEADER_RE = re.compile(
    r"<h2\b[^>]*>Updates in software version\s*(P\d+(?:\.\d+)+)</h2>"
)
_TAG_RE = re.compile(r"<[^>]+>")
_STYLE_RE = re.compile(r"<(style|script)\b[^>]*>.*?</\1>", re.DOTALL)
_LI_RE = re.compile(r"<li\b[^>]*>")
_BLOCK_END_RE = re.compile(r"</(p|h[1-6]|li|ul|ol|div|section)>")
_BLOCK_START_RE = re.compile(r"<(p|h[1-6])\b[^>]*>")


def parse_version(label: str | None) -> tuple[int, ...] | None:
    """"P5.1.9" -> (5, 1, 9). None if no version token is present."""
    if not label:
        return None
    m = _VERSION_RE.search(label)
    if not m:
        return None
    try:
        return tuple(int(n) for n in m.group(0)[1:].split("."))
    except ValueError:
        return None


def version_key(label: str) -> str:
    """Zero-padded sortable form so string ordering == semantic order."""
    parts = parse_version(label) or ()
    return ".".join(f"{n:05d}" for n in parts)


def _notes_from_html(fragment: str) -> str:
    """Turn one version's HTML block into clean, line-broken text.

    Paragraphs and sub-headings become their own lines; list items become
    "• …" bullets. Keeps the changelog readable instead of one wall of text.
    """
    fragment = _STYLE_RE.sub(" ", fragment)
    fragment = _LI_RE.sub("\n• ", fragment)
    fragment = _BLOCK_END_RE.sub("\n", fragment)
    fragment = _BLOCK_START_RE.sub("\n", fragment)
    text = _html.unescape(_TAG_RE.sub(" ", fragment))
    lines = []
    for raw in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if line and line != "•":
            lines.append(line)
    return "\n".join(lines)


def parse_catalog(html_text: str) -> list[tuple[str, str]]:
    """Return [(version_label, notes_text), ...] sorted newest-first."""
    headers = list(_HEADER_RE.finditer(html_text))
    out: list[tuple[str, str]] = []
    for i, m in enumerate(headers):
        version = m.group(1)
        start = m.end()
        if i + 1 < len(headers):
            end = headers[i + 1].start()
        else:
            # Oldest entry: stop at the end of the changelog article so we
            # don't sweep in the page footer.
            art = html_text.find("</article>", start)
            end = art if art != -1 else len(html_text)
        notes = _notes_from_html(html_text[start:end])[:4000]
        out.append((version, notes))
    return sorted(out, key=lambda kv: version_key(kv[0]), reverse=True)


def fetch_release_catalog() -> list[SoftwareRelease]:
    """Scrape the manual, upsert SoftwareRelease rows, return them newest-first."""
    s = get_settings()
    url = s.software_manual_url
    with httpx.Client(
        timeout=20,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (poledash software-watch)"},
    ) as client:
        r = client.get(url)
        r.raise_for_status()
        html_text = r.text

    catalog = parse_catalog(html_text)
    if not catalog:
        log.warning("software: no versions parsed from %s", url)
        return current_releases()

    now = utcnow()
    with Session(get_engine()) as session:
        for version, notes in catalog:
            row = session.get(SoftwareRelease, version)
            if row is None:
                session.add(
                    SoftwareRelease(
                        version=version,
                        version_key=version_key(version),
                        notes=notes or None,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
            else:
                if notes:
                    row.notes = notes
                row.last_seen_at = now
                session.add(row)
        session.commit()
    set_setting(LAST_CHECK_KEY, now.isoformat())
    return current_releases()


def current_releases() -> list[SoftwareRelease]:
    with Session(get_engine()) as s:
        return list(
            s.exec(
                select(SoftwareRelease).order_by(SoftwareRelease.version_key.desc())
            ).all()
        )


def _current_installed(
    vin: str | None = None,
) -> tuple[str | None, datetime | None, str | None]:
    """(version, car-reported timestamp, vin) of the latest installed software.

    Prefers a version reported by the car's own API. Many Polestar 2 vehicles
    return ``software_version: null`` over the consumer API, so we fall back to
    the most recent recorded change — which also covers a value the user
    entered by hand (set_manual_installed_version writes a change row too).
    """
    with Session(get_engine()) as s:
        stmt = (
            select(VehicleSnapshot)
            .where(VehicleSnapshot.software_version.is_not(None))
            .order_by(VehicleSnapshot.fetched_at.desc())
        )
        if vin:
            stmt = stmt.where(VehicleSnapshot.vin == vin)
        row = s.exec(stmt.limit(1)).first()
        if row is not None:
            return row.software_version, row.software_version_ts, row.vin

        chg_stmt = select(SoftwareVersionChange).order_by(
            SoftwareVersionChange.detected_at.desc()
        )
        if vin:
            chg_stmt = chg_stmt.where(SoftwareVersionChange.vin == vin)
        chg = s.exec(chg_stmt.limit(1)).first()
    if chg is None:
        return None, None, None
    return chg.software_version, chg.software_version_ts, chg.vin


C3_SW_SEEN_PREFIX = "c3sw_first_seen:"


def c3_software_seen(version: str | None) -> datetime | None:
    """First time we observed a given C3-reported software version.

    The Polestar/C3 API gives no release or install date, so this stamps
    ``utcnow()`` the first time a version string is seen and returns the same
    value forever after (write-once, idempotent). Lets the /software card show
    "since <date>" for whatever software the car is currently announcing —
    captured from the moment this build starts watching, going forward.
    """
    v = (version or "").strip()
    if not v:
        return None
    key = C3_SW_SEEN_PREFIX + v
    raw = get_setting(key)
    if raw:
        with contextlib.suppress(ValueError):
            return datetime.fromisoformat(raw)
        return None
    now = utcnow()
    set_setting(key, now.isoformat())
    return now


def record_c3_software_event(vin: str, sw: dict) -> None:
    """Persist the car-reported OTA state (version + install state) on change.

    ``sw`` is the decoded C3 software section ({new_version, state_code, state,
    name, notes}). Writes one C3SoftwareEvent per (version, state) transition —
    idempotent across polls, so the steady state costs one SELECT per poll.
    """
    version = (sw.get("new_version") or "").strip()
    if not version:
        return
    state_code = int(sw.get("state_code") or 0)
    with Session(get_engine()) as s:
        last = s.exec(
            select(C3SoftwareEvent)
            .where(C3SoftwareEvent.vin == vin)
            .order_by(C3SoftwareEvent.seen_at.desc())
            .limit(1)
        ).first()
        if (
            last is not None
            and last.version == version
            and last.state_code == state_code
        ):
            return
        s.add(
            C3SoftwareEvent(
                vin=vin,
                seen_at=utcnow(),
                version=version,
                state_code=state_code,
                state=sw.get("state") or "?",
                name=sw.get("name"),
                notes=(sw.get("notes") or None) and str(sw["notes"])[:4000],
            )
        )
        s.commit()
    log.info("software: C3 OTA state %s → %s", version, sw.get("state"))


def _latest_snapshot_vin() -> str | None:
    with Session(get_engine()) as s:
        return s.exec(
            select(VehicleSnapshot.vin).order_by(VehicleSnapshot.fetched_at.desc()).limit(1)
        ).first()


def set_manual_installed_version(version: str) -> None:
    """Record the installed software version the user read from the car.

    Validates the ``P5.1.9`` shape, then writes a SoftwareVersionChange so it
    shows up in history and as the current installed version (the dashboard and
    the update check both read through _current_installed). No-op if it already
    matches the current value. If the car's API later starts reporting a
    version, record_installed_version takes over automatically.
    """
    v = version.strip()
    if parse_version(v) is None:
        raise ValueError("Expected a version like P5.1.9")
    installed, _ts, vin = _current_installed()
    if installed == v:
        return
    with Session(get_engine()) as s:
        s.add(
            SoftwareVersionChange(
                vin=vin or _latest_snapshot_vin() or "manual",
                detected_at=utcnow(),
                software_version=v,
                previous_version=installed,
            )
        )
        s.commit()


def record_installed_version(snap: Snapshot) -> None:
    """Record a change of the car-reported software version and notify on it.

    No-op when the version is unchanged. The first version ever observed for a
    VIN is recorded silently (no notification) so we don't alert for whatever
    the car already ran when tracking began.
    """
    version = snap.software_version
    if not version:
        return
    with Session(get_engine()) as s:
        last = s.exec(
            select(SoftwareVersionChange)
            .where(SoftwareVersionChange.vin == snap.vin)
            .order_by(SoftwareVersionChange.detected_at.desc())
            .limit(1)
        ).first()
        if last is not None and last.software_version == version:
            return
        prev = last.software_version if last is not None else None
        first_ever = last is None
        s.add(
            SoftwareVersionChange(
                vin=snap.vin,
                detected_at=snap.fetched_at,
                software_version=version,
                previous_version=prev,
                software_version_ts=snap.software_version_ts,
            )
        )
        s.commit()

    if first_ever:
        log.info("software: baseline version %s for %s", version, snap.vin)
        return

    notify(
        kind="software_installed",
        key=f"{snap.vin}-{version}",
        title=f"Software updated to {version}",
        body=f"Your Polestar installed software {version} (previously {prev}).",
        vin=snap.vin,
        tags=["gear"],
    )


def check_software_updates() -> None:
    """Twice-daily job: refresh the catalog and email when a version newer than
    the car's installed one is listed in the manual."""
    try:
        releases = fetch_release_catalog()
    except Exception:  # noqa: BLE001
        log.exception("software: catalog fetch failed")
        return
    if not releases:
        return

    latest = releases[0]
    latest_t = parse_version(latest.version)
    installed, _ts, vin = _current_installed()
    installed_t = parse_version(installed)

    if installed_t is None or latest_t is None:
        log.info(
            "software: cannot compare (installed=%s latest=%s)", installed, latest.version
        )
        return
    if latest_t <= installed_t:
        log.info("software: up to date (installed %s, latest %s)", installed, latest.version)
        return

    notify(
        kind="software_update_available",
        key=f"{vin}-{latest.version}",
        title=f"Software update available: {latest.version}",
        body=(
            f"Your Polestar runs {installed}. Version {latest.version} is now "
            f"listed in the Polestar manual.\n\n{(latest.notes or '')[:600]}"
        ),
        email_html=_update_email_html(installed, latest),
        vin=vin,
        priority="high",
        tags=["gear"],
    )
    log.info("software: update available %s -> %s", installed, latest.version)


def _update_email_html(installed: str, latest: SoftwareRelease) -> str:
    notes_raw = (latest.notes or "").strip()
    notes = _html.escape(notes_raw).replace("\n", "<br>") if notes_raw else "—"
    manual_url = get_settings().software_manual_url
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>
<body style="margin:0;padding:0;background:#0e0f11;font-family:'Inter Tight','Helvetica Neue',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0e0f11;padding:32px 16px">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%">
  <tr><td style="padding:0 0 24px">
    <div style="font-family:'Inter Tight',sans-serif;font-size:24px;font-weight:700;color:#e8e8ea">
      poledash <span style="color:#ff5b14">Software-Update</span>
    </div>
    <div style="height:2px;background:#ff5b14;margin-top:12px;width:48px"></div>
  </td></tr>
  <tr><td style="padding-bottom:16px">
    <table width="100%" cellpadding="0" cellspacing="4"><tr>
      <td style="background:#1a1c20;padding:20px 24px;width:50%">
        <div style="font-family:'JetBrains Mono',monospace;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#8a8d93;margin-bottom:6px">Installiert</div>
        <div style="font-family:'Inter Tight',sans-serif;font-size:32px;font-weight:700;color:#e8e8ea;line-height:1">{installed}</div>
      </td>
      <td style="background:#1a1c20;padding:20px 24px;width:50%">
        <div style="font-family:'JetBrains Mono',monospace;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#8a8d93;margin-bottom:6px">Verfügbar</div>
        <div style="font-family:'Inter Tight',sans-serif;font-size:32px;font-weight:700;color:#ff5b14;line-height:1">{latest.version}</div>
      </td>
    </tr></table>
  </td></tr>
  <tr><td style="padding-bottom:16px">
    <div style="font-family:'JetBrains Mono',monospace;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#8a8d93;margin-bottom:8px">Release notes — {latest.version}</div>
    <div style="background:#1a1c20;padding:16px 20px;color:#e8e8ea;font-size:14px;line-height:1.5">{notes}</div>
  </td></tr>
  <tr><td style="padding-top:8px;padding-bottom:16px">
    <a href="{_html.escape(manual_url)}" style="font-family:'JetBrains Mono',monospace;font-size:12px;color:#ff5b14;text-decoration:none">Open the Polestar manual &rarr;</a>
  </td></tr>
  <tr><td style="padding-top:16px;border-top:1px solid #2a2c30">
    <div style="font-family:'JetBrains Mono',monospace;font-size:10px;color:#8a8d93;text-align:center">
      poledash &middot; Polestar 2 Telemetry Dashboard
    </div>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""


def dashboard_state(vin: str | None = None) -> dict:
    """Everything the /software page and /api/software endpoint need."""
    installed, ts, ivin = _current_installed(vin)
    with Session(get_engine()) as s:
        hist_stmt = select(SoftwareVersionChange).order_by(
            SoftwareVersionChange.detected_at.desc()
        )
        if ivin:
            hist_stmt = hist_stmt.where(SoftwareVersionChange.vin == ivin)
        history = list(s.exec(hist_stmt.limit(50)).all())
        c3_events = list(
            s.exec(
                select(C3SoftwareEvent)
                .order_by(C3SoftwareEvent.seen_at.desc())
                .limit(50)
            ).all()
        )
        releases = list(
            s.exec(
                select(SoftwareRelease).order_by(SoftwareRelease.version_key.desc())
            ).all()
        )

    # "Since when" = when we first detected the currently-installed version;
    # fall back to the car-reported timestamp if we have no change record yet.
    installed_since = ts
    if history and history[0].software_version == installed:
        installed_since = history[0].detected_at

    latest = releases[0] if releases else None
    installed_t = parse_version(installed)
    latest_t = parse_version(latest.version) if latest else None
    update_available = bool(installed_t and latest_t and latest_t > installed_t)

    checked_at = None
    raw = get_setting(LAST_CHECK_KEY)
    if raw:
        with contextlib.suppress(ValueError):
            checked_at = datetime.fromisoformat(raw)

    return {
        "installed": installed,
        "installed_since": installed_since,
        "installed_ts": ts,
        "vin": ivin,
        "history": history,
        "c3_events": c3_events,
        "releases": releases,
        "latest": latest,
        "update_available": update_available,
        "checked_at": checked_at,
        "manual_url": get_settings().software_manual_url,
    }
