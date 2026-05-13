"""TSC + REST + Metadata-backed tools for the Scout agent.

Reuses the active TSC server passed via configure(). Nothing here writes to
Tableau — these are read-only investigation tools.
"""
from typing import Any

import tableauserverclient as TSC

from db import connect

_session: dict[str, Any] = {}


def configure(server) -> None:
    _session["server"] = server


def _server():
    s = _session.get("server")
    if s is None:
        raise RuntimeError("tableau_tools not configured (call configure(server) inside auth context)")
    return s


def _find_target(target_name: str) -> dict | None:
    with connect() as c:
        row = c.execute(
            "SELECT target_type, target_id FROM targets WHERE target_name = ? ORDER BY pulled_at DESC LIMIT 1",
            (target_name,),
        ).fetchone()
    return dict(row) if row else None


def get_datasource_owner(target_name: str) -> dict:
    """Returns the owner (username, email) of a datasource by name."""
    server = _server()
    t = _find_target(target_name)
    if t is None or t.get("target_type") != "datasource":
        return {"error": f"No datasource named {target_name!r} in our catalog."}
    try:
        ds = server.datasources.get_by_id(t["target_id"])
        owner_id = getattr(ds, "owner_id", None)
        if not owner_id:
            return {"target_name": target_name, "error": "owner_id missing on datasource"}
        u = server.users.get_by_id(owner_id)
        return {
            "target_name": target_name,
            "owner": {
                "username": getattr(u, "name", None),
                "fullname": getattr(u, "fullname", None),
                "email":    getattr(u, "email", None),
                "site_role": getattr(u, "site_role", None),
            },
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:300]}"}


def get_datasource_connections(target_name: str) -> dict:
    """Lists the upstream connections (databases/files) for a datasource."""
    server = _server()
    t = _find_target(target_name)
    if t is None or t.get("target_type") != "datasource":
        return {"error": f"No datasource named {target_name!r} in our catalog."}
    try:
        ds = server.datasources.get_by_id(t["target_id"])
        server.datasources.populate_connections(ds)
        return {
            "target_name": target_name,
            "connections": [
                {
                    "type":     getattr(c, "connection_type", None),
                    "server":   getattr(c, "server_address", None),
                    "username": getattr(c, "username", None),
                    "database": getattr(c, "datasource_name", None),
                }
                for c in (ds.connections or [])
            ],
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:300]}"}


def get_downstream_assets(target_name: str) -> dict:
    """Uses Metadata API to find workbooks/sheets that depend on a datasource.
    Useful for blast-radius / consequence weighting."""
    server = _server()
    safe = target_name.replace('"', '\\"')
    query = (
        '{ publishedDatasources(filter: {name: "' + safe + '"}) { '
        'luid name '
        'downstreamWorkbooks { luid name updatedAt owner { username } } '
        'downstreamSheets { luid name } '
        '} }'
    )
    try:
        result = server.metadata.query(query)
        if not isinstance(result, dict) or result.get("errors"):
            return {"error": f"Metadata API errors: {result.get('errors') if isinstance(result, dict) else result}"}
        matches = (result.get("data") or {}).get("publishedDatasources") or []
        if not matches:
            return {"error": f"No published datasource named {target_name!r}"}
        ds = matches[0]
        wbs = ds.get("downstreamWorkbooks") or []
        sheets = ds.get("downstreamSheets") or []
        return {
            "target_name":            target_name,
            "downstream_workbook_count": len(wbs),
            "downstream_sheet_count":    len(sheets),
            "downstream_workbooks":      wbs[:20],   # cap to keep payload small
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:300]}"}


def get_failed_job_details(job_id: str) -> dict:
    """Hydrates a single job via jobs.get_by_id — returns finish_code, full notes,
    error context. WARNING: this hits the rate-limited jobs/list bucket; will
    return a structured error if 429'd. Use sparingly."""
    server = _server()
    try:
        job = server.jobs.get_by_id(job_id)
        raw_notes = getattr(job, "notes", None)
        if raw_notes is None:
            notes: list[str] = []
        elif isinstance(raw_notes, str):
            notes = [raw_notes]
        else:
            notes = [getattr(n, "text", None) or getattr(n, "value", None) or str(n) for n in raw_notes]
        return {
            "job_id":        job.id,
            "type":          job.type,
            "finish_code":   getattr(job, "finish_code", None),
            "created_at":    str(getattr(job, "created_at", None)),
            "started_at":    str(getattr(job, "started_at", None)),
            "completed_at":  str(getattr(job, "completed_at", None)),
            "notes":         notes,
            "datasource_id": getattr(job, "datasource_id", None),
            "workbook_id":   getattr(job, "workbook_id", None),
            "subtitle":      getattr(job, "subtitle", None),
        }
    except TSC.ServerResponseError as e:
        msg = str(e)
        if "429" in msg:
            return {
                "error":        "Rate-limited on jobs/list bucket — Tableau Cloud is throttling job lookups right now.",
                "rate_limited": True,
                "hint":         "Use refresh_history (already in scout.db) to reason about the job; the notes/error string is unavailable until the limit lifts.",
                "raw":          msg[:200],
            }
        return {"error": f"{type(e).__name__}: {msg[:300]}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:300]}"}
