"""Read-only MCP server for OpenProject.

Reuses ProjectClient / OpenProjectConnector from the Flask app so API quirks,
pagination and caching stay in one place. No write tools are exposed.

Run:  python src/mcp_server.py            (stdio, for opencode/Claude Desktop)
"""

# Two OpenProject API quirks this server works around, both verified live:
#  - /projects/{id}/work_packages returns top-level work packages only, so the
#    project filter on /work_packages is used wherever completeness matters.
#  - filters need numeric ids; status/type/priority names are resolved first.

import os
import sys
import json
import logging
import datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from config import OPENPROJECT_URL, API_KEY
from openproject_connector import OpenProjectConnector
from project_client import ProjectClient
from cache import cache

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

mcp = MCPServer(
    name="openproject",
    version="1.0.0",
    instructions=(
        "Read-only access to OpenProject. Use list_projects / get_project for context, "
        "list_users / get_user to resolve assignees, list_work_packages to query work, "
        "get_work_package for one item. build_report produces CSV/XLSX files. "
        "OPENPROJECT_URL is in the environment; never invent URLs."
    ),
)

_connector = OpenProjectConnector(OPENPROJECT_URL, API_KEY)
_client = ProjectClient(_connector)

WORK_PACKAGE_LIMIT = 500
CLOSED_STATUSES = {"completed", "rejected", "closed"}


# ---------------------------------------------------------------- helpers
def _fail(msg: str):
    raise ToolError(msg)


def _require_id(value: int, label: str) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        _fail(f"{label} must be an integer, got {value!r}")
    if v <= 0:
        _fail(f"{label} must be positive, got {v}")
    return v


def _text(value: str, label: str, limit: int = 500) -> str:
    v = (value or "").strip()
    if not v:
        _fail(f"{label} must not be empty")
    return v[:limit]


def _project_id_from_href(href: str) -> int | None:
    if not href:
        return None
    try:
        return int(href.rstrip("/").split("/")[-1])
    except ValueError:
        return None


def _iso_duration(iso: str | None) -> float | None:
    """Convert an ISO-8601 duration (PT10H, P2DT2H) to hours."""
    if not iso:
        return None
    s = str(iso)
    if not s.startswith("P"):
        return None
    hours = 0.0
    days = months = 0
    num = ""
    in_time = False
    for ch in s[1:]:
        if ch == "T":
            in_time = True
            continue
        if ch.isdigit() or ch == ".":
            num += ch
            continue
        val = float(num) if num else 0.0
        num = ""
        if ch == "D":
            days = val
        elif ch == "W":
            days += val * 7
        elif ch in ("M",) and not in_time:
            months += val
        elif ch == "H":
            hours += val
        elif ch == "M" and in_time:
            hours += val
        elif ch == "S":
            hours += val / 3600
    if months:
        hours += months * 160
    return round(hours + days * 24, 2)


def _wp_summary(wp: dict) -> dict:
    L = wp.get("_links", {})
    project = L.get("project") or {}
    return {
        "id": wp.get("id"),
        "subject": wp.get("subject"),
        "type": (L.get("type") or {}).get("title"),
        "status": (L.get("status") or {}).get("title"),
        "priority": (L.get("priority") or {}).get("title"),
        "project": project.get("title"),
        "project_id": _project_id_from_href(project.get("href", "")),
        "assignee": (L.get("assignee") or {}).get("title"),
        "responsible": (L.get("responsible") or {}).get("title"),
        "start_date": wp.get("startDate"),
        "due_date": wp.get("dueDate"),
        "created_at": wp.get("createdAt"),
        "updated_at": wp.get("updatedAt"),
        "done_ratio": wp.get("doneRatio"),
        "estimated_hours": _iso_duration(wp.get("estimatedTime")),
        "spent_hours": _iso_duration(wp.get("spentTime")),
        "url": f"{OPENPROJECT_URL}/work_packages/{wp.get('id')}",
    }


def _resolve_project(identifier: str) -> dict:
    """Accept either a numeric id or an identifier like 'zeroerr-research'."""
    ident = _text(identifier, "project", 200)
    projects = _client.get_all_projects()
    if ident.isdigit():
        pid = int(ident)
        for p in projects:
            if p.get("id") == pid:
                return p
        _fail(f"No project with id {pid}")
    for p in projects:
        if p.get("identifier") == ident:
            return p
    for p in projects:
        if (p.get("name") or "") == ident:
            return p
    low = ident.lower()
    for p in projects:
        if low in (p.get("name") or "").lower():
            return p
    _fail(
        f"No project matching {ident!r}. Use list_projects to see valid ids, "
        f"names and identifiers (e.g. '25488-zeroerr-research')."
    )


