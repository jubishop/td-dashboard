"""Read td-sync's data directory: projects, issues, activity, and status history, all read-only."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


STATUSES = ("in_progress", "in_review", "blocked", "open", "closed")
ACTIVE = STATUSES[:-1]


STATUS_LABELS = {
    "in_progress": "In progress",
    "in_review": "In review",
    "blocked": "Blocked",
    "open": "Open",
    "closed": "Closed",
}
PRIORITIES = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}


def parse_time(value: object) -> datetime | None:
    """Parse td's ISO timestamps; SQLite's naive CURRENT_TIMESTAMP values are UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    # Go's zero time and other placeholders cannot convert to local time, so treat them as unset.
    if not 1970 <= parsed.year <= 9000:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def as_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class Issue:
    project: str
    id: str
    title: str
    status: str
    type: str
    priority: str
    parent_id: str
    created: datetime | None
    updated: datetime | None
    closed: datetime | None

    @property
    def last_change(self) -> datetime | None:
        return max((t for t in (self.updated, self.closed, self.created) if t), default=None)


@dataclass(frozen=True)
class Activity:
    project: str
    issue_id: str
    when: datetime
    kind: str
    text: str
    details: tuple[tuple[str, tuple[str, ...]], ...] = ()


@dataclass
class Project:
    id: str
    slug: str
    name: str
    issues: list[Issue] = field(default_factory=list)
    activity: list[Activity] = field(default_factory=list)
    error: str = ""

    def issue(self, issue_id: str) -> Issue | None:
        return next((i for i in self.issues if i.id == issue_id), None)

    @property
    def last_activity(self) -> datetime | None:
        times = [a.when for a in self.activity[:1]] + [i.last_change for i in self.issues if i.last_change]
        return max(times, default=None)


@dataclass(frozen=True)
class Bucket:
    start: date
    end: date
    count: int


def read(path: Path, *statements: tuple[str, tuple]) -> list[list[sqlite3.Row]]:
    """Run statements in one short read-only transaction so they see one snapshot."""
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("begin")
        try:
            return [conn.execute(sql, params).fetchall() for sql, params in statements]
        finally:
            conn.execute("rollback")
    finally:
        conn.close()


def json_list(value: object) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        items = json.loads(value)
    except (TypeError, ValueError):
        return (str(value),)
    if isinstance(items, list):
        return tuple(str(item) for item in items if item)
    return (str(items),) if items else ()


