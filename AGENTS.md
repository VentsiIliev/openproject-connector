# OpenProject Connector - Agent Handoff Guide

## Quick Start
```bash
cd /home/ilv/openproject-connector/src
source ../.venv/bin/activate
python app.py
```
- **Port:** 5001 (not 5000)
- **Kill server:** `kill -9 $(lsof -t -i:5001)`
- User frequently presses `Ctrl+Z` (suspend) instead of `Ctrl+C` — always run `kill -9` before restarting

## Architecture

### File Structure
```
src/
├── app.py                      # Flask app, ALL UI templates (inline Jinja2), routes, JS/CSS
├── project_client.py           # Business logic: API calls, caching, parallel fetching
├── openproject_connector.py    # HTTP client: session, retries, connection pooling
├── cache.py                    # Thread-safe TTL in-memory cache
├── config.py                   # Env loader (OPENPROJECT_URL, API_KEY, APP_PORT)
├── main.py                     # Alternate entrypoint (use app.py directly)
└── mcp_server.py               # Read-only MCP server (stdio) for AI agents
```

### Key Patterns
- **Single-file app:** All HTML/CSS/JS is inline in `app.py` via `render_template_string`
- **Two modes:** `mode=projects` (default) and `mode=users` via URL param
- **No separate templates:** All UI defined in `TEMPLATE`, `SETUP_TEMPLATE`, `ERROR_TEMPLATE` constants
- **Vanilla frontend:** No frameworks. Chart.js CDN + Frappe Gantt CDN
- **Threaded fetching:** `get_full_project()` and `get_full_user()` use `threading` for parallel API calls

## Routes
| Route | Method | Purpose |
|-------|--------|---------|
| `/` | GET | Main dashboard (projects mode or users mode) |
| `/clear-cache` | GET | Flush all cache and reset client |
| `/api/work-packages/<id>/status` | PATCH | Update WP status (drag-and-drop) |

## OpenProject API Integration
- **Auth:** HTTP Basic Auth with username `apikey` and the API key as password
- **Base URL:** `{OPENPROJECT_URL}/api/v3`
- **Pagination:** Uses `nextByOffset` link for offset-based pagination
- **Work package limit:** 500 per project/user query
- **Workflow transitions:** Read from `_links.status.allowedValues` on WP response

### Important: OpenProject Quirks
- **`/users` endpoint does NOT support `sortBy` param** — returns 500 error
- **`only=` sparse fieldset param may not be supported** — causes 500 errors
- **`/work_packages/{id}/form` endpoint does NOT exist** — use WP response `_links` instead
- Status transitions must use full `href` values, not IDs
- **`/projects/{id}/work_packages` returns TOP-LEVEL work packages only** — children are omitted. For a complete set use the `project` filter on `/work_packages` instead (`mcp_server._all_project_work_packages`)
- **Filters require numeric ids** — `status`, `type` and `priority` reject display names with HTTP 400. Resolve name → id first (see `_resolve_status` / `_resolve_type` / `_resolve_priority` in `mcp_server.py`)
- **`/work_packages/{id}/parent`, `/children` and `/time_entries` return 404** — read `_links.parent` (href is null when there is no parent), `_links.children` (array of links), `_embedded.relations`, and filter `/time_entries` by `work_package_id`
- **The `parent` filter with `operator: "!"` is rejected** with HTTP 400 — filter root work packages client-side instead

## Cache Strategy
| Key Pattern | TTL | Content |
|-------------|-----|---------|
| `projects_list` | 600s | All projects |
| `project_{id}` | 600s | Single project |
| `statuses_global` | 3600s | All statuses |
| `members_{id}` | 300s | Project members |
| `tasks_{pid}_{aid}` | 120s | Work packages for project |
| `user_tasks_{uid}` | 120s | Work packages for user |
| `all_users` | 600s | All OpenProject users |

Cache is thread-safe with `threading.Lock`. Max size: 256 entries.

## Features Implemented

### Projects Mode (`mode=projects`)
- Sidebar with project list + search
- Project dashboard with stats cards (total tasks, team members, status, view)
- **Kanban view:** Drag-and-drop columns by status, cards show type/priority/dates/progress
- **Gantt view:** Frappe Gantt with mode switching (Quarter/Half Day/Day/Week/Month)
- **Team view:** Grid of project members (only those with assigned tasks), click for personal Kanban+Gantt
- Advanced filters: Status, Priority, Type, Date range, Sort, Assignee, Members
- Analytics charts: Status donut, Priority bar, Workload bar
- Overdue task detection with alerts
- Task detail modal on card click

### Users Mode (`mode=users`)
- Sidebar with user list + search (debounced 400ms)
- User grid view with name, login, email, admin badge
- **Filters:** Role (All/Admin/Members), Sort (Name A-Z/Z-A/Login)
- **User detail dashboard:**
  - Stats: Total tasks, project count, user info
  - Kanban tab: All tasks across all projects
  - Gantt tab: Timeline view
  - Projects tab: List of projects user participates in (clickable)