def _resolve_user(who: str) -> dict:
    w = _text(who, "user", 200)
    users = _client.get_all_users()
    if w.isdigit():
        uid = int(w)
        for u in users:
            if u.get("id") == uid:
                return u
        _fail(f"No user with id {uid}")
    low = w.lower()
    for u in users:
        if (u.get("login") or "").lower() == low:
            return u
    for u in users:
        if (u.get("name") or "").lower() == low:
            return u
    for u in users:
        if low in (u.get("name") or "").lower() or low in (u.get("mail") or "").lower():
            return u
    _fail(f"No user matching {w!r}. Use list_users to see valid values.")


def _fss(value: str, field: str) -> dict:
    """Build an OpenProject filter set for a simple field."""
    return {field: {"operator": "=", "values": [value]}}


def _resolve_status(raw: str) -> str:
    """Status filter values must be numeric ids; resolve a name to its id."""
    v = _text(raw, "status", 100)
    if v.isdigit():
        return v
    for s in _client.get_statuses():
        if (s.get("name") or "").lower() == v.lower():
            return str(s.get("id"))
    _fail(f"Unknown status {raw!r}. Use the openproject://statuses resource for valid names.")


def _resolve_catalogue(raw: str, label: str, endpoint: str, key: str) -> str:
    """Resolve a type or priority name to its numeric id for filtering."""
    v = _text(raw, label, 100)
    if v.isdigit():
        return v
    for item in _connector.get(endpoint).get("_embedded", {}).get("elements", []):
        if (item.get(key) or "").lower() == v.lower():
            return str(item.get("id"))
    _fail(f"Unknown {label} {raw!r}.")


def _resolve_type(raw: str) -> str:
    return _resolve_catalogue(raw, "type", "/types", "name")


def _resolve_priority(raw: str) -> str:
    return _resolve_catalogue(raw, "priority", "/priorities", "name")


def _all_project_work_packages(project_id: int) -> list[dict]:
    """Every work package in a project, children included.

    /projects/{id}/work_packages only returns top-level items, so the project
    filter on /work_packages is the only complete source.
    """
    import urllib.parse
    f = json.dumps([_fss(str(project_id), "project")])
    return _client._paginate(f"/work_packages?perPage=100&filters={urllib.parse.quote(f)}", limit=WORK_PACKAGE_LIMIT)


# ================================================================ discovery
@mcp.tool()
def list_projects(
    search: str = "",
    active_only: bool = True,
    limit: int = 200,
) -> dict:
    """List all OpenProject projects with counts. Use this before any project query."""
    projects = _client.get_all_projects()
    term = (search or "").strip().lower()

    out = []
    for p in projects:
        if active_only and p.get("isActive") is False:
            continue
        if term and term not in (p.get("name", "") or "").lower() and term not in (p.get("identifier", "") or "").lower():
            continue
        out.append({
            "id": p.get("id"),
            "name": p.get("name"),
            "identifier": p.get("identifier"),
            "description": (p.get("description") or {}).get("raw", "")[:300] if isinstance(p.get("description"), dict) else None,
            "is_active": p.get("isActive"),
            "start_date": p.get("startDate"),
            "due_date": p.get("dueDate"),
            "percent_complete": p.get("percentComplete"),
        })
        if len(out) >= max(1, min(int(limit), 1000)):
            break
    return {"count": len(out), "projects": out}


@mcp.tool()
def list_users(
    search: str = "",
    active_only: bool = True,
    limit: int = 300,
) -> dict:
    """List OpenProject users. 'search' matches name, login or email."""
    users = _client.get_all_users()
    term = (search or "").strip().lower()

    out = []
    for u in users:
        if active_only and u.get("isActive") is False:
            continue
        if term:
            hay = f"{u.get('name','')} {u.get('login','')} {u.get('mail','')}".lower()
            if term not in hay:
                continue
        out.append({
            "id": u.get("id"),
            "name": u.get("name"),
            "login": u.get("login"),
            "email": u.get("mail"),
            "admin": u.get("admin"),
            "status": u.get("status"),
        })
        if len(out) >= max(1, min(int(limit), 1000)):
            break
    return {"count": len(out), "users": out}


