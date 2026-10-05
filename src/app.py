from datetime import datetime

from flask import Flask, render_template_string, request

from config import API_KEY, APP_HOST, APP_PORT, OPENPROJECT_URL
from openproject_connector import OpenProjectConnector
from project_client import ProjectClient

app = Flask(__name__)
_connector = None
_client = None


def get_client():
    global _connector, _client
    if _connector is None:
        _connector = OpenProjectConnector()
        _client = ProjectClient(_connector)
    return _client


def link_title(resource: dict, name: str, fallback: str) -> str:
    links = resource.get("_links", {})
    embedded = resource.get("_embedded", {})

    link_value = links.get(name, {})
    if isinstance(link_value, dict) and link_value.get("title"):
        return link_value["title"]

    embedded_value = embedded.get(name, {})
    if isinstance(embedded_value, dict):
        if embedded_value.get("name"):
            return embedded_value["name"]
        if embedded_value.get("subject"):
            return embedded_value["subject"]

    return fallback


def link_href(resource: dict, name: str) -> str | None:
    links = resource.get("_links", {})
    value = links.get(name, {})
    if isinstance(value, dict):
        return value.get("href")
    return None


def extract_id_from_href(href: str | None) -> int | None:
    if not href:
        return None

    candidate = href.rstrip("/").split("/")[-1]
    try:
        return int(candidate)
    except (TypeError, ValueError):
        return None


def format_date(value: str | None) -> str | None:
    if not value:
        return None

    for pattern in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, pattern).strftime("%b %d, %Y")
        except ValueError:
            continue

    return value


def parse_date_raw(value: str | None) -> datetime | None:
    if not value:
        return None
    for pattern in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            continue
    return None


def initials(value: str | None) -> str:
    if not value:
        return "?"

    parts = [part for part in value.replace("-", " ").split() if part]
    if not parts:
        return "?"

    if len(parts) == 1:
        return parts[0][:2].upper()

    return (parts[0][0] + parts[-1][0]).upper()


def accent_from_priority(priority: str) -> str:
    priority_key = (priority or "").lower()
    if "immediate" in priority_key or "urgent" in priority_key or "high" in priority_key:
        return "#ff5b2e"
    if "low" in priority_key:
        return "#2d9cdb"
    if "normal" in priority_key or "standard" in priority_key:
        return "#7c4dff"
    return "#7c4dff"


def build_project_summary(project: dict) -> dict:
    links = project.get("_links", {})
    status = link_title(project, "status", "Active")
    description = project.get("description", {}).get("raw") or "No description provided."
    active = project.get("active")

    return {
        "id": project.get("id"),
        "name": project.get("name", "Untitled project"),
        "identifier": project.get("identifier"),
        "status": status,
        "description": description.strip(),
        "active": active if active is not None else True,
        "updated_at": format_date(project.get("updatedAt")),
        "href": links.get("self", {}).get("href"),
    }


def build_task_card(task: dict, base_url: str) -> tuple[str, dict]:
    status = link_title(task, "status", "Unsorted")
    done = task.get("percentageDone") or 0

    card = {
        "id": task.get("id"),
        "title": task.get("subject", "Untitled work package"),
        "status": status,
        "assignee": link_title(task, "assignee", "Unassigned"),
        "assignee_initials": initials(link_title(task, "assignee", "Unassigned")),
        "priority": link_title(task, "priority", "Standard"),
        "type": link_title(task, "type", "Task"),
        "author": link_title(task, "author", "Unknown"),
        "start_date": format_date(task.get("startDate")),
        "due_date": format_date(task.get("dueDate")),
        "start_date_raw": task.get("startDate"),
        "due_date_raw": task.get("dueDate"),
        "updated_at": format_date(task.get("updatedAt")),
        "created_at": format_date(task.get("createdAt")),
        "done": max(0, min(100, done)),
        "estimated_time": task.get("estimatedTime"),
        "remaining_time": task.get("remainingTime"),
        "accent": accent_from_priority(link_title(task, "priority", "Standard")),
        "href": f"{base_url}/work_packages/{task.get('id')}",
        "is_overdue": False,
    }

    due = parse_date_raw(task.get("dueDate"))
    if due and due.date() < datetime.now().date() and done < 100:
        card["is_overdue"] = True

    return status, card


def build_status_columns(statuses: list[dict]) -> list[dict]:
    ordered = sorted(
        statuses,
        key=lambda item: (item.get("position", 0), item.get("name", "").lower()),
    )
    return [
        {
            "id": status.get("id"),
            "name": status.get("name", "Unsorted"),
            "href": link_href(status, "self"),
            "color": status.get("color") or "#162331",
            "is_closed": status.get("isClosed", False),
        }
        for status in ordered
    ]


def extract_filter_options(tasks: list[dict]) -> dict:
    statuses = set()
    priorities = set()
    types = set()

    for task in tasks:
        statuses.add(link_title(task, "status", "Unsorted"))
        priorities.add(link_title(task, "priority", "Standard"))
        types.add(link_title(task, "type", "Task"))

    return {
        "statuses": sorted(statuses, key=lambda x: x.lower()),
        "priorities": sorted(priorities, key=lambda x: x.lower()),
        "types": sorted(types, key=lambda x: x.lower()),
    }


def apply_task_filters(cards: list[dict], status_filter: list[str] | None, priority_filter: str, type_filter: str, date_from: str, date_to: str, sort_by: str, member_filter: list[str] | None) -> list[dict]:
    filtered = cards

    if status_filter:
        filtered = [c for c in filtered if c["status"] in status_filter]

    if priority_filter:
        filtered = [c for c in filtered if c["priority"].lower() == priority_filter.lower()]

    if type_filter:
        filtered = [c for c in filtered if c["type"].lower() == type_filter.lower()]

    if member_filter:
        filtered = [c for c in filtered if c["assignee"] in member_filter]

    if date_from:
        try:
            from_dt = datetime.strptime(date_from, "%Y-%m-%d")
            filtered = [c for c in filtered if c["start_date_raw"] and parse_date_raw(c["start_date_raw"]) and parse_date_raw(c["start_date_raw"]) >= from_dt]
        except ValueError:
            pass

    if date_to:
        try:
            to_dt = datetime.strptime(date_to, "%Y-%m-%d")
            filtered = [c for c in filtered if c["due_date_raw"] and parse_date_raw(c["due_date_raw"]) and parse_date_raw(c["due_date_raw"]) <= to_dt]
        except ValueError:
            pass

    if sort_by == "priority":
        priority_order = {"immediate": 0, "urgent": 1, "high": 2, "normal": 3, "standard": 3, "low": 4}
        filtered.sort(key=lambda c: priority_order.get(c["priority"].lower(), 5))
    elif sort_by == "due_date":
        filtered.sort(key=lambda c: c["due_date_raw"] or "9999-99-99")
    elif sort_by == "start_date":
        filtered.sort(key=lambda c: c["start_date_raw"] or "9999-99-99")
    elif sort_by == "progress":
        filtered.sort(key=lambda c: c["done"], reverse=True)
    elif sort_by == "assignee":
        filtered.sort(key=lambda c: c["assignee"].lower())
    elif sort_by == "title":
        filtered.sort(key=lambda c: c["title"].lower())

    return filtered


def build_kanban(
    tasks: list[dict],
    base_url: str,
    status_columns: list[dict],
    search: str = "",
    status_filter: list[str] | None = None,
    priority_filter: str = "",
    type_filter: str = "",
    date_from: str = "",
    date_to: str = "",
    sort_by: str = "",
    member_filter: list[str] | None = None,
) -> dict[str, list[dict]]:
    board: dict[str, list[dict]] = {status["name"]: [] for status in status_columns}
    needle = search.lower()

    all_cards = []
    for task in tasks:
        title = task.get("subject", "")
        if needle and needle not in title.lower():
            continue

        status, card = build_task_card(task, base_url)
        all_cards.append((status, card))

    flat_cards = [card for _, card in all_cards]
    filtered_cards = apply_task_filters(flat_cards, status_filter, priority_filter, type_filter, date_from, date_to, sort_by, member_filter)
    filtered_map = {id(c): c for c in filtered_cards}

    for status, card in all_cards:
        if id(card) in filtered_map:
            board.setdefault(status, []).append(card)

    return board


def build_gantt_data(tasks: list[dict], base_url: str, member_filter: list[str] | None = None) -> list[dict]:
    gantt_tasks = []
    for task in tasks:
        start = task.get("startDate")
        end = task.get("dueDate")
        if not start or not end:
            continue

        assignee = link_title(task, "assignee", "Unassigned")
        if member_filter and assignee not in member_filter:
            continue

        status = link_title(task, "status", "Unsorted")
        priority = link_title(task, "priority", "Standard")
        task_type = link_title(task, "type", "Task")
        done = task.get("percentageDone") or 0

        custom_class = "gantt-low"
        pk = priority.lower()
        if "immediate" in pk or "urgent" in pk or "high" in pk:
            custom_class = "gantt-high"
        elif "normal" in pk or "standard" in pk:
            custom_class = "gantt-normal"

        gantt_tasks.append({
            "id": str(task.get("id", "")),
            "name": f"#{task.get('id')} {task.get('subject', 'Untitled')[:50]}",
            "start": start[:10],
            "end": end[:10],
            "progress": max(0, min(100, done)),
            "custom_class": custom_class,
            "_href": f"{base_url}/work_packages/{task.get('id')}",
            "_status": status,
            "_priority": priority,
            "_type": task_type,
            "_assignee": assignee,
        })

    gantt_tasks.sort(key=lambda t: t["start"])
    return gantt_tasks


def build_member_rollup(members: list[dict]) -> list[dict]:
    rollup = []

    for member in members:
        principal = member.get("_links", {}).get("principal", {})
        roles = member.get("_embedded", {}).get("roles", [])
        rollup.append(
            {
                "name": principal.get("title", "Unknown"),
                "roles": ", ".join(role.get("name", "") for role in roles if role.get("name")) or "Member",
                "kind": principal.get("type", ""),
                "principal_id": extract_id_from_href(principal.get("href")),
            }
        )

    return sorted(rollup, key=lambda item: item["name"].lower())


def paginate_items(items: list[dict], page: int, page_size: int) -> dict:
    total = len(items)
    if total == 0:
        return {
            "entries": [],
            "page": 1,
            "page_size": page_size,
            "total": 0,
            "total_pages": 1,
            "has_prev": False,
            "has_next": False,
        }

    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    end = start + page_size

    return {
        "entries": items[start:end],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
    }