class Store:
    """Loads projects from a td-sync data directory, reloading one only when its database changes."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self._cache: dict[str, tuple[tuple[int, int], Project]] = {}
        self._lock = threading.Lock()

    def project_rows(self) -> list[sqlite3.Row]:
        (rows,) = read(self.data_dir / "server.db",
                       ("select id, slug, name from projects where deleted_at is null order by name", ()))
        return rows

    def projects(self) -> list[Project]:
        return [self._load(row["id"], row["slug"] or row["id"], row["name"] or row["slug"] or row["id"])
                for row in self.project_rows()]

    def _load(self, project_id: str, slug: str, name: str) -> Project:
        path = self.data_dir / "projects" / project_id / "project.db"
        try:
            stat = path.stat()
        except OSError:
            # td-sync builds project.db on a project's first push, so a newly linked repository has none yet.
            if self.has_events(project_id):
                return Project(project_id, slug, name, error="td-sync has events but has not built project.db")
            return Project(project_id, slug, name)
        key = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._cache.get(project_id)
        if cached and cached[0] == key and cached[1].slug == slug and cached[1].name == name:
            return cached[1]
        try:
            project = load_project(path, project_id, slug, name)
        except sqlite3.Error as error:
            # After a crash mid-write, read-only opens fail until td-sync next opens the database and rolls back.
            if cached:
                return replace(cached[1], error=f"could not read project.db ({error}); showing the last copy read")
            return Project(project_id, slug, name, error=f"could not read project.db: {error}")
        with self._lock:
            self._cache[project_id] = (key, project)
        return project

    def has_events(self, project_id: str) -> bool:
        try:
            (rows,) = read(self.data_dir / "projects" / project_id / "events.db",
                           ("select exists(select 1 from events)", ()))
        except sqlite3.Error:
            return False
        return bool(rows[0][0])

    def issue_detail(self, project: Project, issue_id: str) -> dict | None:
        path = self.data_dir / "projects" / project.id / "project.db"
        issues, deps = read(
            path,
            ("select description, acceptance, points, labels from issues where id = ? and deleted_at is null",
             (issue_id,)),
            ("select issue_id, depends_on_id from issue_dependencies where issue_id = ? or depends_on_id = ?",
             (issue_id, issue_id)),
        )
        if not issues:
            return None
        row = issues[0]
        return {
            "description": row["description"] or "",
            "acceptance": row["acceptance"] or "",
            "points": as_int(row["points"]),
            "labels": [label.strip() for label in (row["labels"] or "").split(",") if label.strip()],
            "depends_on": [d["depends_on_id"] for d in deps if d["issue_id"] == issue_id],
            "blocks": [d["issue_id"] for d in deps if d["depends_on_id"] == issue_id],
            "status_changes": self.status_changes(project, issue_id),
        }

    def status_changes(self, project: Project, issue_id: str) -> list[Activity]:
        """Status transitions from the event log; repeated snapshots of one status collapse."""
        path = self.data_dir / "projects" / project.id / "events.db"
        try:
            (rows,) = read(path, ("select action_type, payload, client_timestamp, server_timestamp from events"
                                  " where entity_type = 'issues' and entity_id = ? order by server_seq", (issue_id,)))
        except sqlite3.Error:
            return []
        changes, previous = [], None
        for row in rows:
            when = parse_time(row["client_timestamp"]) or parse_time(row["server_timestamp"])
            if row["action_type"] == "soft_delete":
                if when:
                    changes.append(Activity(project.slug, issue_id, when, "status", "Deleted"))
                continue
            try:
                data = json.loads(row["payload"]).get("new_data") or {}
            except (TypeError, ValueError, AttributeError):
                continue
            snapshot = data.get("issue") if isinstance(data.get("issue"), dict) else data
            status = snapshot.get("status") if isinstance(snapshot, dict) else None
            if not status or status == previous:
                continue
            if previous is not None and when:
                changes.append(Activity(project.slug, issue_id, when, "status",
                                        f"{STATUS_LABELS.get(previous, previous)} → {STATUS_LABELS.get(status, status)}"))
            previous = status
        return changes

    def search(self, project: Project, text: str) -> set[str]:
        path = self.data_dir / "projects" / project.id / "project.db"
        like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        (rows,) = read(path, ("select id from issues where deleted_at is null and"
                              " (id like ?1 escape '\\' or title like ?1 escape '\\' or description like ?1 escape '\\'"
                              " or acceptance like ?1 escape '\\' or labels like ?1 escape '\\')", (like,)))
        return {row["id"] for row in rows}


def load_project(path: Path, project_id: str, slug: str, name: str) -> Project:
    issues, logs, handoffs, reviews, comments = read(
        path,
        ("select id, title, status, type, priority, parent_id, created_at, updated_at, closed_at"
         " from issues where deleted_at is null", ()),
        ("select issue_id, timestamp, type, message from logs", ()),
        ("select issue_id, timestamp, done, remaining, decisions, uncertain from handoffs", ()),
        ("select issue_id, created_at, decision, summary from issue_reviews", ()),
        ("select issue_id, created_at, text from comments", ()),
    )
    project = Project(project_id, slug, name)
    project.issues = [
        Issue(slug, row["id"], row["title"] or "", row["status"] or "open", row["type"] or "", row["priority"] or "",
              row["parent_id"] or "", parse_time(row["created_at"]), parse_time(row["updated_at"]),
              parse_time(row["closed_at"]) if row["status"] == "closed" else None)
        for row in issues
    ]
    activity = []
    for row in logs:
        when = parse_time(row["timestamp"])
        if when:
            activity.append(Activity(slug, row["issue_id"] or "", when, row["type"] or "progress", row["message"] or ""))
    for row in handoffs:
        when = parse_time(row["timestamp"])
        if when:
            sections = tuple((label, json_list(row[column])) for label, column in
                             (("Done", "done"), ("Remaining", "remaining"), ("Decisions", "decisions"),
                              ("Uncertain", "uncertain")) if json_list(row[column]))
            summary = next((items[0] for _, items in sections), "")
            activity.append(Activity(slug, row["issue_id"] or "", when, "handoff", summary, sections))
    for row in reviews:
        when = parse_time(row["created_at"])
        if when:
            decision = (row["decision"] or "").replace("_", " ")
            activity.append(Activity(slug, row["issue_id"] or "", when, "review",
                                     f"{decision.capitalize()}: {row['summary'] or ''}".rstrip(": ")))
    for row in comments:
        when = parse_time(row["created_at"])
        if when:
            activity.append(Activity(slug, row["issue_id"] or "", when, "comment", row["text"] or ""))
    project.activity = sorted(activity, key=lambda a: a.when, reverse=True)
    return project


def counts(issues: list[Issue]) -> dict[str, int]:
    result = dict.fromkeys(STATUSES, 0)
    for issue in issues:
        result[issue.status if issue.status in result else "open"] += 1
    return result


def bucket_spans(days: int, today: date) -> list[tuple[date, date]]:
    """Daily buckets for short ranges; weekly buckets (ending today) for longer ones."""
    step = 1 if days <= 30 else 7
    first = today - timedelta(days=days - 1)
    spans = []
    end = today
    while end >= first:
        start = max(first, end - timedelta(days=step - 1))
        spans.append((start, end))
        end = start - timedelta(days=1)
    return spans[::-1]


def bucketize(times: list[datetime], days: int, now: datetime, tz: ZoneInfo) -> list[Bucket]:
    spans = bucket_spans(days, now.astimezone(tz).date())
    local = sorted(t.astimezone(tz).date() for t in times)
    return [Bucket(start, end, sum(start <= d <= end for d in local)) for start, end in spans]


def range_start(days: int, now: datetime, tz: ZoneInfo) -> datetime:
    first = now.astimezone(tz).date() - timedelta(days=days - 1)
    return datetime.combine(first, datetime.min.time(), tz)


def active_sort_key(issue: Issue) -> tuple:
    stamp = issue.last_change.timestamp() if issue.last_change else 0
    return (ACTIVE.index(issue.status) if issue.status in ACTIVE else len(ACTIVE),
            PRIORITIES.get(issue.priority, 9), -stamp)