@mcp.tool()
def get_project(project: str) -> dict:
    """Get one project by numeric id or identifier, with its member count."""
    p = _resolve_project(project)
    pid = _require_id(p.get("id"), "project id")
    detail = _client.get_project_by_id(pid)
    members = _client.get_project_members(pid)
    return {
        "id": pid,
        "name": detail.get("name"),
        "identifier": detail.get("identifier"),
        "description": (detail.get("description") or {}).get("raw") if isinstance(detail.get("description"), dict) else detail.get("description"),
        "is_active": detail.get("isActive"),
        "start_date": detail.get("startDate"),
        "due_date": detail.get("dueDate"),
        "percent_complete": detail.get("percentComplete"),
        "member_count": len(members),
        "url": f"{OPENPROJECT_URL}/projects/{detail.get('identifier')}",
    }


@mcp.tool()
def get_user(user: str) -> dict:
    """Get one user by id, login or name, with assigned-work summary."""
    u = _resolve_user(user)
    uid = _require_id(u.get("id"), "user id")
    data = _client.get_full_user(uid)
    tasks = data["tasks"]
    est = spent = 0.0
    for t in tasks:
        e = _iso_duration(t.get("estimatedTime"))
        s = _iso_duration(t.get("spentTime"))
        est += e or 0.0
        spent += s or 0.0
    return {
        "id": uid,
        "name": u.get("name"),
        "login": u.get("login"),
        "email": u.get("mail"),
        "admin": u.get("admin"),
        "assigned_count": len(tasks),
        "project_count": len(data["projects"]),
        "estimated_hours_total": round(est, 2),
        "spent_hours_total": round(spent, 2),
        "projects": data["projects"],
    }


@mcp.tool()
def ping() -> dict:
    """Verify connectivity and credentials to the OpenProject API."""
    data = _connector.get("/statuses")
    return {
        "ok": True,
        "openproject_url": OPENPROJECT_URL,
        "api_key_configured": bool(API_KEY),
        "api_key_length": len(API_KEY),
        "statuses_available": len(data.get("_embedded", {}).get("elements", [])),
    }


# ================================================================ work packages
@mcp.tool()
def list_work_packages(
    project: str = "",
    assignee: str = "",
    responsible: str = "",
    status: str = "",
    wtype: str = "",
    priority: str = "",
    search: str = "",
    updated_since: str = "",
    include_closed: bool = True,
    limit: int = 200,
) -> dict:
    """Search work packages across all projects or one project.

    Every filter is optional and combined with AND. project accepts id, identifier
    or name; assignee accepts id, login or name; status, wtype and priority accept
    the display name or numeric id. Results are sorted with open work first.
    """
    lim = max(1, min(int(limit), WORK_PACKAGE_LIMIT))
    fss: list[dict] = []

    project_name = project_id = None
    if project:
        p = _resolve_project(project)
        project_id = _require_id(p.get("id"), "project id")
        project_name = p.get("name")

    for field, raw, label in (("assignee", assignee, "assignee"), ("responsible", responsible, "responsible")):
        if raw:
            u = _resolve_user(raw)
            fss.append(_fss(str(u.get("id")), field))

    if status:
        fss.append(_fss(_resolve_status(status), "status"))
    if wtype:
        fss.append(_fss(_resolve_type(wtype), "type"))
    if priority:
        fss.append(_fss(_resolve_priority(priority), "priority"))

    if updated_since:
        _text(updated_since, "updated_since", 30)

    if project_id:
        fss.append(_fss(str(project_id), "project"))
        endpoint = "/work_packages"
    else:
        endpoint = "/work_packages"

    query = "perPage=100"
    if fss:
        import urllib.parse
        query += "&filters=" + urllib.parse.quote(json.dumps(fss))
    if updated_since:
        query += f"&updatedSince={updated_since}"

    wps = _client._paginate(f"{endpoint}?{query}", limit=WORK_PACKAGE_LIMIT)

    if search:
        term = search.strip().lower()
        wps = [w for w in wps if term in (w.get("subject", "") or "").lower()
               or term in str((w.get("description") or {}).get("raw", "")).lower()]

    if not include_closed:
        wps = [w for w in wps
               if ((w.get("_links", {}).get("status") or {}).get("title") or "").lower() not in CLOSED_STATUSES]

    rows = [_wp_summary(w) for w in wps]

    def key(r):
        closed = (r["status"] or "").lower() in CLOSED_STATUSES
        return (closed, r["due_date"] is None, r["due_date"] or "9999", r["id"])

    rows.sort(key=key)
    truncated = len(rows) > lim
    return {
        "count": min(len(rows), lim),
        "total_matched": len(rows),
        "truncated": truncated,
        "scoped_to_project": project_name,
        "work_packages": rows[:lim],
    }