### Drag-and-Drop
- Cards draggable by drag handle (6-dot icon)
- Backend validates allowed transitions via `GET /work_packages/{id}` → `_links.status.allowedValues`
- PATCH payload: `{"_links": {"status": {"href": "..."}}}` 
- Toast feedback: "Updating..." → "Moved to X" or "Workflow blocked: ..."
- Auto-reload on success, restore card on failure

## UI Conventions
- **CSS variables:** `--accent` (#6366f1), `--bg`, `--content-bg`, `--card-bg`, `--text-primary`, `--line`, etc.
- **Dark/light toggle:** `document.documentElement.getAttribute('data-theme')`
- **Sidebar:** Collapsible, Projects/Users mode toggle buttons
- **Filter auto-submit:** `autoSubmitFilters()` for selects, `autoSubmitFiltersDebounce()` for text inputs
- **View switching:** `switchView('kanban'|'gantt'|'team'|'projects')` updates URL + active tab
- **Modals:** `openTaskModal(card)` / `closeTaskModal()`

## Performance
- Connection pooling: `HTTPAdapter(pool_connections=10, pool_maxsize=20)`
- Gzip compression enabled in headers
- Timeout: 30s per request
- Parallel fetching via `threading.Thread` (not asyncio)
- Cache invalidation on status updates (`tasks_*` keys)

## Testing
No test framework configured. Verify with:
```bash
python3 -m py_compile src/app.py
python3 -m py_compile src/project_client.py
python3 -m py_compile src/openproject_connector.py
```

## Dependencies
```
Flask>=3.0,<4.0
requests>=2.31,<3.0
python-dotenv>=1.0,<2.0
mcp>=2.0,<3.0
openpyxl>=3.1,<4.0
```

## MCP Server (`src/mcp_server.py`)
Read-only OpenProject access for AI agents over stdio. Registered in
`.opencode/opencode.json`, so opencode starts it automatically.

```bash
python src/mcp_server.py     # stdio transport
```

**14 tools:** `ping`, `list_projects`, `list_users`, `get_project`, `get_user`,
`list_work_packages`, `get_work_package`, `get_project_tasks`, `get_relations`,
`get_hierarchy`, `get_time_entries`, `get_time_summary`, `get_overdue`,
`build_report`

**6 resources:** `openproject://projects`, `openproject://users`,
`openproject://statuses`, `openproject://config`,
`openproject://work-packages/{project_id}`, `openproject://user/{user}/report`

Notes:
- **No write tools.** Nothing in the server calls `patch`/`post`/`delete`.
- Every tool resolves human input to ids — projects by id/identifier/name, users by
  id/login/name, and status/type/priority by name → numeric id before filtering.
- Failures raise `ToolError` so the agent sees a readable message instead of a crash.
- `build_report` writes CSV or a styled XLSX (table styling, status colours, overdue
  shading, Summary sheet) and returns the absolute path.
- Reuses the same `ProjectClient` and TTL cache as the Flask app.

## Common Pitfalls
1. **Port 5001 not 5000** — port 5000 is occupied by another app
2. **Ctrl+Z vs Ctrl+C** — user suspends instead of terminating; always `kill -9` first
3. **No `sortBy` on `/users`** — OpenProject returns 500
4. **No `only=` param** — causes 500 on some endpoints
5. **Status transitions need full href** — can't construct from ID alone
6. **Team shows only active members** — derived from tasks, not memberships API
7. **Large template file** — `app.py` is ~3600 lines; search by `TEMPLATE =` to find UI sections
8. **No `.gitignore` for `.venv/`** — but `.venv` exists at project root
9. **MCP SDK is v2** — `FastMCP` was renamed to `MCPServer` (`from mcp.server.mcpserver import MCPServer`). Pinning `mcp<2` restores the v1 import path
10. **MCP client field names are snake_case** — `server_info`, `input_schema`, `is_error`, `uri_template`, not the camelCase of the JSON wire format

## URL Params Reference
| Param | Mode | Purpose |
|-------|------|---------|
| `mode` | both | `projects` or `users` |
| `project_id` | projects | Selected project |
| `user_id` | users | Selected user detail view |
| `view` | both | `kanban`, `gantt`, `team`, `projects` |
| `search` | both | Work package text search |
| `project_search` | projects | Sidebar project filter |
| `user_search` | users | Sidebar user filter |
| `status` | both | Status filter (multi-value) |
| `priority` | both | Priority filter |
| `type` | both | Type filter |
| `date_from` / `date_to` | both | Date range filter |
| `sort` | both | Sort order |
| `assignee_id` | projects | Assignee dropdown filter |
| `member` | projects | Member checkboxes (multi-value) |
| `member_name` | projects | Selected member detail |
| `role_filter` | users | `admin` or `member` |
| `user_sort` | users | `name`, `name_desc`, `login` |
