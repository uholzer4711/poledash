"""Polestar Lab — exploration endpoints.

Live tooling that pokes at the Polestar API to surface fields beyond what
pypolestar currently maps. Everything here is read-only against the upstream
API; nothing writes back to Polestar.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import HTMLResponse
from gql import gql
from gql.transport.exceptions import TransportQueryError

from ..config import get_settings
from ..polestar.c3_control import COMMAND_META
from ..templating import make_templates

templates = make_templates()

log = logging.getLogger(__name__)
router = APIRouter()


# ---------- Lab page (consolidated: live C3 + raw + control + field discovery) ----------
@router.get("/lab", response_class=HTMLResponse)
def lab_page(request: Request):
    return templates.TemplateResponse(request, "lab.html", {"commands": COMMAND_META})


# ---------- Notes log (markdown, persisted in /data) ----------
def _notes_path() -> Path:
    s = get_settings()
    return s.db_path.parent / "lab_notes.md"


@router.get("/api/lab/notes")
def get_notes():
    p = _notes_path()
    if not p.exists():
        return {"content": "", "path": str(p)}
    return {"content": p.read_text(encoding="utf-8"), "path": str(p)}


@router.post("/api/lab/notes")
def append_notes(body: dict = Body(...)):
    text = (body or {}).get("text", "").strip()
    if not text:
        raise HTTPException(400, "text required")
    author = (body or {}).get("author", "user")
    ts = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    entry = f"\n### {ts} — {author}\n\n{text}\n"
    p = _notes_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(entry)
    return {"ok": True, "appended_chars": len(entry)}


# ---------- Upstream watch ----------
@router.get("/api/lab/upstream")
async def upstream_status():
    """Compare installed pypolestar vs PyPI latest vs GitHub HEAD."""
    out: dict = {"installed": None, "pypi_latest": None, "github_head": None,
                 "github_repo": "pypolestar/pypolestar", "issues_url":
                 "https://github.com/pypolestar/pypolestar/issues"}
    try:
        from importlib.metadata import version
        out["installed"] = version("pypolestar")
    except Exception as e:
        out["installed_error"] = str(e)

    async with httpx.AsyncClient(timeout=10) as cl:
        try:
            r = await cl.get("https://pypi.org/pypi/pypolestar/json")
            r.raise_for_status()
            j = r.json()
            out["pypi_latest"] = j["info"]["version"]
            out["pypi_released"] = j["releases"].get(out["pypi_latest"], [{}])[0].get(
                "upload_time"
            )
        except Exception as e:
            out["pypi_error"] = str(e)

        try:
            r = await cl.get(
                "https://api.github.com/repos/pypolestar/pypolestar/commits/main",
                headers={"Accept": "application/vnd.github+json"},
            )
            r.raise_for_status()
            j = r.json()
            out["github_head"] = j["sha"][:10]
            out["github_head_message"] = j["commit"]["message"].split("\n")[0][:120]
            out["github_head_date"] = j["commit"]["committer"]["date"]
            out["github_head_url"] = j["html_url"]
        except Exception as e:
            out["github_error"] = str(e)

        try:
            r = await cl.get(
                "https://api.github.com/repos/pypolestar/pypolestar/issues"
                "?state=open&per_page=8&sort=updated",
                headers={"Accept": "application/vnd.github+json"},
            )
            r.raise_for_status()
            out["recent_issues"] = [
                {"n": i["number"], "title": i["title"], "url": i["html_url"],
                 "updated_at": i["updated_at"]}
                for i in r.json() if "pull_request" not in i
            ]
        except Exception as e:
            out["issues_error"] = str(e)

    return out


# ---------- Proto catalog ----------
_PROTO_FIELD_RE = re.compile(
    r"^\s*(?:repeated\s+)?(?P<type>[A-Za-z_][\w.]*)\s+(?P<name>[a-z_][\w]*)\s*=\s*(?P<num>\d+)",
    re.MULTILINE,
)
# Fields our Snapshot dataclass reads (used by us = green; the rest = brach).
_USED_PROTO_FIELDS = {
    "battery_charge_level_percentage",
    "estimated_distance_to_empty_km",
    "estimated_distance_to_empty_miles",
    "estimated_charging_time_to_full_minutes",
    "charger_connection_status",
    "charging_status",
    "charging_power_watts",
    "charging_current_amps",
    "charging_voltage_volts",
    "charging_type",
    "average_energy_consumption_kwh_per_100_km",
    "average_energy_consumption_kwh_per_100_km_automatic",
    "average_energy_consumption_kwh_per_100_km_since_charge",
    "total_energy_consumption_wh",
    "total_energy_consumption_wh_automatic",
    "total_energy_consumption_wh_since_charge",
    "estimated_charging_time_minutes_to_target_distance",
    "estimated_charging_time_minutes_to_minimum_soc",
    "battery_charge_target_level",
    "charge_target_level_setting_type",
}


def _proto_dir() -> Path | None:
    try:
        import pypolestar.proto as _pp_proto
        return Path(_pp_proto.__file__).parent
    except Exception:
        return None


@router.get("/api/lab/proto-catalog")
def proto_catalog():
    pd = _proto_dir()
    if pd is None:
        raise HTTPException(500, "pypolestar.proto not importable")
    files: list[dict] = []
    for path in sorted(pd.glob("*.proto")):
        src = path.read_text(encoding="utf-8")
        msgs: list[dict] = []
        # group fields by their enclosing message{}
        for m in re.finditer(r"message\s+(\w+)\s*\{([^}]*)\}", src, re.DOTALL):
            mname = m.group(1)
            body = m.group(2)
            fields = [
                {
                    "name": fm.group("name"),
                    "type": fm.group("type"),
                    "num": int(fm.group("num")),
                    "used_by_us": fm.group("name") in _USED_PROTO_FIELDS,
                }
                for fm in _PROTO_FIELD_RE.finditer(body)
            ]
            if fields:
                msgs.append({"name": mname, "fields": fields})
        files.append({"file": path.name, "messages": msgs})
    return {"proto_dir": str(pd), "files": files}


# ---------- GraphQL probe ----------
# A probe field is interpolated into a GraphQL document, so it is whitelisted to
# a single identifier with an optional brace-wrapped sub-selection of identifiers
# ("name" or "name { sub1 sub2 }"). This blocks GraphQL injection (arguments,
# directives, aliases, extra operations) via the field input.
_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\s*\{\s*[A-Za-z_][A-Za-z0-9_\s]*\})?$")

_PARENT_QUERIES: dict[str, str] = {
    # how to wrap a sub-field test under each parent type
    "BatteryV2": "query Q($v:[String!]!){{carTelematicsV2(vins:$v){{battery{{vin {field}}}}}}}",
    "OdometerV2": "query Q($v:[String!]!){{carTelematicsV2(vins:$v){{odometer{{vin {field}}}}}}}",
    "HealthV2": "query Q($v:[String!]!){{carTelematicsV2(vins:$v){{health{{vin {field}}}}}}}",
    "CarTelematicsV2": "query Q($v:[String!]!){{carTelematicsV2(vins:$v){{{field}}}}}",
    "ConsumerCarsV2": "query{{getConsumerCarsV2{{{field}}}}}",
    "Query": "query{{{field}}}",
}


async def _exec_probe(api, query_str: str, variables: dict | None):
    # Go through gql_session directly so we see the original TransportQueryError
    # — pypolestar's `_query_graph_ql` wraps everything as PolestarApiException
    # and loses the validation message.
    sess = api.gql_session_private
    headers = {"Authorization": f"Bearer {api.auth.access_token}"}
    try:
        result = await sess.execute(
            gql(query_str), variable_values=variables,
            extra_args={"headers": headers},
        )
        return {"status": "ok", "result": result}
    except TransportQueryError as e:
        errs = e.errors or []
        first = errs[0] if errs else {}
        msg = first.get("message", "")
        if "FieldUndefined" in msg:
            kind = "undefined"
        elif (
            "SubSelectionRequired" in msg
            or "MissingFieldArgument" in msg
            or "SubSelectionNotAllowed" in msg
            or "WrongType" in msg
        ):
            kind = "shape_hint"
        else:
            kind = "error"
        return {"status": kind, "message": msg, "errors": errs}
    except Exception as e:
        return {"status": "error", "message": repr(e)}


@router.post("/api/lab/probe")
async def graphql_probe(request: Request, body: dict = Body(...)):
    """Probe one or more candidate fields under a parent type.

    Body: {"parent": "BatteryV2", "fields": ["foo","bar"], "vin": "optional"}
    """
    parent = body.get("parent")
    fields = body.get("fields") or []
    if not parent or parent not in _PARENT_QUERIES:
        raise HTTPException(400, f"parent must be one of: {sorted(_PARENT_QUERIES)}")
    if not isinstance(fields, list) or not fields:
        raise HTTPException(400, "fields must be a non-empty list")
    # Validate input up-front (before touching the client): each field is
    # interpolated into a GraphQL document, so allow ONLY a bare identifier with
    # an optional brace-wrapped sub-selection ("name" or "name { sub1 sub2 }").
    # This blocks GraphQL injection (arguments, directives, aliases, extra
    # operations) via the field input.
    for f in fields:
        if not isinstance(f, str) or not _FIELD_RE.match(f):
            raise HTTPException(
                400, f"invalid field name {f!r}: only 'name' or 'name {{ a b }}' allowed"
            )

    client = request.app.state.client
    if not client or not getattr(client, "_api", None):
        raise HTTPException(503, "Polestar client not connected yet")
    api = client.api
    vin = body.get("vin") or (client.vins[0] if client.vins else None)
    template = _PARENT_QUERIES[parent]
    needs_vin = "$v" in template
    if needs_vin and not vin:
        raise HTTPException(400, "no VIN available")

    results = []
    for f in fields:
        q = template.format(field=f)
        variables = {"v": [vin]} if needs_vin else None
        results.append({"field": f, **await _exec_probe(api, q, variables)})
        await asyncio.sleep(0.05)
    return {"parent": parent, "vin": vin, "results": results}


# ---------- Raw gRPC battery dump ----------
@router.get("/api/lab/grpc-battery-raw")
def grpc_battery_raw(request: Request):
    """Return the raw protobuf repr of the last gRPC Battery response.

    Surfaces fields that pypolestar's `_parse_battery` drops on the floor —
    our patch (client.py) stashes the full proto text on the parsed object.
    """
    client = request.app.state.client
    if not client or not getattr(client, "_api", None):
        raise HTTPException(503, "Polestar client not connected yet")
    out = []
    for vin in client.vins:
        try:
            gb = client.api.get_grpc_battery(vin=vin)
        except Exception as e:
            out.append({"vin": vin, "error": repr(e)})
            continue
        if gb is None:
            out.append({"vin": vin, "error": "no gRPC battery cached yet"})
            continue
        extras = {k: getattr(gb, k, None) for k in (
            "avg_consumption_kwh_per_100km_automatic",
            "avg_consumption_kwh_per_100km_since_charge",
            "total_energy_consumption_wh",
            "total_energy_consumption_wh_automatic",
            "total_energy_consumption_wh_since_charge",
            "estimated_distance_to_empty_miles",
            "charger_power_status_raw",
        )}
        out.append({
            "vin": vin,
            "raw_proto": getattr(gb, "_raw_proto_repr", None),
            "extras": extras,
        })
    return {"items": out}


# ---------- Known queries pypolestar uses ----------
@router.get("/api/lab/known-queries")
def known_queries():
    """List the GraphQL queries pypolestar currently sends.

    Surfacing the source-of-truth so the user can see what we already ask for
    and what's still candidate territory.
    """
    try:
        import pypolestar.graphql as g
        src = Path(g.__file__).read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"cannot read pypolestar.graphql: {e}") from e
    queries = []
    for m in re.finditer(r"(QUERY_[A-Z_]+)\s*=\s*gql\(\s*\"\"\"(.*?)\"\"\"\s*\)", src, re.DOTALL):
        queries.append({"name": m.group(1), "body": m.group(2).strip()})
    return {"queries": queries}