@mcp.tool()
def get_work_package(work_package_id: int) -> dict:
    """Get full detail for one work package, including allowed status transitions."""
    wpid = _require_id(work_package_id, "work_package_id")
    wp = _connector.get(f"/work_packages/{wpid}")
    out = _wp_summary(wp)
    L = wp.get("_links", {})

    out["description"] = (
        (wp.get("description") or {}).get("raw") if isinstance(wp.get("description"), dict) else wp.get("description")
    )
    out["lock_version"] = wp.get("lockVersion")
    out["custom_fields"] = {
        str(cf.get("name")): (cf.get("value") if cf.get("value") is not None else cf.get("_links", {}).get("title"))
        for cf in (wp.get("customFields") or [])
    }
    out["categories"] = [c.get("title") for c in (wp.get("_embedded", {}).get("category") or [])]
    out["allowed_status_transitions"] = [
        {"id": v.get("href", "").rstrip("/").split("/")[-1], "name": v.get("title")}
        for v in ((L.get("status") or {}).get("allowedValues") or [])
    ]
    out["parent_id"] = _project_id_from_href((L.get("parent") or {}).get("href", "")) if L.get("parent") else None
    return out


@mcp.tool()
def get_project_tasks(
    project: str,
    include_closed: bool = True,
    limit: int = 300,
) -> dict:
    """All work packages of one project, grouped by status with a small summary."""
    p = _resolve_project(project)
    pid = _require_id(p.get("id"), "project id")
    wps = _all_project_work_packages(pid)

    if not include_closed:
        wps = [w for w in wps
               if ((w.get("_links", {}).get("status") or {}).get("title") or "").lower() not in CLOSED_STATUSES]

    rows = [_wp_summary(w) for w in wps]
    rows.sort(key=lambda r: ((r["status"] or "").lower() in CLOSED_STATUSES, r["status"] or "", r["id"]))
    lim = max(1, min(int(limit), WORK_PACKAGE_LIMIT))

    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"] or "None"] = by_status.get(r["status"] or "None", 0) + 1

    return {
        "project": {"id": pid, "name": p.get("name"), "identifier": p.get("identifier")},
        "total": len(rows),
        "count_by_status": dict(sorted(by_status.items(), key=lambda kv: -kv[1])),
        "truncated": len(rows) > lim,
        "work_packages": rows[:lim],
    }


# ================================================================ relations
@mcp.tool()
def get_relations(work_package_id: int) -> dict:
    """Parent, children and related/relates links of one work package."""
    wpid = _require_id(work_package_id, "work_package_id")

    def slim(item):
        wp = item.get("_embedded", {}).get("workPackage") or item
        L = wp.get("_links", {})
        parent = L.get("parent") or {}
        return {
            "id": wp.get("id"),
            "subject": wp.get("subject"),
            "status": (L.get("status") or {}).get("title"),
            "project": (L.get("project") or {}).get("title"),
            "parent_id": _project_id_from_href(parent.get("href", "")) if parent else None,
        }

    wp = _connector.get(f"/work_packages/{wpid}")
    L = wp.get("_links", {})

    parent = L.get("parent") or {}
    parents = [slim(_connector.get(parent["href"]))] if parent.get("href") else []

    child_links = L.get("children") or []
    children = [slim(_connector.get(cl["href"])) for cl in child_links if cl.get("href")]

    related = []
    for rel in (wp.get("_embedded", {}).get("relations") or {}).get("elements", []) if isinstance(
        wp.get("_embedded", {}).get("relations"), dict
    ) else (wp.get("_embedded", {}).get("relations") or []):
        related.append({
            "type": rel.get("type"),
            "direction": rel.get("_links", {}).get("direction"),
            "work_package": slim(rel.get("_embedded", {}).get("workPackage") or {}),
        })

    return {
        "work_package_id": wpid,
        "subject": wp.get("subject"),
        "parents": parents,
        "children": children,
        "related_count": len(related),
        "related": related[:100],
    }