def build_dashboard(
    client: ProjectClient,
    selected_project_id: int | None,
    search: str,
    assignee_filter_id: int | None = None,
    status_filter: list[str] | None = None,
    priority_filter: str = "",
    type_filter: str = "",
    date_from: str = "",
    date_to: str = "",
    sort_by: str = "",
    member_filter: list[str] | None = None,
) -> dict:
    raw_projects = client.get_all_projects()
    projects = [build_project_summary(project) for project in raw_projects]
    projects.sort(key=lambda item: item["name"].lower())

    selected_project = None
    board: dict[str, list[dict]] = {}
    gantt: list[dict] = []
    members: list[dict] = []
    total_tasks = 0
    status_columns: list[dict] = []
    filter_options: dict = {"statuses": [], "priorities": [], "types": []}
    chart_data: dict = {"status": [], "priority": [], "workload": []}
    overdue_tasks: list[dict] = []

    if projects:
        valid_ids = {project["id"] for project in projects if project["id"] is not None}
        if selected_project_id not in valid_ids:
            selected_project_id = projects[0]["id"]

        if selected_project_id is not None:
            full_project = client.get_full_project(selected_project_id, assignee_id=assignee_filter_id)
            selected_project = build_project_summary(full_project["project"])
            members = build_member_rollup(full_project["members"])
            status_columns = build_status_columns(full_project["statuses"])
            board = build_kanban(
                full_project["tasks"],
                client.connector.base_url,
                status_columns,
                search,
                status_filter,
                priority_filter,
                type_filter,
                date_from,
                date_to,
                sort_by,
                member_filter,
            )
            gantt = build_gantt_data(full_project["tasks"], client.connector.base_url, member_filter)
            total_tasks = sum(len(cards) for cards in board.values())
            filter_options = extract_filter_options(full_project["tasks"])

            status_counts: dict[str, int] = {}
            priority_counts: dict[str, int] = {}
            workload_counts: dict[str, int] = {}
            today = datetime.now().date()

            project_members_map = {}
            for task in full_project["tasks"]:
                assignee_name = link_title(task, "assignee", None)
                if assignee_name and assignee_name != "Unassigned":
                    project_members_map[assignee_name] = project_members_map.get(assignee_name, 0) + 1

            team_members = []
            for m in members:
                if m["name"] in project_members_map:
                    team_members.append({**m, "task_count": project_members_map[m["name"]]})
            team_members.sort(key=lambda x: x["name"].lower())

            for task in full_project["tasks"]:
                status = link_title(task, "status", "Unsorted")
                priority = link_title(task, "priority", "Standard")
                assignee = link_title(task, "assignee", "Unassigned")
                status_counts[status] = status_counts.get(status, 0) + 1
                priority_counts[priority] = priority_counts.get(priority, 0) + 1
                workload_counts[assignee] = workload_counts.get(assignee, 0) + 1

                due = parse_date_raw(task.get("dueDate"))
                if due and due.date() < today and not status_columns:
                    pass
                is_closed = False
                for sc in status_columns:
                    if sc["name"] == status:
                        is_closed = sc.get("is_closed", False)
                        break
                if due and due.date() < today and not is_closed:
                    overdue_tasks.append({
                        "id": task.get("id"),
                        "title": task.get("subject", "Untitled"),
                        "assignee": assignee,
                        "due_date": format_date(task.get("dueDate")),
                        "due_date_raw": task.get("dueDate"),
                        "status": status,
                        "priority": priority,
                        "href": f"{client.connector.base_url}/work_packages/{task.get('id')}",
                    })

            chart_colors = {
                "In Progress": "#6366f1",
                "New": "#06b6d4",
                "Done": "#10b981",
                "Closed": "#94a3b8",
                "In Review": "#f59e0b",
                "Blocked": "#ef4444",
                "Feedback": "#8b5cf6",
            }
            default_colors = ["#6366f1", "#06b6d4", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899", "#14b8a6"]

            chart_data["status"] = [{"label": k, "count": v, "color": chart_colors.get(k, default_colors[i % len(default_colors)])} for i, (k, v) in enumerate(status_counts.items())]
            chart_data["priority"] = [{"label": k, "count": v} for k, v in priority_counts.items()]
            chart_data["workload"] = [{"label": k, "count": v} for k, v in sorted(workload_counts.items(), key=lambda x: x[1], reverse=True)[:10]]
            overdue_tasks.sort(key=lambda t: t["due_date_raw"] or "9999-99-99")

    return {
        "projects": projects,
        "selected_project_id": selected_project_id,
        "selected_project": selected_project,
        "board": board,
        "gantt": gantt,
        "status_columns": status_columns,
        "members": members,
        "team_members": team_members,
        "total_tasks": total_tasks,
        "filter_options": filter_options,
        "chart_data": chart_data,
        "overdue_tasks": overdue_tasks,
    }


def render_user_dashboard(client, mode, search, user_search, selected_user_id, status_filter, priority_filter, type_filter, date_from, date_to, sort_by, view, project_search, role_filter, user_sort):
    try:
        all_users = client.get_all_users()
    except Exception as exc:
        return render_template_string(ERROR_TEMPLATE, error=str(exc), base_url=OPENPROJECT_URL)

    user_list = []
    for u in all_users:
        name = u.get("name", u.get("login", "Unknown"))
        if user_search and user_search.lower() not in name.lower():
            continue
        is_admin = u.get("admin", False)
        if role_filter == "admin" and not is_admin:
            continue
        if role_filter == "member" and is_admin:
            continue
        user_list.append({
            "id": u.get("id"),
            "name": name,
            "login": u.get("login", ""),
            "email": u.get("mail", ""),
            "admin": is_admin,
            "avatar": u.get("avatarUrl", ""),
            "status": u.get("status", "active"),
        })

    if user_sort == "name_desc":
        user_list.sort(key=lambda u: u["name"].lower(), reverse=True)
    elif user_sort == "login":
        user_list.sort(key=lambda u: u["login"].lower())
    else:
        user_list.sort(key=lambda u: u["name"].lower())

    if selected_user_id:
        user_data = client.get_full_user(selected_user_id)
        user_info = user_data.get("user", {})
        tasks = user_data.get("tasks", [])
        projects = user_data.get("projects", [])

        statuses = client.get_statuses()
        status_cols = build_status_columns(statuses)

        user_board = build_kanban(
            tasks,
            client.connector.base_url,
            status_cols,
            search,
            status_filter,
            priority_filter,
            type_filter,
            date_from,
            date_to,
            sort_by,
        )
        user_gantt = build_gantt_data(tasks, client.connector.base_url)
        user_total = sum(len(cards) for cards in user_board.values())

        status_counts = {}
        priority_counts = {}
        project_counts = {}
        for task in tasks:
            st = link_title(task, "status", "Unsorted")
            pr = link_title(task, "priority", "Standard")
            status_counts[st] = status_counts.get(st, 0) + 1
            priority_counts[pr] = priority_counts.get(pr, 0) + 1

        for p in projects:
            pname = p.get("name", "Unknown")
            project_counts[pname] = project_counts.get(pname, 0) + 1

        return render_template_string(
            TEMPLATE,
            mode=mode,
            user_list=user_list,
            selected_user_id=selected_user_id,
            user_name=user_info.get("name", ""),
            user_login=user_info.get("login", ""),
            user_email=user_info.get("mail", ""),
            user_admin=user_info.get("admin", False),
            user_board=user_board,
            user_gantt=user_gantt,
            user_total=user_total,
            user_projects=projects,
            user_status_counts=status_counts,
            user_priority_counts=priority_counts,
            user_status_columns=status_cols,
            search=search,
            project_search=project_search,
            user_search=user_search,
            status_filter=status_filter,
            priority_filter=priority_filter,
            type_filter=type_filter,
            date_from=date_from,
            date_to=date_to,
            sort_by=sort_by,
            view=view,
            openproject_url=OPENPROJECT_URL,
            projects=[],
            all_projects_count=0,
            selected_project_id=None,
            project=None,
            board={},
            gantt=[],
            status_columns=[],
            members=[],
            team_members=[],
            total_tasks=0,
            filter_options={"statuses": [], "priorities": [], "types": []},
            chart_data={"status": [], "priority": [], "workload": []},
            overdue_tasks=[],
            active_filters_count=0,
            selected_member_name="",
            member_board={},
            member_gantt=[],
            member_total=0,
            workload_map={},
        )

    return render_template_string(
        TEMPLATE,
        mode=mode,
        user_list=user_list,
        selected_user_id=None,
        search=search,
        project_search=project_search,
        user_search=user_search,
        openproject_url=OPENPROJECT_URL,
        projects=[],
        all_projects_count=0,
        selected_project_id=None,
        project=None,
        board={},
        gantt=[],
        status_columns=[],
        members=[],
        team_members=[],
        total_tasks=0,
        filter_options={"statuses": [], "priorities": [], "types": []},
        chart_data={"status": [], "priority": [], "workload": []},
        overdue_tasks=[],
        view=view,
        active_filters_count=0,
        selected_member_name="",
        member_board={},
        member_gantt=[],
        member_total=0,
        workload_map={},
        status_filter=[],
        priority_filter="",
        type_filter="",
        date_from="",
        date_to="",
        sort_by="",
    )


@app.route("/")
def dashboard():
    mode = request.args.get("mode", "projects").strip()
    if mode not in ("projects", "users"):
        mode = "projects"

    search = request.args.get("search", "").strip()
    project_search = request.args.get("project_search", "").strip()
    user_search = request.args.get("user_search", "").strip()
    assignee_filter_id = request.args.get("assignee_id", type=int)
    project_id = request.args.get("project_id", type=int)
    selected_user_id = request.args.get("user_id", type=int)
    status_filter = request.args.getlist("status")
    priority_filter = request.args.get("priority", "").strip()
    type_filter = request.args.get("type", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    sort_by = request.args.get("sort", "").strip()
    view = request.args.get("view", "kanban").strip()
    member_filter = request.args.getlist("member")
    selected_member_name = request.args.get("member_name", "").strip()

    if not OPENPROJECT_URL or not API_KEY:
        return render_template_string(
            SETUP_TEMPLATE,
            openproject_url=OPENPROJECT_URL or "Not configured",
            api_key_present=bool(API_KEY),
        )

    role_filter = request.args.get("role_filter", "").strip()
    user_sort = request.args.get("user_sort", "name").strip()

    client = get_client()

    if mode == "users":
        return render_user_dashboard(client, mode, search, user_search, selected_user_id, status_filter, priority_filter, type_filter, date_from, date_to, sort_by, view, project_search, role_filter, user_sort)

    try:
        data = build_dashboard(client, project_id, search, assignee_filter_id, status_filter, priority_filter, type_filter, date_from, date_to, sort_by, member_filter if member_filter else None)
    except Exception as exc:
        return render_template_string(ERROR_TEMPLATE, error=str(exc), base_url=OPENPROJECT_URL)

    member_board = {}
    member_gantt = []
    member_total = 0
    if selected_member_name and data["status_columns"]:
        member_tasks = [t for t in data.get("board", {}) for _ in []]
        all_tasks_for_member = []
        raw_tasks = client.get_work_packages(project_id)
        for task in raw_tasks:
            if link_title(task, "assignee", "Unassigned") == selected_member_name:
                all_tasks_for_member.append(task)
        member_board = build_kanban(
            all_tasks_for_member,
            client.connector.base_url,
            data["status_columns"],
            search,
            status_filter,
            priority_filter,
            type_filter,
            date_from,
            date_to,
            sort_by,
        )
        member_gantt = build_gantt_data(all_tasks_for_member, client.connector.base_url)
        member_total = sum(len(cards) for cards in member_board.values())

    workload_map = {}
    for wd in data.get("chart_data", {}).get("workload", []):
        workload_map[wd["label"]] = wd["count"]

    filtered_projects = data["projects"]
    if project_search:
        needle = project_search.lower()
        filtered_projects = [
            project
            for project in data["projects"]
            if needle in project["name"].lower()
            or needle in (project["identifier"] or "").lower()
        ]

    assignee_filter = ""
    if assignee_filter_id is not None:
        selected_member = next(
            (member for member in data["members"] if member["principal_id"] == assignee_filter_id),
            None,
        )
        if selected_member:
            assignee_filter = selected_member["name"]

    active_filters_count = 0
    if status_filter:
        active_filters_count += len(status_filter)
    if priority_filter:
        active_filters_count += 1
    if type_filter:
        active_filters_count += 1
    if date_from:
        active_filters_count += 1
    if date_to:
        active_filters_count += 1
    if assignee_filter_id:
        active_filters_count += 1
    if member_filter:
        active_filters_count += len(member_filter)

    return render_template_string(
        TEMPLATE,
        search=search,
        project_search=project_search,
        assignee_filter=assignee_filter,
        assignee_filter_id=assignee_filter_id,
        projects=filtered_projects,
        all_projects_count=len(data["projects"]),
        selected_project_id=data["selected_project_id"],
        project=data["selected_project"],
        board=data["board"],
        gantt=data["gantt"],
        status_columns=data["status_columns"],
        members=data["members"],
        team_members=data["team_members"],
        total_tasks=data["total_tasks"],
        openproject_url=OPENPROJECT_URL,
        filter_options=data["filter_options"],
        status_filter=status_filter,
        priority_filter=priority_filter,
        type_filter=type_filter,
        date_from=date_from,
        date_to=date_to,
        member_filter=member_filter,
        sort_by=sort_by,
        chart_data=data["chart_data"],
        overdue_tasks=data["overdue_tasks"],
        view=view,
        active_filters_count=active_filters_count,
        selected_member_name=selected_member_name,
        member_board=member_board,
        member_gantt=member_gantt,
        member_total=member_total,
        workload_map=workload_map,
    )


@app.route("/clear-cache")
def clear_cache():
    from cache import cache as app_cache
    app_cache.clear()
    global _connector, _client
    _connector = None
    _client = None
    return {"status": "cache cleared"}


@app.route("/api/work-packages/<int:wp_id>/status", methods=["PATCH"])
def update_work_package_status(wp_id):
    import json
    data = request.get_json(silent=True)
    if not data or "status_href" not in data:
        return {"error": "status_href required"}, 400

    client = get_client()
    try:
        transitions = client.get_available_status_transitions(wp_id)
        allowed_hrefs = {t["href"] for t in transitions}
        allowed_names = {t["href"]: t["name"] for t in transitions}
        target_href = data["status_href"]

        if target_href not in allowed_hrefs:
            names = list(allowed_names.values())
            return {
                "error": f"Cannot transition. Allowed: {', '.join(names)}",
                "allowed": names,
            }, 403

        result = client.update_work_package_status(wp_id, target_href)

        for key in list(cache._store.keys()):
            if key.startswith("tasks_"):
                cache.invalidate(key)

        return {"success": True, "work_package": result}
    except Exception as e:
        return {"error": str(e)}, 500


SETUP_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>OpenProject Dashboard Setup</title>
    <style>
        :root {
            --bg: #f5f1e8;
            --panel: rgba(255, 255, 255, 0.78);
            --ink: #13212f;
            --muted: #566270;
            --line: rgba(19, 33, 47, 0.14);
            --accent: #c96b3b;
            --accent-soft: rgba(201, 107, 59, 0.14);
            --shadow: 0 24px 60px rgba(19, 33, 47, 0.12);
        }

        * { box-sizing: border-box; }

        body {
            margin: 0;
            min-height: 100vh;
            font-family: "Segoe UI", Arial, sans-serif;
            color: var(--ink);
            background:
                radial-gradient(circle at top left, rgba(201, 107, 59, 0.18), transparent 30%),
                linear-gradient(180deg, #f8f4ec 0%, #efe7da 100%);
            display: grid;
            place-items: center;
            padding: 24px;
        }

        .card {
            width: min(720px, 100%);
            background: var(--panel);
            backdrop-filter: blur(18px);
            border: 1px solid var(--line);
            border-radius: 28px;
            box-shadow: var(--shadow);
            padding: 32px;
        }

        h1 { margin: 0 0 12px; font-size: 2rem; }
        p { color: var(--muted); line-height: 1.6; }
        code {
            background: var(--accent-soft);
            padding: 2px 8px;
            border-radius: 999px;
            color: var(--accent);
        }
        ul { padding-left: 20px; color: var(--muted); }
    </style>
</head>
<body>
    <section class="card">
        <h1>OpenProject configuration required</h1>
        <p>This dashboard expects <code>OPENPROJECT_URL</code> and <code>OPENPROJECT_API_KEY</code> in the environment before it can query the API.</p>
        <ul>
            <li>Detected URL: {{ openproject_url }}</li>
            <li>API key present: {{ "yes" if api_key_present else "no" }}</li>
        </ul>
        <p>After setting them, restart the Flask app and this page will turn into the project dashboard.</p>
    </section>
</body>
</html>
"""


ERROR_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>OpenProject Dashboard Error</title>
    <style>
        body {
            margin: 0;
            min-height: 100vh;
            display: grid;
            place-items: center;
            padding: 24px;
            font-family: "Segoe UI", Arial, sans-serif;
            background: #f5f1e8;
            color: #13212f;
        }
        .card {
            width: min(720px, 100%);
            border-radius: 24px;
            padding: 28px;
            background: #fffdf9;
            border: 1px solid rgba(19, 33, 47, 0.14);
            box-shadow: 0 18px 48px rgba(19, 33, 47, 0.12);
        }
        code {
            white-space: pre-wrap;
            display: block;
            margin-top: 14px;
            padding: 14px;
            border-radius: 16px;
            background: #f3ece1;
        }
    </style>
</head>
<body>
    <section class="card">
        <h1>Unable to load OpenProject data</h1>
        <p>The dashboard could not fetch data from {{ base_url }}.</p>
        <code>{{ error }}</code>
    </section>
</body>
</html>
"""


TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>OpenProject Portfolio Dashboard</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/frappe-gantt/0.6.1/frappe-gantt.min.css">
    <style>
        :root {
            --sidebar-bg: #0f172a;
            --sidebar-bg-gradient: linear-gradient(180deg, #0f172a 0%, #1e293b 100%);
            --sidebar-width: 280px;
            --sidebar-collapsed: 0px;
            --content-bg: #f8fafc;
            --card-bg: #ffffff;
            --ink: #0f172a;
            --ink-secondary: #475569;
            --ink-muted: #94a3b8;
            --ink-light: #cbd5e1;
            --line: #e2e8f0;
            --line-light: #f1f5f9;
            --accent: #6366f1;
            --accent-hover: #4f46e5;
            --accent-light: #818cf8;
            --accent-soft: rgba(99, 102, 241, 0.08);
            --accent-medium: rgba(99, 102, 241, 0.15);
            --accent-glow: rgba(99, 102, 241, 0.4);
            --success: #10b981;
            --success-soft: rgba(16, 185, 129, 0.1);
            --warning: #f59e0b;
            --warning-soft: rgba(245, 158, 11, 0.1);
            --danger: #ef4444;
            --danger-soft: rgba(239, 68, 68, 0.1);
            --purple: #8b5cf6;
            --purple-soft: rgba(139, 92, 246, 0.1);
            --cyan: #06b6d4;
            --cyan-soft: rgba(6, 182, 212, 0.1);
            --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.05);
            --shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.07), 0 2px 4px -2px rgba(0, 0, 0, 0.05);
            --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.08), 0 4px 6px -4px rgba(0, 0, 0, 0.05);
            --shadow-xl: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 8px 10px -6px rgba(0, 0, 0, 0.05);
            --radius-xl: 16px;
            --radius-lg: 12px;
            --radius-md: 10px;
            --radius-sm: 8px;
            --radius-xs: 6px;
            --transition: 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            color: var(--ink);
            background: var(--content-bg);
            min-height: 100vh;
            overflow-x: hidden;
        }

        a { color: inherit; text-decoration: none; }

        .shell {
            min-height: 100vh;
            display: flex;
        }

        .sidebar {
            width: var(--sidebar-width);
            background: var(--sidebar-bg-gradient);
            position: fixed;
            top: 0;
            left: 0;
            height: 100vh;
            z-index: 100;
            display: flex;
            flex-direction: column;
            transition: transform var(--transition);
            overflow: hidden;
        }

        .sidebar.collapsed {
            transform: translateX(calc(-1 * var(--sidebar-width)));
        }

        .sidebar-overlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.5);
            backdrop-filter: blur(4px);
            z-index: 99;
            opacity: 0;
            transition: opacity var(--transition);
        }

        .sidebar-overlay.visible {
            display: block;
            opacity: 1;
        }

        .sidebar-header {
            padding: 20px 20px 16px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
        }

        .sidebar-brand {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .sidebar-logo {
            width: 36px;
            height: 36px;
            border-radius: var(--radius-sm);
            background: linear-gradient(135deg, var(--accent), var(--accent-light));
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 16px;
            font-weight: 800;
            color: white;
            flex-shrink: 0;
        }

        .sidebar-title h1 {
            font-size: 1rem;
            font-weight: 700;
            color: #f8fafc;
            line-height: 1.2;
        }

        .sidebar-title span {
            font-size: 0.7rem;
            color: var(--ink-muted);
            letter-spacing: 0.05em;
            text-transform: uppercase;
        }

        .sidebar-toggle {
            position: fixed;
            top: 16px;
            left: 16px;
            z-index: 101;
            width: 40px;
            height: 40px;
            border-radius: var(--radius-sm);
            background: var(--card-bg);
            border: 1px solid var(--line);
            box-shadow: var(--shadow);
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all var(--transition);
            color: var(--ink-secondary);
        }

        .sidebar-toggle:hover {
            background: var(--accent-soft);
            border-color: var(--accent);
            color: var(--accent);
        }

        .sidebar-toggle svg {
            width: 20px;
            height: 20px;
            transition: transform var(--transition);
        }

        .sidebar.collapsed ~ .main-wrapper .sidebar-toggle svg {
            transform: rotate(180deg);
        }

        .sidebar-mode-toggle {
            display: flex;
            gap: 6px;
            padding: 12px 20px;
        }
        .mode-btn {
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            padding: 8px 12px;
            border-radius: var(--radius-sm);
            color: var(--ink-muted);
            text-decoration: none;
            font-size: 0.8rem;
            font-weight: 600;
            transition: all 0.2s;
            border: 1px solid transparent;
        }
        .mode-btn:hover {
            background: rgba(255, 255, 255, 0.08);
            color: #f8fafc;
        }
        .mode-btn.active {
            background: var(--accent);
            color: #fff;
            border-color: var(--accent);
        }
        .admin-badge {
            background: #f59e0b;
            color: #000;
            padding: 1px 6px;
            border-radius: 4px;
            font-size: 0.65rem;
            font-weight: 700;
            text-transform: uppercase;
        }

        .sidebar-search {
            padding: 16px 20px;
        }

        .sidebar-search input {
            width: 100%;
            padding: 10px 14px;
            border-radius: var(--radius-sm);
            border: 1px solid rgba(255, 255, 255, 0.1);
            background: rgba(255, 255, 255, 0.06);
            color: #f8fafc;
            font-size: 0.85rem;
            outline: none;
            transition: all 0.2s;
        }

        .sidebar-search input::placeholder { color: var(--ink-muted); }

        .sidebar-search input:focus {
            border-color: var(--accent);
            background: rgba(255, 255, 255, 0.1);
            box-shadow: 0 0 0 3px var(--accent-soft);
        }

        .sidebar-nav {
            flex: 1;
            overflow-y: auto;
            padding: 8px 12px;
        }

        .sidebar-nav::-webkit-scrollbar { width: 4px; }
        .sidebar-nav::-webkit-scrollbar-track { background: transparent; }
        .sidebar-nav::-webkit-scrollbar-thumb { background: rgba(255, 255, 255, 0.15); border-radius: 2px; }

        .nav-section-label {
            font-size: 0.65rem;
            font-weight: 600;
            color: var(--ink-muted);
            text-transform: uppercase;
            letter-spacing: 0.1em;
            padding: 12px 8px 8px;
        }

        .project-item {
            display: block;
            padding: 10px 12px;
            border-radius: var(--radius-sm);
            margin-bottom: 2px;
            transition: all 0.15s;
            cursor: pointer;
        }

        .project-item:hover {
            background: rgba(255, 255, 255, 0.08);
        }

        .project-item.active {
            background: var(--accent-medium);
            border: 1px solid rgba(99, 102, 241, 0.3);
        }

        .project-item-name {
            font-size: 0.85rem;
            font-weight: 600;
            color: #e2e8f0;
            line-height: 1.3;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .project-item.active .project-item-name { color: var(--accent-light); }

        .project-item-meta {
            font-size: 0.72rem;
            color: var(--ink-muted);
            margin-top: 4px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .project-item-status {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 2px 6px;
            border-radius: 4px;
            background: rgba(16, 185, 129, 0.15);
            color: #34d399;
            font-size: 0.65rem;
            font-weight: 600;
        }

        .sidebar-footer {
            padding: 16px 20px;
            border-top: 1px solid rgba(255, 255, 255, 0.08);
        }

        .sidebar-footer a {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 10px 12px;
            border-radius: var(--radius-sm);
            color: var(--ink-muted);
            font-size: 0.85rem;
            transition: all 0.15s;
        }

        .sidebar-footer a:hover {
            background: rgba(255, 255, 255, 0.08);
            color: #e2e8f0;
        }

        .main-wrapper {
            flex: 1;
            margin-left: var(--sidebar-width);
            transition: margin-left var(--transition);
            min-width: 0;
        }

        .main-wrapper.expanded {
            margin-left: 0;
        }

        .topbar {
            position: sticky;
            top: 0;
            z-index: 50;
            background: rgba(248, 250, 252, 0.8);
            backdrop-filter: blur(12px);
            border-bottom: 1px solid var(--line);
            padding: 16px 24px;
            display: flex;
            align-items: center;
            gap: 16px;
        }

        .topbar-spacer {
            width: 40px;
            flex-shrink: 0;
        }

        .topbar-search {
            flex: 1;
            max-width: 480px;
            position: relative;
        }

        .topbar-search input {
            width: 100%;
            padding: 10px 14px 10px 40px;
            border-radius: var(--radius-md);
            border: 1px solid var(--line);
            background: var(--card-bg);
            color: var(--ink);
            font-size: 0.88rem;
            outline: none;
            transition: all 0.2s;
        }

        .topbar-search input:focus {
            border-color: var(--accent);
            box-shadow: 0 0 0 3px var(--accent-soft);
        }

        .topbar-search-icon {
            position: absolute;
            left: 14px;
            top: 50%;
            transform: translateY(-50%);
            color: var(--ink-muted);
            pointer-events: none;
        }

        .topbar-actions {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-left: auto;
        }

        .topbar-btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 40px;
            height: 40px;
            border-radius: var(--radius-sm);
            border: 1px solid var(--line);
            background: var(--card-bg);
            color: var(--ink-secondary);
            cursor: pointer;
            transition: all 0.15s;
        }

        .topbar-btn:hover {
            background: var(--accent-soft);
            border-color: var(--accent);
            color: var(--accent);
        }

        .main-content {
            padding: 24px;
        }

        .page-header {
            margin-bottom: 24px;
        }

        .page-header h2 {
            font-size: 1.75rem;
            font-weight: 800;
            letter-spacing: -0.02em;
            line-height: 1.2;
        }

        .page-header p {
            color: var(--ink-muted);
            margin-top: 6px;
            font-size: 0.92rem;
        }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }

        .stat-card {
            background: var(--card-bg);
            border: 1px solid var(--line);
            border-radius: var(--radius-lg);
            padding: 20px;
            transition: all 0.2s;
            position: relative;
            overflow: hidden;
        }

        .stat-card:hover {
            box-shadow: var(--shadow-lg);
            transform: translateY(-2px);
        }

        .stat-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 3px;
        }

        .stat-card.purple::before { background: linear-gradient(90deg, var(--accent), var(--accent-light)); }
        .stat-card.green::before { background: linear-gradient(90deg, var(--success), #34d399); }
        .stat-card.orange::before { background: linear-gradient(90deg, var(--warning), #fbbf24); }
        .stat-card.cyan::before { background: linear-gradient(90deg, var(--cyan), #22d3ee); }

        .stat-card-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 12px;
        }

        .stat-card-label {
            font-size: 0.78rem;
            color: var(--ink-muted);
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .stat-card-icon {
            width: 36px;
            height: 36px;
            border-radius: var(--radius-sm);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 16px;
        }

        .stat-card.purple .stat-card-icon { background: var(--accent-soft); color: var(--accent); }
        .stat-card.green .stat-card-icon { background: var(--success-soft); color: var(--success); }
        .stat-card.orange .stat-card-icon { background: var(--warning-soft); color: var(--warning); }
        .stat-card.cyan .stat-card-icon { background: var(--cyan-soft); color: var(--cyan); }

        .stat-card-value {
            font-size: 1.8rem;
            font-weight: 800;
            letter-spacing: -0.02em;
        }

        .stat-card-sub {
            font-size: 0.78rem;
            color: var(--ink-muted);
            margin-top: 4px;
        }

        .card {
            background: var(--card-bg);
            border: 1px solid var(--line);
            border-radius: var(--radius-lg);
            box-shadow: var(--shadow-sm);
        }

        .card-header {
            padding: 18px 20px;
            border-bottom: 1px solid var(--line-light);
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            flex-wrap: wrap;
        }

        .card-header h3 {
            font-size: 1rem;
            font-weight: 700;
        }

        .card-body { padding: 20px; }

        .filter-panel {
            padding: 16px 20px;
            display: grid;
            gap: 12px;
        }

        .filter-row {
            display: flex;
            gap: 10px;
            align-items: center;
            flex-wrap: wrap;
        }

        .filter-input, .filter-select {
            border: 1px solid var(--line);
            background: var(--card-bg);
            color: var(--ink);
            border-radius: var(--radius-sm);
            padding: 9px 14px;
            font-size: 0.84rem;
            outline: none;
            transition: all 0.15s;
        }

        .filter-input:focus, .filter-select:focus {
            border-color: var(--accent);
            box-shadow: 0 0 0 3px var(--accent-soft);
        }

        .filter-input { flex: 1 1 220px; min-width: 180px; }
        .filter-select { min-width: 130px; }

        .filter-label {
            font-size: 0.72rem;
            font-weight: 600;
            color: var(--ink-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .filter-advanced {
            padding: 14px 16px;
            background: var(--content-bg);
            border-radius: var(--radius-md);
            border: 1px solid var(--line-light);
            display: none;
        }

        .filter-advanced.visible { display: block; }

        .filter-section {
            display: flex;
            gap: 10px;
            align-items: flex-start;
            flex-wrap: wrap;
            padding: 10px 0;
        }

        .filter-section + .filter-section { border-top: 1px solid var(--line-light); }

        .filter-section-title {
            font-size: 0.72rem;
            font-weight: 600;
            color: var(--ink-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            min-width: 60px;
            padding-top: 4px;
        }

        .checkbox-group { display: flex; gap: 6px; flex-wrap: wrap; }

        .checkbox-label {
            display: inline-flex;
            align-items: center;
            gap: 5px;
            padding: 5px 10px;
            border-radius: var(--radius-xs);
            background: var(--card-bg);
            border: 1px solid var(--line);
            font-size: 0.78rem;
            cursor: pointer;
            transition: all 0.15s;
            user-select: none;
        }

        .checkbox-label:hover {
            border-color: var(--accent);
            background: var(--accent-soft);
        }

        .checkbox-label input[type="checkbox"],
        .checkbox-label input[type="radio"] {
            accent-color: var(--accent);
            width: 14px;
            height: 14px;
        }

        .checkbox-label input:checked + span {
            color: var(--accent);
            font-weight: 600;
        }

        .date-inputs {
            display: flex;
            gap: 6px;
            align-items: center;
        }

        .date-inputs input[type="date"] {
            border: 1px solid var(--line);
            background: var(--card-bg);
            color: var(--ink);
            border-radius: var(--radius-xs);
            padding: 5px 8px;
            font-size: 0.8rem;
            outline: none;
        }

        .date-inputs input[type="date"]:focus {
            border-color: var(--accent);
            box-shadow: 0 0 0 2px var(--accent-soft);
        }

        .date-inputs span { color: var(--ink-muted); font-size: 0.78rem; }

        .btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            border: none;
            border-radius: var(--radius-sm);
            padding: 9px 16px;
            font-size: 0.84rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.15s;
            white-space: nowrap;
        }

        .btn-primary {
            background: var(--accent);
            color: white;
        }

        .btn-primary:hover {
            background: var(--accent-hover);
            box-shadow: 0 4px 12px var(--accent-glow);
        }

        .btn-secondary {
            background: var(--card-bg);
            color: var(--ink-secondary);
            border: 1px solid var(--line);
        }

        .btn-secondary:hover { background: var(--content-bg); }

        .btn-ghost {
            background: transparent;
            color: var(--ink-muted);
            padding: 8px 12px;
        }

        .btn-ghost:hover { background: var(--content-bg); color: var(--ink); }

        .filter-actions { display: flex; gap: 8px; margin-left: auto; }

        .tabs {
            display: flex;
            gap: 2px;
            background: var(--content-bg);
            padding: 4px;
            border-radius: var(--radius-md);
            width: fit-content;
        }

        .tab {
            padding: 8px 16px;
            border-radius: var(--radius-sm);
            font-size: 0.84rem;
            font-weight: 600;
            color: var(--ink-muted);
            cursor: pointer;
            transition: all 0.15s;
            border: none;
            background: transparent;
        }

        .tab:hover { color: var(--ink); }

        .tab.active {
            background: var(--card-bg);
            color: var(--accent);
            box-shadow: var(--shadow-sm);
        }

        .view-content { display: none; }
        .view-content.active { display: block; }

        .team-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 16px; margin-top: 16px; }
        .member-card {
            background: var(--card-bg);
            border: 1px solid var(--line-light);
            border-radius: var(--radius-lg);
            padding: 16px;
            cursor: pointer;
            transition: transform 0.15s, box-shadow 0.15s, border-color 0.15s;
            display: flex;
            align-items: center;
            gap: 14px;
        }
        .member-card:hover { transform: translateY(-2px); box-shadow: var(--shadow-md); border-color: var(--accent); }
        .member-avatar {
            width: 44px; height: 44px; border-radius: 50%;
            background: var(--accent); color: var(--card-bg);
            display: flex; align-items: center; justify-content: center;
            font-weight: 700; font-size: 1rem; flex-shrink: 0;
        }
        .member-info { min-width: 0; }
        .member-name { font-weight: 600; font-size: 0.95rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .member-count { font-size: 0.8rem; color: #64748b; margin-top: 2px; }
        .member-back-btn {
            display: inline-flex; align-items: center; gap: 6px;
            background: var(--content-bg); border: 1px solid var(--line-light);
            border-radius: var(--radius-md); padding: 6px 12px;
            color: var(--text-primary); font-size: 0.85rem; cursor: pointer;
            transition: background 0.15s, border-color 0.15s;
        }
        .member-back-btn:hover { background: var(--accent); color: var(--card-bg); border-color: var(--accent); }
        .member-detail-header { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
        .member-detail-title { font-size: 1.1rem; font-weight: 700; }

        .user-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }
        .user-card {
            background: var(--card-bg);
            border: 1px solid var(--line-light);
            border-radius: var(--radius-lg);
            padding: 16px;
            display: flex;
            align-items: center;
            gap: 14px;
            text-decoration: none;
            color: inherit;
            transition: transform 0.15s, box-shadow 0.15s, border-color 0.15s;
        }
        .user-card:hover { transform: translateY(-2px); box-shadow: var(--shadow-md); border-color: var(--accent); }
        .user-card-avatar {
            width: 48px; height: 48px; border-radius: 50%;
            background: linear-gradient(135deg, var(--accent), #8b5cf6);
            color: #fff; display: flex; align-items: center; justify-content: center;
            font-weight: 700; font-size: 1.2rem; flex-shrink: 0;
        }
        .user-card-info { min-width: 0; }
        .user-card-name { font-weight: 600; font-size: 0.95rem; }
        .user-card-login { font-size: 0.8rem; color: #64748b; margin-top: 2px; }
        .user-card-email { font-size: 0.75rem; color: #94a3b8; margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

        .board {
            display: grid;
            grid-auto-flow: column;
            grid-auto-columns: minmax(270px, 1fr);
            gap: 14px;
            overflow-x: auto;
            padding: 4px;
        }

        .board::-webkit-scrollbar { height: 6px; }
        .board::-webkit-scrollbar-track { background: transparent; }
        .board::-webkit-scrollbar-thumb { background: var(--line); border-radius: 3px; }

        .kanban-column {
            background: var(--content-bg);
            border: 1px solid var(--line-light);
            border-radius: var(--radius-lg);
            padding: 12px;
            min-height: 300px;
        }

        .column-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 10px;
            margin-bottom: 12px;
            padding-bottom: 10px;
            border-bottom: 2px solid var(--column-color, var(--line));
        }

        .column-title {
            font-size: 0.88rem;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .column-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            flex-shrink: 0;
        }

        .column-count {
            min-width: 24px;
            height: 22px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            border-radius: 999px;
            background: var(--card-bg);
            color: var(--ink-muted);
            border: 1px solid var(--line);
            font-size: 0.72rem;
            font-weight: 700;
        }

        .kanban-card {
            display: block;
            border: 1px solid var(--line);
            border-radius: var(--radius-md);
            background: var(--card-bg);
            margin-bottom: 10px;
            box-shadow: var(--shadow-sm);
            transition: all 0.2s;
            overflow: hidden;
            cursor: grab;
        }

        .kanban-card:active { cursor: grabbing; }

        .kanban-card.dragging {
            opacity: 0.4;
            transform: scale(0.95);
        }

        .kanban-card.drag-over {
            transform: translateY(-2px);
            box-shadow: var(--shadow-lg);
            border-color: var(--accent);
        }

        .kanban-column.drag-active {
            background: var(--accent-soft);
            border-color: var(--accent);
            border-style: dashed;
        }

        .drag-handle {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 18px;
            height: 18px;
            border-radius: 4px;
            color: var(--ink-muted);
            cursor: grab;
            flex-shrink: 0;
            transition: all 0.15s;
        }

        .drag-handle:hover {
            background: var(--accent-soft);
            color: var(--accent);
        }

        .drag-toast {
            position: fixed;
            bottom: 24px;
            left: 50%;
            transform: translateX(-50%) translateY(100px);
            padding: 12px 20px;
            border-radius: var(--radius-lg);
            background: #1e293b;
            color: #f8fafc;
            font-size: 0.88rem;
            font-weight: 600;
            box-shadow: var(--shadow-xl);
            z-index: 300;
            opacity: 0;
            transition: all 0.3s ease;
            pointer-events: none;
        }

        .drag-toast.visible {
            transform: translateX(-50%) translateY(0);
            opacity: 1;
        }

        .drag-toast.success { background: #059669; }
        .drag-toast.error { background: #dc2626; }

        .kanban-card:hover {
            transform: translateY(-2px);
            box-shadow: var(--shadow-lg);
            border-color: var(--accent);
        }

        .kanban-card-link {
            display: block;
            color: inherit;
            text-decoration: none;
            padding: 14px;
            border-left: 3px solid var(--card-accent, var(--accent));
        }

        .kanban-card-title {
            margin: 0 0 10px;
            font-size: 0.88rem;
            line-height: 1.4;
            font-weight: 600;
        }

        .tag-row { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 10px; }

        .tag {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            border-radius: var(--radius-xs);
            padding: 3px 7px;
            font-size: 0.7rem;
            font-weight: 600;
        }

        .tag-type { background: var(--accent-soft); color: var(--accent); }
        .tag-priority-high { background: var(--danger-soft); color: var(--danger); }
        .tag-priority-normal { background: var(--purple-soft); color: var(--purple); }
        .tag-priority-low { background: var(--success-soft); color: var(--success); }

        .kanban-dates {
            display: flex;
            justify-content: space-between;
            gap: 8px;
            margin-bottom: 10px;
            font-size: 0.75rem;
            color: var(--ink-muted);
        }

        .progress-section { margin-bottom: 10px; }

        .progress-header {
            display: flex;
            justify-content: space-between;
            font-size: 0.72rem;
            color: var(--ink-muted);
            margin-bottom: 6px;
        }

        .progress-track {
            width: 100%;
            height: 5px;
            background: var(--line-light);
            border-radius: 999px;
            overflow: hidden;
        }

        .progress-fill {
            height: 100%;
            border-radius: 999px;
            background: linear-gradient(90deg, var(--success), #34d399);
            transition: width 0.3s;
        }

        .kanban-footer {
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .avatar {
            width: 24px;
            height: 24px;
            border-radius: 999px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            background: var(--accent-medium);
            color: var(--accent);
            font-size: 0.6rem;
            font-weight: 700;
            flex-shrink: 0;
        }

        .assignee-text {
            color: var(--ink-muted);
            font-size: 0.75rem;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .gantt-wrapper { padding: 4px; }

        .gantt-toolbar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
        }

        .gantt-modes { display: flex; gap: 4px; }

        .gantt-mode {
            padding: 6px 12px;
            border-radius: var(--radius-xs);
            font-size: 0.78rem;
            font-weight: 600;
            color: var(--ink-muted);
            cursor: pointer;
            border: 1px solid var(--line);
            background: var(--card-bg);
            transition: all 0.15s;
        }

        .gantt-mode:hover { border-color: var(--accent); }

        .gantt-mode.active {
            background: var(--accent);
            color: white;
            border-color: var(--accent);
        }

        .gantt-chart {
            background: var(--card-bg);
            border-radius: var(--radius-md);
            border: 1px solid var(--line);
            overflow: hidden;
        }

        .gantt-stats {
            display: flex;
            gap: 16px;
            margin-bottom: 16px;
        }

        .gantt-stat {
            font-size: 0.82rem;
            color: var(--ink-muted);
        }

        .gantt-stat strong { color: var(--ink); font-weight: 700; }

        .gantt-container .gantt .grid-header { fill: var(--content-bg); stroke: var(--line-light); }
        .gantt-container .gantt .grid-row { fill: transparent; }
        .gantt-container .gantt .grid-row:nth-child(even) { fill: var(--content-bg); }
        .gantt-container .gantt .row-line { stroke: var(--line-light); }
        .gantt-container .gantt .bar { fill: var(--accent); }
        .gantt-container .gantt .bar-progress { fill: var(--success); }
        .gantt-container .gantt .bar-label { fill: var(--ink); font-size: 11px; }
        .gantt-container .gantt .bar-wrapper.high .bar { fill: var(--danger); }
        .gantt-container .gantt .bar-wrapper.high .bar-progress { fill: #f87171; }
        .gantt-container .gantt .bar-wrapper.normal .bar { fill: var(--accent); }
        .gantt-container .gantt .bar-wrapper.normal .bar-progress { fill: var(--success); }
        .gantt-container .gantt .bar-wrapper.low .bar { fill: var(--purple); }
        .gantt-container .gantt .bar-wrapper.low .bar-progress { fill: #a78bfa; }

        .empty-state {
            padding: 48px 24px;
            text-align: center;
            color: var(--ink-muted);
        }

        .empty-state-icon {
            font-size: 2rem;
            margin-bottom: 12px;
            opacity: 0.5;
        }

        .empty-state p { font-size: 0.92rem; }

        .badge {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 4px 10px;
            border-radius: 999px;
            font-size: 0.75rem;
            font-weight: 600;
        }

        .badge-accent { background: var(--accent-soft); color: var(--accent); }

        .active-filters-bar {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            padding: 12px 16px;
            background: var(--content-bg);
            border-radius: var(--radius-md);
            margin-top: 12px;
        }

        .filter-tag {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 4px 10px;
            border-radius: var(--radius-xs);
            background: var(--card-bg);
            border: 1px solid var(--line);
            font-size: 0.75rem;
            color: var(--ink-secondary);
        }

        .filter-tag-remove {
            cursor: pointer;
            color: var(--ink-muted);
            font-size: 0.8rem;
        }

        .filter-tag-remove:hover { color: var(--danger); }

        .chart-card {
            background: var(--card-bg);
            border: 1px solid var(--line);
            border-radius: var(--radius-lg);
            padding: 16px;
        }

        .chart-card-header { margin-bottom: 12px; }

        .chart-card-label {
            font-size: 0.78rem;
            font-weight: 600;
            color: var(--ink-muted);
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }

        .modal-overlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.6);
            backdrop-filter: blur(4px);
            z-index: 200;
            align-items: center;
            justify-content: center;
            padding: 24px;
        }

        .modal-overlay.visible {
            display: flex;
        }

        .modal {
            background: var(--card-bg);
            border-radius: var(--radius-xl);
            box-shadow: var(--shadow-xl);
            width: min(520px, 100%);
            max-height: 85vh;
            overflow-y: auto;
        }

        .modal-header {
            padding: 20px 24px;
            border-bottom: 1px solid var(--line-light);
            display: flex;
            align-items: start;
            justify-content: space-between;
            gap: 16px;
        }

        .modal-header h3 {
            font-size: 1.1rem;
            font-weight: 700;
        }

        .modal-header-sub {
            font-size: 0.82rem;
            color: var(--ink-muted);
            margin-top: 4px;
        }

        .modal-close {
            width: 32px;
            height: 32px;
            border-radius: var(--radius-sm);
            border: 1px solid var(--line);
            background: var(--card-bg);
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            color: var(--ink-muted);
            flex-shrink: 0;
            transition: all 0.15s;
        }

        .modal-close:hover {
            background: var(--danger-soft);
            color: var(--danger);
            border-color: var(--danger);
        }

        .modal-body { padding: 20px 24px; }

        .modal-field {
            margin-bottom: 16px;
        }

        .modal-field:last-child { margin-bottom: 0; }

        .modal-field-label {
            font-size: 0.72rem;
            font-weight: 600;
            color: var(--ink-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 6px;
        }

        .modal-field-value {
            font-size: 0.92rem;
            color: var(--ink);
        }

        .modal-tags { display: flex; gap: 6px; flex-wrap: wrap; }

        .modal-progress {
            margin-top: 8px;
        }

        .modal-progress-track {
            width: 100%;
            height: 8px;
            background: var(--line-light);
            border-radius: 999px;
            overflow: hidden;
        }

        .modal-progress-fill {
            height: 100%;
            border-radius: 999px;
            background: linear-gradient(90deg, var(--success), #34d399);
        }

        .modal-progress-header {
            display: flex;
            justify-content: space-between;
            font-size: 0.78rem;
            color: var(--ink-muted);
            margin-bottom: 6px;
        }

        .modal-actions {
            padding: 16px 24px;
            border-top: 1px solid var(--line-light);
            display: flex;
            gap: 10px;
            justify-content: flex-end;
        }

        .kanban-card.overdue {
            border-color: #fecaca;
            background: linear-gradient(135deg, #ffffff 0%, #fef2f2 100%);
        }

        .kanban-card.overdue .kanban-card-link {
            border-left-color: #ef4444;
        }

        .overdue-badge {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 2px 6px;
            border-radius: 4px;
            background: var(--danger-soft);
            color: var(--danger);
            font-size: 0.65rem;
            font-weight: 700;
            text-transform: uppercase;
        }

        @media (max-width: 1024px) {
            .sidebar { transform: translateX(calc(-1 * var(--sidebar-width))); }
            .sidebar.mobile-open { transform: translateX(0); }
            .sidebar-overlay.visible { display: block; opacity: 1; }
            .main-wrapper { margin-left: 0 !important; }
            .stats-grid { grid-template-columns: repeat(2, 1fr); }
        }

        @media (max-width: 640px) {
            .main-content { padding: 16px; }
            .stats-grid { grid-template-columns: 1fr; }
            .filter-row { flex-direction: column; align-items: stretch; }
            .filter-actions { margin-left: 0; }
            .board { grid-auto-columns: minmax(250px, 85vw); }
        }
    </style>
</head>
<body>
    <div class="shell">
        <div class="sidebar-overlay" id="sidebarOverlay" onclick="toggleSidebar()"></div>

        <aside class="sidebar {% if not project and mode != 'users' %}collapsed{% endif %}" id="sidebar">
            <div class="sidebar-header">
                <div class="sidebar-brand">
                    <div class="sidebar-logo">OP</div>
                    <div class="sidebar-title">
                        <h1>OpenProject</h1>
                        <span>{% if mode == 'users' %}Admin Panel{% else %}Portfolio Dashboard{% endif %}</span>
                    </div>
                </div>
            </div>

            <div class="sidebar-mode-toggle">
                <a class="mode-btn {% if mode == 'projects' %}active{% endif %}" href="/?mode=projects{% if view %}&view={{ view }}{% endif %}">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/>
                    </svg>
                    Projects
                </a>
                <a class="mode-btn {% if mode == 'users' %}active{% endif %}" href="/?mode=users{% if view %}&view={{ view }}{% endif %}">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/>
                    </svg>
                    Users
                </a>
            </div>

            {% if mode == 'users' %}
                <div class="sidebar-search">
                    <form method="get">
                        <input type="hidden" name="mode" value="users" />
                        {% if view %}
                            <input type="hidden" name="view" value="{{ view }}" />
                        {% endif %}
                        {% if selected_user_id %}
                            <input type="hidden" name="user_id" value="{{ selected_user_id }}" />
                        {% endif %}
                        <input name="user_search" value="{{ user_search }}" placeholder="Search users..." oninput="autoSubmitFiltersDebounce()" />
                    </form>
                </div>
                <nav class="sidebar-nav">
                    <div class="nav-section-label">Users ({{ user_list|length }})</div>
                    {% for u in user_list %}
                        <a class="project-item {% if selected_user_id and selected_user_id == u.id %}active{% endif %}"
                           href="/?mode=users&user_id={{ u.id }}{% if user_search %}&user_search={{ user_search|urlencode }}{% endif %}{% if view %}&view={{ view }}{% endif %}">
                            <div class="project-item-name">{{ u.name }}</div>
                            <div class="project-item-meta">
                                {% if u.login %}<span>{{ u.login }}</span>{% endif %}
                                {% if u.admin %}<span class="admin-badge">Admin</span>{% endif %}
                            </div>
                        </a>
                    {% else %}
                        <div class="empty-state" style="padding: 24px 12px;">
                            <p>No users found</p>
                        </div>
                    {% endfor %}
                </nav>
            {% else %}
                <div class="sidebar-search">
                    <form method="get">
                        {% if selected_project_id %}
                            <input type="hidden" name="project_id" value="{{ selected_project_id }}" />
                        {% endif %}
                        {% if view %}
                            <input type="hidden" name="view" value="{{ view }}" />
                        {% endif %}
                        <input name="project_search" value="{{ project_search }}" placeholder="Search projects..." oninput="autoSubmitFiltersDebounce()" />
                    </form>
                </div>
                <nav class="sidebar-nav">
                    <div class="nav-section-label">Projects</div>
                    {% for item in projects %}
                        <a class="project-item {% if item.id == selected_project_id %}active{% endif %}"
                           href="/?project_id={{ item.id }}{% if project_search %}&project_search={{ project_search|urlencode }}{% endif %}{% if view %}&view={{ view }}{% endif %}">
                            <div class="project-item-name">{{ item.name }}</div>
                            <div class="project-item-meta">
                                {% if item.identifier %}<span>{{ item.identifier }}</span>{% endif %}
                                {% if item.updated_at %}<span>{{ item.updated_at }}</span>{% endif %}
                                <span class="project-item-status">{{ item.status }}</span>
                            </div>
                        </a>
                    {% else %}
                        <div class="empty-state" style="padding: 24px 12px;">
                            <p>No projects found</p>
                        </div>
                    {% endfor %}
                </nav>
            {% endif %}

            <div class="sidebar-footer">
                <a href="{{ openproject_url }}" target="_blank" rel="noreferrer">
                    <span>Open in OpenProject</span>
                </a>
            </div>
        </aside>

        <div class="main-wrapper {% if not project and not selected_user_id %}expanded{% endif %}" id="mainWrapper">
            <header class="topbar">
                <div class="topbar-spacer"></div>
                {% if project or selected_user_id %}
                    <div class="topbar-search">
                        <svg class="topbar-search-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
                        </svg>
                        <form method="get">
                            {% if project %}
                                <input type="hidden" name="project_id" value="{{ selected_project_id }}" />
                                {% if project_search %}
                                    <input type="hidden" name="project_search" value="{{ project_search }}" />
                                {% endif %}
                            {% endif %}
                            {% if selected_user_id %}
                                <input type="hidden" name="mode" value="users" />
                                <input type="hidden" name="user_id" value="{{ selected_user_id }}" />
                                {% if user_search %}
                                    <input type="hidden" name="user_search" value="{{ user_search }}" />
                                {% endif %}
                            {% endif %}
                            <input type="hidden" name="view" value="{{ view }}" />
                            <input name="search" value="{{ search }}" placeholder="Search work packages..." oninput="autoSubmitFiltersDebounce()" />
                        </form>
                    </div>
                {% endif %}
                <div class="topbar-actions">
                    {% if project %}
                        <form method="get" style="display: inline;">
                            <input type="hidden" name="project_id" value="{{ selected_project_id }}" />
                            {% if project_search %}
                                <input type="hidden" name="project_search" value="{{ project_search }}" />
                            {% endif %}
                            {% if view %}
                                <input type="hidden" name="view" value="{{ view }}" />
                            {% endif %}
                            <button type="submit" class="topbar-btn" title="Apply filters">
                                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                    <path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z"/>
                                </svg>
                            </button>
                        </form>
                    {% endif %}
                </div>
            </header>

            <main class="main-content">
                {% if project %}
                    <div class="page-header">
                        <h2>{{ project.name }}</h2>
                        <p>{{ project.description }}</p>
                    </div>

                    <div class="stats-grid">
                        <div class="stat-card purple">
                            <div class="stat-card-header">
                                <span class="stat-card-label">Total Tasks</span>
                                <div class="stat-card-icon">
                                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                        <path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/>
                                    </svg>
                                </div>
                            </div>
                            <div class="stat-card-value">{{ total_tasks }}</div>
                            <div class="stat-card-sub">Across {{ board|length }} columns</div>
                        </div>

                        <div class="stat-card green">
                            <div class="stat-card-header">
                                <span class="stat-card-label">Team Members</span>
                                <div class="stat-card-icon">
                                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                        <path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/>
                                    </svg>
                                </div>
                            </div>
                            <div class="stat-card-value">{{ members|length }}</div>
                            <div class="stat-card-sub">{{ projects|length }} project{{ 's' if projects|length != 1 }}</div>
                        </div>

                        <div class="stat-card orange">
                            <div class="stat-card-header">
                                <span class="stat-card-label">Status</span>
                                <div class="stat-card-icon">
                                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                        <circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>
                                    </svg>
                                </div>
                            </div>
                            <div class="stat-card-value" style="font-size: 1.2rem;">{{ project.status }}</div>
                            {% if project.identifier %}
                                <div class="stat-card-sub">{{ project.identifier }}</div>
                            {% endif %}
                        </div>

                        <div class="stat-card cyan">
                            <div class="stat-card-header">
                                <span class="stat-card-label">View</span>
                                <div class="stat-card-icon">
                                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                        <rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>
                                    </svg>
                                </div>
                            </div>
                            <div class="stat-card-value" style="font-size: 1.2rem;">{{ view|title }}</div>
                            <div class="stat-card-sub">Switch in tabs below</div>
                        </div>
                    </div>

                    <div class="card">
                        <div class="card-header">
                            <div class="tabs">
                                <button class="tab {% if view == 'kanban' %}active{% endif %}" onclick="switchView('kanban')">Kanban</button>
                                <button class="tab {% if view == 'gantt' %}active{% endif %}" onclick="switchView('gantt')">Gantt</button>
                                <button class="tab {% if view == 'team' %}active{% endif %}" onclick="switchView('team')">Team</button>
                            </div>
                            <div style="display: flex; gap: 8px; align-items: center;">
                                {% if active_filters_count > 0 %}
                                    <span class="badge badge-accent">{{ active_filters_count }} filter{{ 's' if active_filters_count > 1 }} active</span>
                                {% endif %}
                                <button class="btn btn-secondary" id="toggleFilters" onclick="toggleFilters()">
                                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                        <path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z"/>
                                    </svg>
                                    Filters
                                </button>
                                <a class="btn btn-ghost" href="/?project_id={{ selected_project_id }}{% if project_search %}&project_search={{ project_search|urlencode }}{% endif %}{% if view %}&view={{ view }}{% endif %}">Reset</a>
                            </div>
                        </div>

                        <form method="get" id="filterForm">
                            <input type="hidden" name="project_id" value="{{ selected_project_id }}" />
                            <input type="hidden" name="view" value="{{ view }}" id="viewInput" />
                            {% if project_search %}
                                <input type="hidden" name="project_search" value="{{ project_search }}" />
                            {% endif %}

                            <div class="filter-advanced" id="filterAdvanced">
                                <div class="filter-section">
                                    <span class="filter-section-title">Assignee</span>
                                    <select name="assignee_id" class="filter-select" onchange="autoSubmitFilters()">
                                        <option value="">All members</option>
                                        {% for member in members %}
                                            <option value="{{ member.principal_id }}" {% if assignee_filter_id == member.principal_id %}selected{% endif %}>{{ member.name }}</option>
                                        {% endfor %}
                                    </select>
                                </div>

                                <div class="filter-section">
                                    <span class="filter-section-title">Members</span>
                                    <div class="checkbox-group">
                                        {% for member in members %}
                                            <label class="checkbox-label">
                                                <input type="checkbox" name="member" value="{{ member.name }}" {% if member.name in member_filter %}checked{% endif %} onchange="autoSubmitFilters()" />
                                                <span>{{ member.name }}</span>
                                            </label>
                                        {% endfor %}
                                    </div>
                                </div>

                                <div class="filter-section">
                                    <span class="filter-section-title">Status</span>
                                    <div class="checkbox-group">
                                        {% for status in filter_options.statuses %}
                                            <label class="checkbox-label">
                                                <input type="checkbox" name="status" value="{{ status }}" {% if status in status_filter %}checked{% endif %} onchange="autoSubmitFilters()" />
                                                <span>{{ status }}</span>
                                            </label>
                                        {% endfor %}
                                    </div>
                                </div>

                                <div class="filter-section">
                                    <span class="filter-section-title">Priority</span>
                                    <div class="checkbox-group">
                                        {% for priority in filter_options.priorities %}
                                            <label class="checkbox-label">
                                                <input type="radio" name="priority" value="{{ priority }}" {% if priority_filter == priority %}checked{% endif %} onchange="autoSubmitFilters()" />
                                                <span>{{ priority }}</span>
                                            </label>
                                        {% endfor %}
                                    </div>
                                </div>

                                <div class="filter-section">
                                    <span class="filter-section-title">Type</span>
                                    <select name="type" class="filter-select" onchange="autoSubmitFilters()">
                                        <option value="">All types</option>
                                        {% for t in filter_options.types %}
                                            <option value="{{ t }}" {% if type_filter == t %}selected{% endif %}>{{ t }}</option>
                                        {% endfor %}
                                    </select>
                                </div>

                                <div class="filter-section">
                                    <span class="filter-section-title">Date range</span>
                                    <div class="date-inputs">
                                        <input type="date" name="date_from" value="{{ date_from }}" onchange="autoSubmitFilters()" />
                                        <span>to</span>
                                        <input type="date" name="date_to" value="{{ date_to }}" onchange="autoSubmitFilters()" />
                                    </div>
                                </div>

                                <div class="filter-section">
                                    <span class="filter-section-title">Sort</span>
                                    <select name="sort" class="filter-select" onchange="autoSubmitFilters()">
                                        <option value="">Default</option>
                                        <option value="priority" {% if sort_by == "priority" %}selected{% endif %}>Priority</option>
                                        <option value="due_date" {% if sort_by == "due_date" %}selected{% endif %}>Due Date</option>
                                        <option value="start_date" {% if sort_by == "start_date" %}selected{% endif %}>Start Date</option>
                                        <option value="progress" {% if sort_by == "progress" %}selected{% endif %}>Progress</option>
                                        <option value="assignee" {% if sort_by == "assignee" %}selected{% endif %}>Assignee</option>
                                        <option value="title" {% if sort_by == "title" %}selected{% endif %}>Title</option>
                                    </select>
                                </div>

                                <div class="filter-section">
                                    <div style="margin-left: auto; display: flex; gap: 8px;">
                                        <button type="submit" class="btn btn-primary">Apply Filters</button>
                                        <button type="button" class="btn btn-secondary" onclick="resetFilters()">Clear All</button>
                                    </div>
                                </div>
                            </div>
                        </form>

                        <div class="card-body">
                            {% if overdue_tasks %}
                                <div style="margin-bottom: 20px;">
                                    <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px;">
                                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#ef4444" stroke-width="2">
                                            <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
                                        </svg>
                                        <h3 style="font-size: 0.95rem; font-weight: 700; color: #ef4444;">Overdue Tasks ({{ overdue_tasks|length }})</h3>
                                    </div>
                                    <div style="display: grid; gap: 8px;">
                                        {% for task in overdue_tasks[:6] %}
                                            <a href="{{ task.href }}" target="_blank" rel="noreferrer" class="overdue-task" style="display: flex; align-items: center; justify-content: space-between; padding: 10px 14px; border-radius: 8px; border: 1px solid #fecaca; background: #fef2f2; transition: all 0.15s;">
                                                <div style="display: flex; align-items: center; gap: 10px; min-width: 0;">
                                                    <span style="font-size: 0.75rem; font-weight: 700; color: #ef4444;">#{{ task.id }}</span>
                                                    <span style="font-size: 0.85rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{{ task.title }}</span>
                                                </div>
                                                <div style="display: flex; align-items: center; gap: 12px; flex-shrink: 0; margin-left: 12px;">
                                                    <span style="font-size: 0.75rem; color: #64748b;">{{ task.assignee }}</span>
                                                    <span style="font-size: 0.75rem; font-weight: 600; color: #ef4444; white-space: nowrap;">{{ task.due_date }}</span>
                                                </div>
                                            </a>
                                        {% endfor %}
                                        {% if overdue_tasks|length > 6 %}
                                            <div style="text-align: center; padding: 8px; font-size: 0.8rem; color: #64748b;">+ {{ overdue_tasks|length - 6 }} more overdue tasks</div>
                                        {% endif %}
                                    </div>
                                </div>
                            {% endif %}

                            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 24px;">
                                <div class="chart-card">
                                    <div class="chart-card-header">
                                        <span class="chart-card-label">Status Distribution</span>
                                    </div>
                                    <canvas id="statusChart" style="max-height: 220px;"></canvas>
                                </div>
                                <div class="chart-card">
                                    <div class="chart-card-header">
                                        <span class="chart-card-label">Priority Breakdown</span>
                                    </div>
                                    <canvas id="priorityChart" style="max-height: 220px;"></canvas>
                                </div>
                                <div class="chart-card">
                                    <div class="chart-card-header">
                                        <span class="chart-card-label">Team Workload</span>
                                    </div>
                                    <canvas id="workloadChart" style="max-height: 220px;"></canvas>
                                </div>
                            </div>

                            <div id="view-kanban" class="view-content {% if view == 'kanban' %}active{% endif %}">
                                {% if board %}
                                    <div class="board">
                                        {% for column in status_columns %}
                                            <div class="kanban-column" style="--column-color: {{ column.color }};" data-status-id="{{ column.id }}" data-status-href="{{ column.href }}" data-status-name="{{ column.name }}" ondragover="handleDragOver(event)" ondragleave="handleDragLeave(event)" ondrop="handleDrop(event)">
                                                <div class="column-header">
                                                    <span class="column-title">
                                                        <span class="column-dot" style="background: {{ column.color }};"></span>
                                                        {{ column.name }}
                                                    </span>
                                                    <span class="column-count">{{ board[column.name]|length }}</span>
                                                </div>
                                                {% for card in board[column.name] %}
                                                    <article class="kanban-card {% if card.is_overdue %}overdue{% endif %}" style="--card-accent: {{ card.accent }};" draggable="true" ondragstart="handleDragStart(event, '{{ card.id }}', '{{ card.status }}')" ondragend="handleDragEnd(event)" data-card-id="{{ card.id }}">
                                                        <a class="kanban-card-link" href="javascript:void(0)" onclick='openTaskModal({{ card | tojson }})' style="cursor: pointer;">
                                                            <div style="display: flex; align-items: center; justify-content: space-between; gap: 8px;">
                                                                <span style="display: flex; align-items: center; gap: 6px; min-width: 0;">
                                                                    <span class="drag-handle" onclick="event.preventDefault(); event.stopPropagation();">
                                                                        <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
                                                                            <circle cx="9" cy="6" r="2"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="9" cy="18" r="2"/><circle cx="15" cy="18" r="2"/>
                                                                        </svg>
                                                                    </span>
                                                                    <h4 class="kanban-card-title">#{{ card.id }} {{ card.title }}</h4>
                                                                </span>
                                                                {% if card.is_overdue %}
                                                                    <span class="overdue-badge">Overdue</span>
                                                                {% endif %}
                                                            </div>
                                                            <div class="tag-row">
                                                                <span class="tag tag-type">{{ card.type }}</span>
                                                                {% set pk = card.priority|lower %}
                                                                {% if "high" in pk or "urgent" in pk or "immediate" in pk %}
                                                                    <span class="tag tag-priority-high">{{ card.priority }}</span>
                                                                {% elif "low" in pk %}
                                                                    <span class="tag tag-priority-low">{{ card.priority }}</span>
                                                                {% else %}
                                                                    <span class="tag tag-priority-normal">{{ card.priority }}</span>
                                                                {% endif %}
                                                            </div>
                                                            <div class="kanban-dates">
                                                                <span>Start: {{ card.start_date or "—" }}</span>
                                                                <span>Due: {{ card.due_date or "—" }}</span>
                                                            </div>
                                                            <div class="progress-section">
                                                                <div class="progress-header">
                                                                    <span>Progress</span>
                                                                    <strong>{{ card.done }}%</strong>
                                                                </div>
                                                                <div class="progress-track">
                                                                    <div class="progress-fill" style="width: {{ card.done }}%;"></div>
                                                                </div>
                                                            </div>
                                                            <div class="kanban-footer">
                                                                <span class="avatar">{{ card.assignee_initials }}</span>
                                                                <span class="assignee-text">{{ card.assignee }}</span>
                                                            </div>
                                                        </a>
                                                    </article>
                                                {% endfor %}
                                            </div>
                                        {% endfor %}
                                    </div>
                                {% else %}
                                    <div class="empty-state">
                                        <div class="empty-state-icon">
                                            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                                <path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/>
                                            </svg>
                                        </div>
                                        <p>No work packages matched the current filters.</p>
                                    </div>
                                {% endif %}
                            </div>

                            <div id="view-gantt" class="view-content {% if view == 'gantt' %}active{% endif %}">
                                {% if gantt %}
                                    <div class="gantt-toolbar">
                                        <div class="gantt-stats">
                                            <span class="gantt-stat"><strong>{{ gantt|length }}</strong> tasks</span>
                                            {% set gantt_with_dates = gantt | selectattr('start', 'defined') | list %}
                                            {% if gantt_with_dates %}
                                                <span class="gantt-stat"><strong>{{ gantt_with_dates[0].start }}</strong> to <strong>{{ gantt_with_dates[-1].end }}</strong></span>
                                            {% endif %}
                                        </div>
                                        <div class="gantt-modes">
                                            <button class="gantt-mode" onclick="changeGanttMode('Quarter Day')">Quarter</button>
                                            <button class="gantt-mode" onclick="changeGanttMode('Half Day')">Half Day</button>
                                            <button class="gantt-mode active" onclick="changeGanttMode('Day')">Day</button>
                                            <button class="gantt-mode" onclick="changeGanttMode('Week')">Week</button>
                                            <button class="gantt-mode" onclick="changeGanttMode('Month')">Month</button>
                                        </div>
                                    </div>
                                    <div class="gantt-chart" id="gantt-chart"></div>
                                {% else %}
                                    <div class="empty-state">
                                        <div class="empty-state-icon">
                                            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                                <rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>
                                            </svg>
                                        </div>
                                        <p>No tasks with date ranges available for the Gantt chart.</p>
                                    </div>
                                {% endif %}
                            </div>

                            <div id="view-team" class="view-content {% if view == 'team' %}active{% endif %}">
                                {% if selected_member_name %}
                                    <div class="member-detail-header">
                                        <button class="member-back-btn" onclick="window.location.href='/?project_id={{ selected_project_id }}&view=team{% if project_search %}&project_search={{ project_search|urlencode }}{% endif %}'">
                                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                                <path d="M19 12H5M12 19l-7-7 7-7"/>
                                            </svg>
                                            Back to team
                                        </button>
                                        <div class="member-avatar">{{ selected_member_name[0] }}</div>
                                        <span class="member-detail-title">{{ selected_member_name }} <span style="color: #64748b; font-weight: 400; font-size: 0.9rem;">({{ member_total }} tasks)</span></span>
                                    </div>
                                    {% if member_board %}
                                        <div class="board">
                                            {% for column in status_columns %}
                                                <div class="kanban-column" style="--column-color: {{ column.color }};" data-status-id="{{ column.id }}" data-status-href="{{ column.href }}" data-status-name="{{ column.name }}" ondragover="handleDragOver(event)" ondragleave="handleDragLeave(event)" ondrop="handleDrop(event)">
                                                    <div class="column-header">
                                                        <span class="column-title">
                                                            <span class="column-dot" style="background: {{ column.color }};"></span>
                                                            {{ column.name }}
                                                        </span>
                                                        <span class="column-count">{{ member_board[column.name]|length }}</span>
                                                    </div>
                                                    {% for card in member_board[column.name] %}
                                                        <article class="kanban-card {% if card.is_overdue %}overdue{% endif %}" style="--card-accent: {{ card.accent }};" draggable="true" ondragstart="handleDragStart(event, '{{ card.id }}', '{{ card.status }}')" ondragend="handleDragEnd(event)" data-card-id="{{ card.id }}">
                                                            <a class="kanban-card-link" href="javascript:void(0)" onclick='openTaskModal({{ card | tojson }})' style="cursor: pointer;">
                                                                <div style="display: flex; align-items: center; justify-content: space-between; gap: 8px;">
                                                                    <span style="display: flex; align-items: center; gap: 6px; min-width: 0;">
                                                                        <span class="drag-handle" onclick="event.preventDefault(); event.stopPropagation();">
                                                                            <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
                                                                                <circle cx="9" cy="6" r="2"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="9" cy="18" r="2"/><circle cx="15" cy="18" r="2"/>
                                                                            </svg>
                                                                        </span>
                                                                        <h4 class="kanban-card-title">#{{ card.id }} {{ card.title }}</h4>
                                                                    </span>
                                                                    {% if card.is_overdue %}
                                                                        <span class="overdue-badge">Overdue</span>
                                                                    {% endif %}
                                                                </div>
                                                                <div class="tag-row">
                                                                    <span class="tag tag-type">{{ card.type }}</span>
                                                                    {% set pk = card.priority|lower %}
                                                                    {% if "high" in pk or "urgent" in pk or "immediate" in pk %}
                                                                        <span class="tag tag-priority-high">{{ card.priority }}</span>
                                                                    {% elif "low" in pk %}
                                                                        <span class="tag tag-priority-low">{{ card.priority }}</span>
                                                                    {% else %}
                                                                        <span class="tag tag-priority-normal">{{ card.priority }}</span>
                                                                    {% endif %}
                                                                </div>
                                                                <div class="kanban-dates">
                                                                    <span>Start: {{ card.start_date or "—" }}</span>
                                                                    <span>Due: {{ card.due_date or "—" }}</span>
                                                                </div>
                                                                <div class="progress-section">
                                                                    <div class="progress-header">
                                                                        <span>Progress</span>
                                                                        <strong>{{ card.done }}%</strong>
                                                                    </div>
                                                                    <div class="progress-track">
                                                                        <div class="progress-fill" style="width: {{ card.done }}%;"></div>
                                                                    </div>
                                                                </div>
                                                                <div class="kanban-footer">
                                                                    <span class="avatar">{{ card.assignee_initials }}</span>
                                                                    <span class="assignee-text">{{ card.assignee }}</span>
                                                                </div>
                                                            </a>
                                                        </article>
                                                    {% endfor %}
                                                </div>
                                            {% endfor %}
                                        </div>
                                    {% else %}
                                        <div class="empty-state">
                                            <div class="empty-state-icon">
                                                <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                                    <path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/>
                                                </svg>
                                            </div>
                                            <p>No work packages assigned to {{ selected_member_name }}.</p>
                                        </div>
                                    {% endif %}

                                    {% if member_gantt %}
                                        <div style="margin-top: 24px;">
                                            <h4 style="font-size: 1rem; font-weight: 600; margin-bottom: 12px;">Timeline</h4>
                                            <div class="gantt-chart" id="member-gantt-chart"></div>
                                        </div>
                                        <script>
                                            (function() {
                                                var mg = {{ member_gantt | tojson }};
                                                if (mg && mg.length > 0 && typeof Gantt !== 'undefined') {
                                                    new Gantt('#member-gantt-chart', mg, {
                                                        view_mode: 'Day',
                                                        date_format: 'YYYY-MM-DD',
                                                        popup_trigger: 'click',
                                                        custom_popup_html: function(task) { return '<div class="gantt-popup"><h5>'+task.name+'</h5><p>Start: '+task.start+'<br>End: '+task.end+'</p></div>'; },
                                                    });
                                                }
                                            })();
                                        </script>
                                    {% endif %}
                                {% else %}
                                    <div class="team-grid">
                                        {% for member in team_members %}
                                            <a class="member-card" href="/?project_id={{ selected_project_id }}&view=team&member_name={{ member.name|urlencode }}{% if project_search %}&project_search={{ project_search|urlencode }}{% endif %}">
                                                <div class="member-avatar">{{ member.name[0] }}</div>
                                                <div class="member-info">
                                                    <div class="member-name">{{ member.name }}</div>
                                                    <div class="member-count">{{ member.task_count }} task{{ 's' if member.task_count != 1 }}</div>
                                                </div>
                                            </a>
                                        {% else %}
                                            <div class="empty-state" style="grid-column: 1 / -1;">
                                                <p>No members found for this project.</p>
                                            </div>
                                        {% endfor %}
                                    </div>
                                {% endif %}
                            </div>
                        </div>
                    </div>
                {% elif mode == 'users' and selected_user_id %}
                    <div class="page-header">
                        <h2>{{ user_name }}</h2>
                        <p>{{ user_email }} {% if user_admin %}<span class="admin-badge">Admin</span>{% endif %}</p>
                    </div>

                    <div class="stats-grid">
                        <div class="stat-card purple">
                            <div class="stat-card-header">
                                <span class="stat-card-label">Total Tasks</span>
                                <div class="stat-card-icon">
                                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                        <path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/>
                                    </svg>
                                </div>
                            </div>
                            <div class="stat-card-value">{{ user_total }}</div>
                            <div class="stat-card-sub">Across {{ user_projects|length }} project{{ 's' if user_projects|length != 1 }}</div>
                        </div>

                        <div class="stat-card green">
                            <div class="stat-card-header">
                                <span class="stat-card-label">Projects</span>
                                <div class="stat-card-icon">
                                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                        <path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/>
                                    </svg>
                                </div>
                            </div>
                            <div class="stat-card-value">{{ user_projects|length }}</div>
                            <div class="stat-card-sub">Active participation</div>
                        </div>

                        <div class="stat-card orange">
                            <div class="stat-card-header">
                                <span class="stat-card-label">User</span>
                                <div class="stat-card-icon">
                                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                        <path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/><circle cx="12" cy="7" r="4"/>
                                    </svg>
                                </div>
                            </div>
                            <div class="stat-card-value" style="font-size: 1rem;">{{ user_login }}</div>
                            <div class="stat-card-sub">{{ user_email }}</div>
                        </div>

                        <div class="stat-card cyan">
                            <div class="stat-card-header">
                                <span class="stat-card-label">View</span>
                                <div class="stat-card-icon">
                                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                        <rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>
                                    </svg>
                                </div>
                            </div>
                            <div class="stat-card-value" style="font-size: 1.2rem;">{{ view|title }}</div>
                            <div class="stat-card-sub">Switch in tabs below</div>
                        </div>
                    </div>

                    <div class="card">
                        <div class="card-header">
                            <div class="tabs">
                                <button class="tab {% if view == 'kanban' %}active{% endif %}" onclick="switchView('kanban')">Kanban</button>
                                <button class="tab {% if view == 'gantt' %}active{% endif %}" onclick="switchView('gantt')">Gantt</button>
                                <button class="tab {% if view == 'projects' %}active{% endif %}" onclick="switchView('projects')">Projects</button>
                            </div>
                            <div style="display: flex; gap: 8px; align-items: center;">
                                {% if active_filters_count > 0 %}
                                    <span class="badge badge-accent">{{ active_filters_count }} filter{{ 's' if active_filters_count > 1 }} active</span>
                                {% endif %}
                                <a class="btn btn-ghost" href="/?mode=users{% if user_search %}&user_search={{ user_search|urlencode }}{% endif %}{% if view %}&view={{ view }}{% endif %}">Back to users</a>
                            </div>
                        </div>

                        <form method="get" id="filterForm">
                            <input type="hidden" name="mode" value="users" />
                            <input type="hidden" name="user_id" value="{{ selected_user_id }}" />
                            <input type="hidden" name="view" value="{{ view }}" id="viewInput" />
                            {% if user_search %}
                                <input type="hidden" name="user_search" value="{{ user_search }}" />
                            {% endif %}

                            <div class="filter-advanced" id="filterAdvanced">
                                <div class="filter-section">
                                    <span class="filter-section-title">Status</span>
                                    <div class="checkbox-group">
                                        {% for status in filter_options.statuses %}
                                            <label class="checkbox-label">
                                                <input type="checkbox" name="status" value="{{ status }}" {% if status in status_filter %}checked{% endif %} onchange="autoSubmitFilters()" />
                                                <span>{{ status }}</span>
                                            </label>
                                        {% endfor %}
                                    </div>
                                </div>

                                <div class="filter-section">
                                    <span class="filter-section-title">Priority</span>
                                    <div class="checkbox-group">
                                        {% for priority in filter_options.priorities %}
                                            <label class="checkbox-label">
                                                <input type="checkbox" name="priority" value="{{ priority }}" {% if priority == priority_filter %}checked{% endif %} onchange="autoSubmitFilters()" />
                                                <span>{{ priority }}</span>
                                            </label>
                                        {% endfor %}
                                    </div>
                                </div>

                                <div class="filter-section">
                                    <span class="filter-section-title">Type</span>
                                    <div class="checkbox-group">
                                        {% for type in filter_options.types %}
                                            <label class="checkbox-label">
                                                <input type="checkbox" name="type" value="{{ type }}" {% if type == type_filter %}checked{% endif %} onchange="autoSubmitFilters()" />
                                                <span>{{ type }}</span>
                                            </label>
                                        {% endfor %}
                                    </div>
                                </div>

                                <div class="filter-section">
                                    <span class="filter-section-title">Date Range</span>
                                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
                                        <div>
                                            <label style="font-size: 0.75rem; color: var(--ink-muted);">From</label>
                                            <input type="date" name="date_from" value="{{ date_from }}" class="filter-select" onchange="autoSubmitFilters()" />
                                        </div>
                                        <div>
                                            <label style="font-size: 0.75rem; color: var(--ink-muted);">To</label>
                                            <input type="date" name="date_to" value="{{ date_to }}" class="filter-select" onchange="autoSubmitFilters()" />
                                        </div>
                                    </div>
                                </div>

                                <div class="filter-section">
                                    <span class="filter-section-title">Sort By</span>
                                    <select name="sort" class="filter-select" onchange="autoSubmitFilters()">
                                        <option value="" {% if not sort_by %}selected{% endif %}>Default</option>
                                        <option value="due_date_asc" {% if sort_by == 'due_date_asc' %}selected{% endif %}>Due Date ↑</option>
                                        <option value="due_date_desc" {% if sort_by == 'due_date_desc' %}selected{% endif %}>Due Date ↓</option>
                                        <option value="priority" {% if sort_by == 'priority' %}selected{% endif %}>Priority</option>
                                        <option value="title" {% if sort_by == 'title' %}selected{% endif %}>Title</option>
                                    </select>
                                </div>
                            </div>
                        </form>

                        <div id="view-kanban" class="view-content {% if view == 'kanban' %}active{% endif %}">
                            {% if user_board %}
                                <div class="board">
                                    {% for column in user_status_columns %}
                                        <div class="kanban-column" style="--column-color: {{ column.color }};" data-status-id="{{ column.id }}" data-status-href="{{ column.href }}" data-status-name="{{ column.name }}" ondragover="handleDragOver(event)" ondragleave="handleDragLeave(event)" ondrop="handleDrop(event)">
                                            <div class="column-header">
                                                <span class="column-title">
                                                    <span class="column-dot" style="background: {{ column.color }};"></span>
                                                    {{ column.name }}
                                                </span>
                                                <span class="column-count">{{ user_board[column.name]|length }}</span>
                                            </div>
                                            {% for card in user_board[column.name] %}
                                                <article class="kanban-card {% if card.is_overdue %}overdue{% endif %}" style="--card-accent: {{ card.accent }};" draggable="true" ondragstart="handleDragStart(event, '{{ card.id }}', '{{ card.status }}')" ondragend="handleDragEnd(event)" data-card-id="{{ card.id }}">
                                                    <a class="kanban-card-link" href="javascript:void(0)" onclick='openTaskModal({{ card | tojson }})' style="cursor: pointer;">
                                                        <div style="display: flex; align-items: center; justify-content: space-between; gap: 8px;">
                                                            <span style="display: flex; align-items: center; gap: 6px; min-width: 0;">
                                                                <span class="drag-handle" onclick="event.preventDefault(); event.stopPropagation();">
                                                                    <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
                                                                        <circle cx="9" cy="6" r="2"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="9" cy="18" r="2"/><circle cx="15" cy="18" r="2"/>
                                                                    </svg>
                                                                </span>
                                                                <h4 class="kanban-card-title">#{{ card.id }} {{ card.title }}</h4>
                                                            </span>
                                                            {% if card.is_overdue %}
                                                                <span class="overdue-badge">Overdue</span>
                                                            {% endif %}
                                                        </div>
                                                        <div class="tag-row">
                                                            <span class="tag tag-type">{{ card.type }}</span>
                                                            {% set pk = card.priority|lower %}
                                                            {% if "high" in pk or "urgent" in pk or "immediate" in pk %}
                                                                <span class="tag tag-priority-high">{{ card.priority }}</span>
                                                            {% elif "low" in pk %}
                                                                <span class="tag tag-priority-low">{{ card.priority }}</span>
                                                            {% else %}
                                                                <span class="tag tag-priority-normal">{{ card.priority }}</span>
                                                            {% endif %}
                                                        </div>
                                                        <div class="kanban-dates">
                                                            <span>Start: {{ card.start_date or "—" }}</span>
                                                            <span>Due: {{ card.due_date or "—" }}</span>
                                                        </div>
                                                        <div class="progress-section">
                                                            <div class="progress-header">
                                                                <span>Progress</span>
                                                                <strong>{{ card.done }}%</strong>
                                                            </div>
                                                            <div class="progress-track">
                                                                <div class="progress-fill" style="width: {{ card.done }}%;"></div>
                                                            </div>
                                                        </div>
                                                        <div class="kanban-footer">
                                                            <span class="avatar">{{ card.assignee_initials }}</span>
                                                            <span class="assignee-text">{{ card.assignee }}</span>
                                                        </div>
                                                    </a>
                                                </article>
                                            {% endfor %}
                                        </div>
                                    {% endfor %}
                                </div>
                            {% else %}
                                <div class="empty-state">
                                    <div class="empty-state-icon">
                                        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                            <path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/>
                                        </svg>
                                    </div>
                                    <p>No work packages matched the current filters.</p>
                                </div>
                            {% endif %}
                        </div>

                        <div id="view-gantt" class="view-content {% if view == 'gantt' %}active{% endif %}">
                            {% if user_gantt %}
                                <div class="gantt-toolbar">
                                    <div class="gantt-stats">
                                        <span class="gantt-stat"><strong>{{ user_gantt|length }}</strong> tasks</span>
                                        {% set ug_with_dates = user_gantt | selectattr('start', 'defined') | list %}
                                        {% if ug_with_dates %}
                                            <span class="gantt-stat"><strong>{{ ug_with_dates[0].start }}</strong> to <strong>{{ ug_with_dates[-1].end }}</strong></span>
                                        {% endif %}
                                    </div>
                                    <div class="gantt-modes">
                                        <button class="gantt-mode" onclick="changeGanttMode('Quarter Day')">Quarter</button>
                                        <button class="gantt-mode" onclick="changeGanttMode('Half Day')">Half Day</button>
                                        <button class="gantt-mode active" onclick="changeGanttMode('Day')">Day</button>
                                        <button class="gantt-mode" onclick="changeGanttMode('Week')">Week</button>
                                        <button class="gantt-mode" onclick="changeGanttMode('Month')">Month</button>
                                    </div>
                                </div>
                                <div class="gantt-chart" id="gantt-chart"></div>
                            {% else %}
                                <div class="empty-state">
                                    <div class="empty-state-icon">
                                        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                            <rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>
                                        </svg>
                                    </div>
                                    <p>No tasks with date ranges available for the Gantt chart.</p>
                                </div>
                            {% endif %}
                        </div>

                        <div id="view-projects" class="view-content {% if view == 'projects' %}active{% endif %}">
                            {% if user_projects %}
                                <div class="team-grid">
                                    {% for p in user_projects %}
                                        <a class="member-card" href="/?mode=projects&project_id={{ p.id }}&view=kanban">
                                            <div class="member-avatar" style="background: #6366f1;">{{ p.name[0] }}</div>
                                            <div class="member-info">
                                                <div class="member-name">{{ p.name }}</div>
                                                <div class="member-count">{% if p.identifier %}{{ p.identifier }}{% else %}Project{% endif %}</div>
                                            </div>
                                        </a>
                                    {% endfor %}
                                </div>
                            {% else %}
                                <div class="empty-state">
                                    <p>No projects found for this user.</p>
                                </div>
                            {% endif %}
                        </div>
                    </div>
                {% elif mode == 'users' %}
                    <div class="page-header">
                        <h2>Users</h2>
                        <p>Browse all OpenProject users and their workloads</p>
                    </div>

                    <div class="card">
                        <div class="card-header">
                            <span>All Users ({{ user_list|length }})</span>
                            <div style="display: flex; gap: 8px; align-items: center;">
                                <button class="btn btn-secondary" id="toggleFilters" onclick="toggleFilters()">
                                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                        <path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z"/>
                                    </svg>
                                    Filters
                                </button>
                            </div>
                        </div>

                        <form method="get" id="filterForm">
                            <input type="hidden" name="mode" value="users" />
                            {% if view %}
                                <input type="hidden" name="view" value="{{ view }}" />
                            {% endif %}

                            <div class="filter-advanced" id="filterAdvanced">
                                <div class="filter-section">
                                    <span class="filter-section-title">Search</span>
                                    <div class="sidebar-search" style="padding: 0;">
                                        <input name="user_search" value="{{ user_search }}" placeholder="Type to search..." style="width: 100%; padding: 10px 14px; border-radius: var(--radius-sm); border: 1px solid var(--line-light); background: var(--content-bg); color: var(--text-primary); font-size: 0.85rem; outline: none;" oninput="autoSubmitFiltersDebounce()" />
                                    </div>
                                </div>

                                <div class="filter-section">
                                    <span class="filter-section-title">Role</span>
                                    <select name="role_filter" class="filter-select" onchange="autoSubmitFilters()">
                                        <option value="">All roles</option>
                                        <option value="admin" {% if request.args.get('role_filter') == 'admin' %}selected{% endif %}>Admin only</option>
                                        <option value="member" {% if request.args.get('role_filter') == 'member' %}selected{% endif %}>Members only</option>
                                    </select>
                                </div>

                                <div class="filter-section">
                                    <span class="filter-section-title">Sort By</span>
                                    <select name="user_sort" class="filter-select" onchange="autoSubmitFilters()">
                                        <option value="name" {% if request.args.get('user_sort', 'name') == 'name' %}selected{% endif %}>Name A-Z</option>
                                        <option value="name_desc" {% if request.args.get('user_sort') == 'name_desc' %}selected{% endif %}>Name Z-A</option>
                                        <option value="login" {% if request.args.get('user_sort') == 'login' %}selected{% endif %}>Login</option>
                                    </select>
                                </div>
                            </div>
                        </form>

                        <div class="card-body">
                            {% if user_list %}
                                <div class="user-grid">
                                    {% for u in user_list %}
                                        <a class="user-card" href="/?mode=users&user_id={{ u.id }}{% if user_search %}&user_search={{ user_search|urlencode }}{% endif %}">
                                            <div class="user-card-avatar">{{ u.name[0] }}</div>
                                            <div class="user-card-info">
                                                <div class="user-card-name">{{ u.name }}{% if u.admin %} <span class="admin-badge">Admin</span>{% endif %}</div>
                                                <div class="user-card-login">{{ u.login }}</div>
                                                <div class="user-card-email">{{ u.email }}</div>
                                            </div>
                                        </a>
                                    {% endfor %}
                                </div>
                            {% else %}
                                <div class="empty-state">
                                    <div class="empty-state-icon">
                                        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                            <path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/>
                                        </svg>
                                    </div>
                                    <p>No users found.</p>
                                </div>
                            {% endif %}
                        </div>
                    </div>
                {% else %}
                    <div class="page-header">
                        <h2>Welcome</h2>
                        <p>Select a project from the sidebar to get started.</p>
                    </div>
                    <div class="card">
                        <div class="card-body">
                            <div class="empty-state">
                                <div class="empty-state-icon">
                                    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                        <path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/>
                                    </svg>
                                </div>
                                <p>Choose a project to view its kanban board and Gantt timeline.</p>
                            </div>
                        </div>
                    </div>
                {% endif %}
            </main>
        </div>
    </div>

    <div class="drag-toast" id="dragToast"></div>

    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/frappe-gantt/0.6.1/frappe-gantt.min.js"></script>
    <div class="modal-overlay" id="taskModal" onclick="closeTaskModal(event)">
        <div class="modal" onclick="event.stopPropagation()">
            <div class="modal-header">
                <div>
                    <h3 id="modalTitle"></h3>
                    <div class="modal-header-sub" id="modalSubtitle"></div>
                </div>
                <button class="modal-close" onclick="closeTaskModal()">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M18 6L6 18M6 6l12 12"/>
                    </svg>
                </button>
            </div>
            <div class="modal-body" id="modalBody"></div>
            <div class="modal-actions">
                <a class="btn btn-primary" id="modalOpenLink" href="#" target="_blank" rel="noreferrer">Open in OpenProject</a>
            </div>
        </div>
    </div>
    <script>
        var ganttData = {{ gantt | tojson }};
        {% if selected_user_id and user_gantt %}
            if (ganttData.length === 0 && {{ user_gantt | length }} > 0) {
                ganttData = {{ user_gantt | tojson }};
            }
        {% endif %}
        var chartData = {{ chart_data | tojson }};
        var gantt = null;
        var sidebarOpen = {% if project or selected_user_id %}true{% else %}false{% endif %};

        function toggleSidebar() {
            var sidebar = document.getElementById('sidebar');
            var mainWrapper = document.getElementById('mainWrapper');
            var overlay = document.getElementById('sidebarOverlay');
            sidebarOpen = !sidebarOpen;

            if (window.innerWidth <= 1024) {
                sidebar.classList.toggle('mobile-open');
                overlay.classList.toggle('visible');
            } else {
                sidebar.classList.toggle('collapsed');
                mainWrapper.classList.toggle('expanded');
            }
        }

        function toggleFilters() {
            document.getElementById('filterAdvanced').classList.toggle('visible');
        }

        function resetFilters() {
            var form = document.getElementById('filterForm');
            var projectId = form.querySelector('input[name="project_id"]').value;
            var view = document.getElementById('viewInput').value;
            window.location.href = '/?project_id=' + projectId + '&view=' + view;
        }

        var filterSubmitTimeout = null;

        function autoSubmitFilters() {
            document.getElementById('filterForm').submit();
        }

        function autoSubmitFiltersDebounce() {
            if (filterSubmitTimeout) clearTimeout(filterSubmitTimeout);
            filterSubmitTimeout = setTimeout(function() {
                document.getElementById('filterForm').submit();
            }, 400);
        }

        var draggedCardId = null;
        var draggedStatusName = null;
        var toastTimeout = null;

        function handleDragStart(e, cardId, statusName) {
            draggedCardId = cardId;
            draggedStatusName = statusName;
            e.target.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', cardId);
        }

        function handleDragEnd(e) {
            e.target.classList.remove('dragging');
            document.querySelectorAll('.kanban-column').forEach(function(col) {
                col.classList.remove('drag-active');
            });
        }

        function handleDragOver(e) {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            var column = e.currentTarget;
            if (column !== draggedCardId) {
                column.classList.add('drag-active');
            }
        }

        function handleDragLeave(e) {
            var column = e.currentTarget;
            if (!column.contains(e.relatedTarget)) {
                column.classList.remove('drag-active');
            }
        }

        function handleDrop(e) {
            e.preventDefault();
            var column = e.currentTarget;
            column.classList.remove('drag-active');

            var targetStatusId = column.getAttribute('data-status-id');
            var targetStatusHref = column.getAttribute('data-status-href');
            var targetStatusName = column.getAttribute('data-status-name');

            if (!draggedCardId || !targetStatusHref) return;

            var sourceCard = document.querySelector('[data-card-id="' + draggedCardId + '"]');
            if (sourceCard) {
                var targetColumn = e.currentTarget;

                if (draggedStatusName !== targetStatusName) {
                    updateTaskStatus(draggedCardId, targetStatusId, targetStatusHref, targetStatusName, sourceCard, targetColumn, 0);
                }
            }
        }

        function updateTaskStatus(cardId, statusId, statusHref, statusName, cardEl, targetColumn, insertIndex) {
            cardEl.style.opacity = '0.5';
            cardEl.style.pointerEvents = 'none';
            showDragToast('Updating status...', 'info');

            fetch('/api/work-packages/' + cardId + '/status', {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ status_href: statusHref })
            })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.success) {
                    showDragToast('Moved to ' + statusName, 'success');
                    setTimeout(function() { location.reload(); }, 800);
                } else if (data.error && data.error.includes('Cannot transition')) {
                    showDragToast('Workflow blocked: ' + data.error, 'error');
                    cardEl.style.opacity = '1';
                    cardEl.style.pointerEvents = 'auto';
                } else {
                    showDragToast('Error: ' + (data.error || 'Unknown'), 'error');
                    cardEl.style.opacity = '1';
                    cardEl.style.pointerEvents = 'auto';
                }
            })
            .catch(function(err) {
                showDragToast('Network error', 'error');
                cardEl.style.opacity = '1';
                cardEl.style.pointerEvents = 'auto';
            });
        }

        function showDragToast(message, type) {
            var toast = document.getElementById('dragToast');
            if (!toast) return;
            toast.textContent = message;
            toast.className = 'drag-toast visible ' + (type === 'success' ? 'success' : type === 'error' ? 'error' : '');
            clearTimeout(toastTimeout);
            toastTimeout = setTimeout(function() {
                toast.classList.remove('visible');
            }, 2500);
        }

        function switchView(viewName) {
            document.querySelectorAll('.view-content').forEach(function(el) { el.classList.remove('active'); });
            document.querySelectorAll('.tab').forEach(function(el) { el.classList.remove('active'); });
            document.getElementById('view-' + viewName).classList.add('active');
            event.target.classList.add('active');
            document.getElementById('viewInput').value = viewName;
            if (viewName === 'gantt' && !gantt) {
                setTimeout(initGantt, 100);
            }
        }

        function changeGanttMode(mode) {
            document.querySelectorAll('.gantt-mode').forEach(function(el) { el.classList.remove('active'); });
            event.target.classList.add('active');
            if (gantt) gantt.change_view_mode(mode);
        }

        function initGantt() {
            var container = document.getElementById('gantt-chart');
            if (!container || !ganttData || ganttData.length === 0) return;
            if (gantt) container.innerHTML = '';
            gantt = new Gantt("#gantt-chart", ganttData, {
                view_mode: "Day",
                date_format: "YYYY-MM-DD",
                custom_popup_html: function(task) {
                    return '<div style="padding: 12px; min-width: 200px;">' +
                        '<h5 style="margin: 0 0 8px; font-size: 13px;">' + task.name + '</h5>' +
                        '<p style="margin: 3px 0; font-size: 12px; color: #64748b;">Status: ' + task._status + '</p>' +
                        '<p style="margin: 3px 0; font-size: 12px; color: #64748b;">Priority: ' + task._priority + '</p>' +
                        '<p style="margin: 3px 0; font-size: 12px; color: #64748b;">Type: ' + task._type + '</p>' +
                        '<p style="margin: 3px 0; font-size: 12px; color: #64748b;">Assignee: ' + task._assignee + '</p>' +
                        '<p style="margin: 3px 0; font-size: 12px; color: #64748b;">Progress: ' + task.progress + '%</p>' +
                        '</div>';
                }
            });
            container.addEventListener('click', function(e) {
                var bar = e.target.closest('.bar-wrapper');
                if (bar) {
                    var taskId = bar.getAttribute('data-id');
                    var task = ganttData.find(function(t) { return t.id === taskId; });
                    if (task && task._href) window.open(task._href, '_blank');
                }
            });
        }

        function openTaskModal(card) {
            var modal = document.getElementById('taskModal');
            document.getElementById('modalTitle').textContent = '#' + card.id + ' ' + card.title;
            document.getElementById('modalSubtitle').textContent = card.type;
            document.getElementById('modalOpenLink').href = card.href;

            var pk = card.priority.toLowerCase();
            var priorityClass = 'tag-priority-normal';
            if (pk.includes('high') || pk.includes('urgent') || pk.includes('immediate')) priorityClass = 'tag-priority-high';
            else if (pk.includes('low')) priorityClass = 'tag-priority-low';

            var body = document.getElementById('modalBody');
            body.innerHTML =
                '<div class="modal-tags">' +
                    '<span class="tag tag-type">' + card.type + '</span>' +
                    '<span class="tag ' + priorityClass + '">' + card.priority + '</span>' +
                '</div>' +
                '<div class="modal-field" style="margin-top: 16px;">' +
                    '<div class="modal-field-label">Status</div>' +
                    '<div class="modal-field-value">' + card.status + '</div>' +
                '</div>' +
                '<div class="modal-field">' +
                    '<div class="modal-field-label">Assignee</div>' +
                    '<div class="modal-field-value">' + card.assignee + '</div>' +
                '</div>' +
                '<div class="modal-field">' +
                    '<div class="modal-field-label">Author</div>' +
                    '<div class="modal-field-value">' + card.author + '</div>' +
                '</div>' +
                '<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px;">' +
                    '<div class="modal-field">' +
                        '<div class="modal-field-label">Start Date</div>' +
                        '<div class="modal-field-value">' + (card.start_date || '—') + '</div>' +
                    '</div>' +
                    '<div class="modal-field">' +
                        '<div class="modal-field-label">Due Date</div>' +
                        '<div class="modal-field-value">' + (card.due_date || '—') + '</div>' +
                    '</div>' +
                '</div>' +
                '<div class="modal-field">' +
                    '<div class="modal-field-label">Estimated Time</div>' +
                    '<div class="modal-field-value">' + (card.estimated_time || '—') + '</div>' +
                '</div>' +
                '<div class="modal-field">' +
                    '<div class="modal-field-label">Remaining Time</div>' +
                    '<div class="modal-field-value">' + (card.remaining_time || '—') + '</div>' +
                '</div>' +
                '<div class="modal-progress">' +
                    '<div class="modal-progress-header">' +
                        '<span>Progress</span>' +
                        '<strong>' + card.done + '%</strong>' +
                    '</div>' +
                    '<div class="modal-progress-track">' +
                        '<div class="modal-progress-fill" style="width: ' + card.done + '%;"></div>' +
                    '</div>' +
                '</div>';

            modal.classList.add('visible');
        }

        function closeTaskModal(e) {
            if (e && e.target !== e.currentTarget) return;
            document.getElementById('taskModal').classList.remove('visible');
        }

        function initCharts() {
            if (!chartData || !chartData.status.length) return;
            var textColor = '#94a3b8';
            var fontFamily = '-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif';

            new Chart(document.getElementById('statusChart'), {
                type: 'doughnut',
                data: {
                    labels: chartData.status.map(function(s) { return s.label; }),
                    datasets: [{
                        data: chartData.status.map(function(s) { return s.count; }),
                        backgroundColor: chartData.status.map(function(s) { return s.color; }),
                        borderWidth: 0,
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { position: 'bottom', labels: { color: textColor, padding: 12, usePointStyle: true, pointStyleWidth: 8, font: { size: 11, family: fontFamily } } }
                    },
                    cutout: '65%',
                }
            });

            new Chart(document.getElementById('priorityChart'), {
                type: 'bar',
                data: {
                    labels: chartData.priority.map(function(p) { return p.label; }),
                    datasets: [{
                        data: chartData.priority.map(function(p) { return p.count; }),
                        backgroundColor: ['#ef4444', '#f59e0b', '#6366f1', '#10b981'],
                        borderRadius: 6,
                        borderSkipped: false,
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { display: false } },
                    scales: {
                        x: { grid: { display: false }, ticks: { color: textColor, font: { size: 11, family: fontFamily } } },
                        y: { grid: { color: '#f1f5f9' }, ticks: { color: textColor, font: { size: 11, family: fontFamily }, stepSize: 1 }, beginAtZero: true }
                    }
                }
            });

            new Chart(document.getElementById('workloadChart'), {
                type: 'bar',
                data: {
                    labels: chartData.workload.map(function(w) { return w.label.length > 12 ? w.label.slice(0, 12) + '...' : w.label; }),
                    datasets: [{
                        data: chartData.workload.map(function(w) { return w.count; }),
                        backgroundColor: '#6366f1',
                        borderRadius: 6,
                        borderSkipped: false,
                    }]
                },
                options: {
                    indexAxis: 'y',
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { display: false } },
                    scales: {
                        x: { grid: { display: false }, ticks: { color: textColor, font: { size: 11, family: fontFamily }, stepSize: 1 }, beginAtZero: true },
                        y: { grid: { display: false }, ticks: { color: textColor, font: { size: 11, family: fontFamily } } }
                    }
                }
            });
        }

        document.addEventListener('DOMContentLoaded', function() {
            initCharts();
            if (document.getElementById('view-gantt').classList.contains('active')) {
                setTimeout(initGantt, 200);
            }
        });
    </script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(debug=True, host=APP_HOST, port=APP_PORT)
