import json
import threading

from cache import cache
from openproject_connector import OpenProjectConnector

WORK_PACKAGE_LIMIT = 500
PROJECTS_TTL = 600
STATUSES_TTL = 3600
MEMBERS_TTL = 300
TASKS_TTL = 120
USERS_TTL = 600


class ProjectClient:
    def __init__(self, connector: OpenProjectConnector):
        self.connector = connector

    def _paginate(self, endpoint: str, limit: int | None = None):
        items = []
        count = 0

        while endpoint:
            data = self.connector.get(endpoint)
            elements = data.get("_embedded", {}).get("elements", [])
            items.extend(elements)
            count += len(elements)

            if limit and count >= limit:
                break

            next_link = data.get("_links", {}).get("nextByOffset", {}).get("href")
            endpoint = next_link if next_link else None

        return items[:limit] if limit else items

    def get_all_projects(self):
        cached = cache.get("projects_list")
        if cached is not None:
            return cached

        projects = self._paginate("/projects")
        cache.set("projects_list", projects, ttl=PROJECTS_TTL)
        return projects

    def get_project_by_id(self, project_id: int):
        cache_key = f"project_{project_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        project = self.connector.get(f"/projects/{project_id}")
        cache.set(cache_key, project, ttl=PROJECTS_TTL)
        return project

    def get_statuses(self):
        cached = cache.get("statuses_global")
        if cached is not None:
            return cached

        statuses = self._paginate("/statuses")
        cache.set("statuses_global", statuses, ttl=STATUSES_TTL)
        return statuses

    def get_work_packages(self, project_id: int, assignee_id: int | None = None):
        cache_key = f"tasks_{project_id}_{assignee_id or 'all'}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        filters = []
        if assignee_id is not None:
            filters.append(
                {
                    "assigned_to": {
                        "operator": "=",
                        "values": [str(assignee_id)],
                    }
                }
            )

        filters_param = json.dumps(filters) if filters else ""
        query = f"perPage=100"
        if filters_param:
            query += f"&filters={filters_param}"

        tasks = self._paginate(f"/projects/{project_id}/work_packages?{query}", limit=WORK_PACKAGE_LIMIT)
        cache.set(cache_key, tasks, ttl=TASKS_TTL)
        return tasks

    def get_project_members(self, project_id: int):
        cache_key = f"members_{project_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        filters = json.dumps([
            {
                "project": {
                    "operator": "=",
                    "values": [str(project_id)]
                }
            }
        ])

        members = self._paginate(f"/memberships?filters={filters}")
        cache.set(cache_key, members, ttl=MEMBERS_TTL)
        return members

    def get_full_project(self, project_id: int, assignee_id: int | None = None):
        results = {}
        errors = []

        def fetch(key, fn):
            try:
                results[key] = fn()
            except Exception as e:
                errors.append(f"{key}: {e}")

        threads = [
            threading.Thread(target=fetch, args=("project", lambda: self.get_project_by_id(project_id))),
            threading.Thread(target=fetch, args=("statuses", self.get_statuses)),
            threading.Thread(target=fetch, args=("tasks", lambda: self.get_work_packages(project_id, assignee_id=assignee_id))),
            threading.Thread(target=fetch, args=("members", lambda: self.get_project_members(project_id))),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if errors:
            import logging
            logging.warning("Partial failures in get_full_project: %s", errors)

        return {
            "project": results.get("project", {}),
            "statuses": results.get("statuses", []),
            "tasks": results.get("tasks", []),
            "members": results.get("members", []),
        }

    def get_all_users(self):
        cache_key = "all_users"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        users = self._paginate("/users")
        cache.set(cache_key, users, ttl=USERS_TTL)
        return users

    def get_user_work_packages(self, user_id: int):
        cache_key = f"user_tasks_{user_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        filters = json.dumps([
            {"assignee": {"operator": "=", "values": [str(user_id)]}}
        ])
        tasks = self._paginate(f"/work_packages?perPage=100&filters={filters}", limit=WORK_PACKAGE_LIMIT)
        cache.set(cache_key, tasks, ttl=TASKS_TTL)
        return tasks

    def get_full_user(self, user_id: int):
        results = {}
        errors = []

        def fetch(key, fn):
            try:
                results[key] = fn()
            except Exception as e:
                errors.append(f"{key}: {e}")

        threads = [
            threading.Thread(target=fetch, args=("user", lambda: self.connector.get(f"/users/{user_id}"))),
            threading.Thread(target=fetch, args=("tasks", lambda: self.get_user_work_packages(user_id))),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if errors:
            import logging
            logging.warning("Partial failures in get_full_user: %s", errors)

        user_data = results.get("user", {})
        tasks = results.get("tasks", [])

        project_ids = set()
        for task in tasks:
            pid = task.get("_links", {}).get("project", {}).get("href", "")
            if pid:
                pid_str = pid.rstrip("/").split("/")[-1]
                try:
                    project_ids.add(int(pid_str))
                except ValueError:
                    pass

        project_summaries = []
        proj_threads = []
        proj_results = {}

        def fetch_proj(pid):
            try:
                cache_key = f"project_{pid}"
                cached = cache.get(cache_key)
                if cached is not None:
                    proj_results[pid] = {
                        "id": pid,
                        "name": cached.get("name", "Unknown"),
                        "identifier": cached.get("identifier", ""),
                        "status": cached.get("status", ""),
                    }
                else:
                    proj = self.connector.get(f"/projects/{pid}")
                    cache.set(cache_key, proj, ttl=PROJECTS_TTL)
                    proj_results[pid] = {
                        "id": pid,
                        "name": proj.get("name", "Unknown"),
                        "identifier": proj.get("identifier", ""),
                        "status": proj.get("status", ""),
                    }
            except Exception:
                proj_results[pid] = {"id": pid, "name": "Unknown", "identifier": "", "status": ""}

        for pid in sorted(project_ids):
            t = threading.Thread(target=fetch_proj, args=(pid,))
            proj_threads.append(t)

        for t in proj_threads:
            t.start()
        for t in proj_threads:
            t.join()

        for pid in sorted(project_ids):
            if pid in proj_results:
                project_summaries.append(proj_results[pid])

        return {
            "user": user_data,
            "tasks": tasks,
            "projects": project_summaries,
        }

    def invalidate_project_cache(self, project_id: int):
        cache.invalidate(f"project_{project_id}")
        cache.invalidate(f"tasks_{project_id}_all")
        cache.invalidate(f"tasks_{project_id}_None")
        cache.invalidate(f"members_{project_id}")
        for key in list(cache._store.keys()):
            if key.startswith(f"tasks_{project_id}_"):
                cache.invalidate(key)

    def get_available_status_transitions(self, work_package_id: int):
        wp = self.connector.get(f"/work_packages/{work_package_id}")
        status_links = wp.get("_links", {}).get("status", {})
        allowed_values = status_links.get("allowedValues", [])
        if not allowed_values:
            return []
        return [
            {
                "id": v.get("href", "").rstrip("/").split("/")[-1],
                "name": v.get("title", "Unknown"),
                "href": v.get("href"),
            }
            for v in allowed_values
        ]

    def update_work_package_status(self, work_package_id: int, status_href: str):
        payload = {
            "_links": {
                "status": {
                    "href": status_href
                }
            }
        }
        result = self.connector.patch(f"/work_packages/{work_package_id}", payload)
        return result