@mcp.tool()
def get_hierarchy(
    project: str = "",
    work_package_id: int = 0,
    max_depth: int = 3,
) -> dict:
    """Walk the parent/child tree. Give work_package_id for a subtree, or project for its roots."""
    depth = max(1, min(int(max_depth), 6))

    if work_package_id:
        wpid = _require_id(work_package_id, "work_package_id")
        start = _connector.get(f"/work_packages/{wpid}")
        roots = [start] if (start.get("_links", {}).get("children")) else []
        context = {"root_id": wpid, "root_subject": start.get("subject"), "scope": "subtree"}
    else:
        if not project:
            _fail("Provide either work_package_id or project")
        p = _resolve_project(project)
        pid = _require_id(p.get("id"), "project id")
        wps = _all_project_work_packages(pid)
        ids = {w.get("id") for w in wps}
        roots = [w for w in wps if not (w.get("_links", {}).get("parent") or {}).get("href")
                 or _project_id_from_href(w["_links"]["parent"]["href"]) not in ids]
        context = {"root_id": None, "root_subject": p.get("name"), "scope": f"project {p.get('identifier')}"}

    def walk(items, level):
        out = []
        for w in items:
            L = w.get("_links", {})
            node = {
                "id": w.get("id"),
                "subject": w.get("subject"),
                "status": (L.get("status") or {}).get("title"),
                "type": (L.get("type") or {}).get("title"),
                "assignee": (L.get("assignee") or {}).get("title"),
                "due_date": w.get("dueDate"),
                "depth": level,
                "children": [],
            }
            if level < depth:
                kids = []
                for cl in (w.get("_links", {}).get("children") or []):
                    if cl.get("href"):
                        kids.append(_connector.get(cl["href"]))
                node["children"] = walk(kids, level + 1)
            out.append(node)
        return out

    tree = walk(roots, 0)

    def count(nodes):
        return len(nodes) + sum(count(n["children"]) for n in nodes)

    return {**context, "root_count": len(tree), "node_count": count(tree), "tree": tree}


# ================================================================ time tracking
@mcp.tool()
def get_time_entries(
    work_package_id: int = 0,
    user: str = "",
    project: str = "",
    limit: int = 200,
) -> dict:
    """Read logged work time. Filter by work package, user and/or project."""
    fss = []
    if user:
        u = _resolve_user(user)
        fss.append(_fss(str(u.get("id")), "user"))
    if project:
        p = _resolve_project(project)
        fss.append(_fss(str(p.get("id")), "project"))

    lim = max(1, min(int(limit), 1000))

    import urllib.parse

    if work_package_id:
        wpid = _require_id(work_package_id, "work_package_id")
        fss = [_fss(str(wpid), "work_package_id")]

    query = "perPage=100"
    if fss:
        query += "&filters=" + urllib.parse.quote(json.dumps(fss))
    entries = _client._paginate(f"/time_entries?{query}", limit=lim)

    rows = []
    for e in entries:
        rows.append({
            "id": e.get("id"),
            "work_package_id": (e.get("_links", {}).get("workPackage") or {}).get("href", "").rstrip("/").split("/")[-1],
            "user": (e.get("_links", {}).get("user") or {}).get("title"),
            "project": (e.get("_links", {}).get("project") or {}).get("title"),
            "activity": (e.get("_links", {}).get("activity") or {}).get("title"),
            "hours": _iso_duration(e.get("hours")),
            "spent_on": e.get("spentOn"),
            "comment": ((e.get("comment") or {}).get("raw") or "")[:200]
            if isinstance(e.get("comment"), dict) else None,
        })

    total = round(sum(r["hours"] or 0.0 for r in rows), 2)
    return {
        "entry_count": len(rows),
        "total_hours": total,
        "entries": rows[:lim],
    }


@mcp.tool()
def get_time_summary(
    user: str = "",
    project: str = "",
) -> dict:
    """Estimated vs spent vs remaining hours rolled up per project (or for one user's work)."""
    if user:
        u = _resolve_user(user)
        data = _client.get_full_user(_require_id(u.get("id"), "user id"))
        wps, scope = data["tasks"], {"scope": "user", "user": u.get("name"), "user_id": u.get("id")}
    else:
        p = _resolve_project(project)
        pid = _require_id(p.get("id"), "project id")
        wps = _all_project_work_packages(pid)
        scope = {"scope": "project", "project": p.get("name"), "project_id": pid}

    per_project: dict[str, dict] = {}
    for w in wps:
        name = (w.get("_links", {}).get("project") or {}).get("title") or "Unknown"
        est = _iso_duration(w.get("estimatedTime")) or 0.0
        spent = _iso_duration(w.get("spentTime")) or 0.0
        bucket = per_project.setdefault(name, {"estimated_hours": 0.0, "spent_hours": 0.0, "work_packages": 0, "with_estimate": 0})
        bucket["estimated_hours"] += est
        bucket["spent_hours"] += spent
        bucket["work_packages"] += 1
        if est:
            bucket["with_estimate"] += 1

    for b in per_project.values():
        b["estimated_hours"] = round(b["estimated_hours"], 2)
        b["spent_hours"] = round(b["spent_hours"], 2)
        b["remaining_hours"] = round(b["estimated_hours"] - b["spent_hours"], 2)
        b["consumption_pct"] = (
            round(b["spent_hours"] / b["estimated_hours"] * 100, 1) if b["estimated_hours"] else None
        )

    rows = sorted(per_project.items(), key=lambda kv: -kv[1]["spent_hours"])
    tot_est = round(sum(b["estimated_hours"] for _n, b in rows), 2)
    tot_spent = round(sum(b["spent_hours"] for _n, b in rows), 2)
    return {
        **scope,
        "total_estimated_hours": tot_est,
        "total_spent_hours": tot_spent,
        "total_remaining_hours": round(tot_est - tot_spent, 2),
        "overall_consumption_pct": round(tot_spent / tot_est * 100, 1) if tot_est else None,
        "by_project": [{"project": n, **b} for n, b in rows],
    }


@mcp.tool()
def get_overdue(
    project: str = "",
    assignee: str = "",
    limit: int = 100,
) -> dict:
    """Open work packages already past their due date, worst first."""
    today = datetime.date.today()
    wps = _client.get_user_work_packages(_require_id(_resolve_user(assignee).get("id"), "user id")) if assignee else None

    if wps is None:
        if project:
            p = _resolve_project(project)
            wps = _all_project_work_packages(_require_id(p.get("id"), "project id"))
        else:
            out, offset = [], 0
            for p in _client.get_all_projects():
                if p.get("isActive") is False:
                    continue
                offset += 1
                if offset > 25:
                    break
                out.extend(_all_project_work_packages(_require_id(p.get("id"), "project id")))
            wps = out

    rows = []
    for w in wps:
        L = w.get("_links", {})
        status = ((L.get("status") or {}).get("title") or "").lower()
        if status in CLOSED_STATUSES:
            continue
        due = w.get("dueDate")
        if not due:
            continue
        try:
            due_d = datetime.date.fromisoformat(due[:10])
        except ValueError:
            continue
        if due_d >= today:
            continue
        r = _wp_summary(w)
        r["days_overdue"] = (today - due_d).days
        rows.append(r)

    rows.sort(key=lambda r: -r["days_overdue"])
    lim = max(1, min(int(limit), 500))
    return {
        "as_of": today.isoformat(),
        "count": min(len(rows), lim),
        "total_overdue": len(rows),
        "work_packages": rows[:lim],
    }


# ================================================================ exports
@mcp.tool()
def build_report(
    assignee: str = "",
    project: str = "",
    kind: str = "xlsx",
    output_path: str = "",
) -> dict:
    """Export matching work packages to a CSV or a formatted XLSX file.

    Filter by assignee and/or project. Returns the absolute path of the file written.
    """
    k = (kind or "xlsx").lower()
    if k not in ("xlsx", "csv"):
        _fail("kind must be 'xlsx' or 'csv'")

    rows: list[dict] = []
    title_bits = []
    if assignee:
        u = _resolve_user(assignee)
        rows = list(_client.get_user_work_packages(_require_id(u.get("id"), "user id")))
        title_bits.append(u.get("name"))
    if project:
        p = _resolve_project(project)
        pid = _require_id(p.get("id"), "project id")
        existing = {r["id"] for r in rows}
        rows.extend([w for w in _all_project_work_packages(pid) if w.get("id") not in existing])
        title_bits.append(p.get("identifier"))

    if not rows:
        _fail("No work packages matched. Provide assignee and/or project.")

    summaries = [_wp_summary(w) for w in rows]
    summaries.sort(key=lambda r: r["id"])

    default_name = "openproject_report_" + "_".join(
        (t or "").replace(" ", "_") for t in title_bits if t
    ).lower() + (".xlsx" if k == "xlsx" else ".csv")
    path = (output_path or "").strip() or os.path.join(os.path.expanduser("~"), default_name)
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    if k == "csv" and not path.lower().endswith(".csv"):
        path += ".csv"
    if k == "xlsx" and not path.lower().endswith((".xlsx", ".xlsm")):
        path += ".xlsx"

    cols = [
        ("id", "ID"), ("subject", "Subject"), ("project", "Project"), ("status", "Status"),
        ("priority", "Priority"), ("type", "Type"), ("assignee", "Assignee"),
        ("responsible", "Responsible"), ("start_date", "Start"), ("due_date", "Due"),
        ("done_ratio", "% Done"), ("estimated_hours", "Estimated h"), ("spent_hours", "Spent h"),
        ("created_at", "Created"), ("updated_at", "Updated"), ("url", "Link"),
    ]

    if k == "csv":
        import csv as _csv
        with open(path, "w", newline="", encoding="utf-8") as fh:
            wtr = _csv.writer(fh)
            wtr.writerow([c[1] for c in cols])
            for r in summaries:
                wtr.writerow([r.get(c[0]) for c in cols])
    else:
        _write_xlsx(path, summaries, cols, title_bits)

    return {
        "path": path,
        "format": k,
        "rows": len(summaries),
        "bytes": os.path.getsize(path),
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
    }


def _write_xlsx(path: str, rows: list[dict], cols: list, title_bits: list[str]):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
        from openpyxl.worksheet.table import Table, TableStyleInfo
    except ImportError:
        _fail("openpyxl is required for xlsx export. Install it or use kind='csv'.")

    NAVY, WHITE, BORDER, BAND = "1E293B", "FFFFFF", "E2E8F0", "F8FAFC"
    STATUS_FILL = {
        "Completed": "DCFCE7", "Rejected": "F1F5F9", "In progress": "E0E7FF",
        "On hold": "FEF3C7", "New": "DBEAFE", "To be scheduled": "FFEDD5",
    }
    STATUS_FONT = {
        "Completed": "16A34A", "Rejected": "94A3B8", "In progress": "4338CA",
        "On hold": "B45309", "New": "1D4ED8", "To be scheduled": "C2410C",
    }
    thin = Side(style="thin", color=BORDER)
    box = Border(left=thin, right=thin, top=thin, bottom=thin)

    wb = Workbook()
    ws = wb.active
    ws.title = "Work Packages"
    ws.sheet_view.showGridLines = False

    scope = " · ".join(filter(None, title_bits)) or "all projects"
    ws.cell(row=1, column=1, value=f"OpenProject Work Packages — {scope}").font = Font(bold=True, size=15, color=NAVY)
    ws.cell(row=2, column=1, value=f"{len(rows)} work packages · generated {datetime.date.today().isoformat()} · read-only export").font = Font(italic=True, size=9, color="334155")
    ws.row_dimensions[1].height = 26

    hr = 4
    for i, (_key, label) in enumerate(cols, start=1):
        c = ws.cell(row=hr, column=i, value=label)
        c.font = Font(bold=True, size=10, color=WHITE)
        c.fill = PatternFill("solid", fgColor=NAVY)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = box
    ws.row_dimensions[hr].height = 24

    widths = [8, 52, 30, 18, 11, 13, 20, 20, 13, 13, 10, 13, 12, 13, 14, 38]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    today = datetime.date.today()
    for r, item in enumerate(rows, start=hr + 1):
        for i, (key, _label) in enumerate(cols, start=1):
            cell = ws.cell(row=r, column=i, value=item.get(key))
            cell.border = box
            cell.font = Font(size=10)
            cell.alignment = Alignment(vertical="center", wrap_text=(key == "subject"))
            if (r - hr) % 2 == 0:
                cell.fill = PatternFill("solid", fgColor=BAND)
        sc = ws.cell(row=r, column=4)
        if sc.value in STATUS_FILL:
            sc.fill = PatternFill("solid", fgColor=STATUS_FILL[sc.value])
            sc.font = Font(size=10, bold=True, color=STATUS_FONT.get(sc.value, "334155"))
        ws.cell(row=r, column=16).font = Font(size=9, color="2563EB")
        for col in (9, 10, 14, 15):
            if ws.cell(row=r, column=col).value:
                ws.cell(row=r, column=col).number_format = "yyyy-mm-dd"
        due, closed = item.get("due_date"), (item.get("status") or "").lower() in CLOSED_STATUSES
        if due and not closed:
            try:
                if datetime.date.fromisoformat(str(due)[:10]) < today:
                    for i in range(1, len(cols) + 1):
                        ws.cell(row=r, column=i).fill = PatternFill("solid", fgColor="FEE2E2")
            except ValueError:
                pass

    last = hr + len(rows)
    ref = f"A{hr}:P{last}"
    tbl = Table(displayName="WorkPackages", ref=ref)
    tbl.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=False)
    ws.add_table(tbl)
    ws.freeze_panes = f"C{hr + 1}"

    sm = wb.create_sheet("Summary")
    sm.sheet_view.showGridLines = False
    sm.cell(row=1, column=1, value="Summary").font = Font(bold=True, size=14, color=NAVY)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.get("status") or "None"] = counts.get(r.get("status") or "None", 0) + 1
    sm.cell(row=3, column=1, value="By status").font = Font(bold=True, size=11, color=NAVY)
    for i, (k, v) in enumerate(sorted(counts.items(), key=lambda kv: -kv[1]), start=4):
        sm.cell(row=i, column=1, value=k).font = Font(size=10)
        c = sm.cell(row=i, column=2, value=v)
        c.font = Font(bold=True, size=10, color="4338CA")
    p0 = 4 + len(counts) + 2
    sm.cell(row=p0, column=1, value="By project").font = Font(bold=True, size=11, color=NAVY)
    pc: dict[str, int] = {}
    for r in rows:
        pc[r.get("project") or "Unknown"] = pc.get(r.get("project") or "Unknown", 0) + 1
    for i, (k, v) in enumerate(sorted(pc.items(), key=lambda kv: -kv[1]), start=p0 + 1):
        sm.cell(row=i, column=1, value=k).font = Font(size=10)
        sm.cell(row=i, column=2, value=v).font = Font(bold=True, size=10, color="4338CA")
    sm.cell(row=p0 + 1 + len(pc) + 2, column=1, value="Hours").font = Font(bold=True, size=11, color=NAVY)
    est = sum(r.get("estimated_hours") or 0 for r in rows)
    spent = sum(r.get("spent_hours") or 0 for r in rows)
    for i, (label, val) in enumerate([("Estimated", est), ("Spent", spent), ("Remaining", round(est - spent, 2))], start=p0 + len(pc) + 4):
        sm.cell(row=i, column=1, value=label).font = Font(size=10)
        sm.cell(row=i, column=2, value=round(val, 2)).font = Font(bold=True, size=10, color="4338CA")
    sm.column_dimensions["A"].width = 38
    sm.column_dimensions["B"].width = 14

    wb.save(path)


# ================================================================ resources
@mcp.resource("openproject://projects", name="Projects", mime_type="application/json")
def projects_resource() -> str:
    """Every project visible to the API key."""
    return json.dumps({"projects": list_projects()["projects"]}, indent=2, ensure_ascii=False)


@mcp.resource("openproject://users", name="Users", mime_type="application/json")
def users_resource() -> str:
    """Every active user visible to the API key."""
    return json.dumps({"users": list_users()["users"]}, indent=2, ensure_ascii=False)


@mcp.resource("openproject://statuses", name="Statuses", mime_type="application/json")
def statuses_resource() -> str:
    """The status catalogue with per-status open work package counts."""
    statuses = _client.get_statuses()
    out = []
    for s in statuses:
        out.append({
            "id": s.get("id"),
            "name": s.get("name"),
            "is_closed": s.get("isClosed"),
            "default": s.get("isDefault"),
            "position": s.get("position"),
        })
    return json.dumps({"statuses": out}, indent=2, ensure_ascii=False)


@mcp.resource("openproject://work-packages/{project_id}", name="Project work packages", mime_type="application/json")
def project_wps_resource(project_id: str) -> str:
    """All work packages of one project, as JSON. Use openproject://projects for valid ids."""
    p = _resolve_project(project_id)
    res = get_project_tasks(p["identifier"], limit=WORK_PACKAGE_LIMIT)
    return json.dumps(res, indent=2, ensure_ascii=False, default=str)


@mcp.resource("openproject://user/{user}/report", name="User workload report", mime_type="application/json")
def user_report_resource(user: str) -> str:
    """Workload, status split, overdue items and hours for one user."""
    u = _resolve_user(user)
    uid = _require_id(u.get("id"), "user id")
    data = _client.get_full_user(uid)
    rows = [_wp_summary(w) for w in data["tasks"]]
    est = round(sum(r["estimated_hours"] or 0 for r in rows), 2)
    spent = round(sum(r["spent_hours"] or 0 for r in rows), 2)
    today = datetime.date.today()
    overdue = []
    for r in rows:
        if (r["status"] or "").lower() in CLOSED_STATUSES or not r["due_date"]:
            continue
        try:
            d = datetime.date.fromisoformat(str(r["due_date"])[:10])
        except ValueError:
            continue
        if d < today:
            overdue.append({**r, "days_overdue": (today - d).days})
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"] or "None"] = counts.get(r["status"] or "None", 0) + 1
    return json.dumps({
        "user": {"id": uid, "name": u.get("name"), "login": u.get("login"), "email": u.get("mail")},
        "total_work_packages": len(rows),
        "count_by_status": counts,
        "estimated_hours": est,
        "spent_hours": spent,
        "remaining_hours": round(est - spent, 2),
        "projects": data["projects"],
        "overdue_count": len(overdue),
        "overdue": sorted(overdue, key=lambda r: -r["days_overdue"]),
        "work_packages": rows,
    }, indent=2, ensure_ascii=False, default=str)


@mcp.resource("openproject://config", name="Server config", mime_type="application/json")
def config_resource() -> str:
    """Non-secret configuration: which OpenProject instance is connected and cache size."""
    return json.dumps({
        "openproject_url": OPENPROJECT_URL,
        "api_key_configured": bool(API_KEY),
        "mode": "read-only",
        "work_package_limit": WORK_PACKAGE_LIMIT,
        "cache_size": len(cache._store),
    }, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")