#!/usr/bin/env python3
"""Serve a read-only web dashboard of every td project that td-sync holds.

td-sync keeps each project's materialized td database at
<data-dir>/projects/<id>/project.db, its event log beside it in events.db, and
the project names in <data-dir>/server.db. Every read opens SQLite read-only
and holds its shared lock for a few milliseconds; td-sync's writers wait up to
five seconds for a lock, so the dashboard never makes a push fail.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import ipaddress
import json
import re
import socket
import sqlite3
import sys
import threading
import time
import traceback
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit
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
RANGES = (7, 30, 90)
DEFAULT_RANGE = 30
REFRESH_SECONDS = 60
SYNC_HEALTH_TTL = 15
# Host headers the dashboard answers besides IP literals; any other name could be DNS rebinding.
# A deployment names its own hosts with --hosts.
DEFAULT_HOSTS = ("localhost",)
MINIMUM_PYTHON = (3, 12)
URL_PATTERN = re.compile(r"https?://[^\s<>\"'`]+[^\s<>\"'`.,;:!?)\]}]")


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


# Rendering ----------------------------------------------------------------------------------------

esc = html.escape


class Clock:
    def __init__(self, tz: ZoneInfo, now: datetime) -> None:
        self.tz, self.now = tz, now

    def full(self, when: datetime) -> str:
        local = when.astimezone(self.tz)
        return f"{local:%a %b} {local.day}, {local.year} at {local:%I:%M %p %Z}".replace(" 0", " ")

    def short(self, when: datetime) -> str:
        local = when.astimezone(self.tz)
        return f"{local:%b} {local.day}, {local:%I:%M %p}".replace(", 0", ", ")

    def relative(self, when: datetime | None) -> str:
        if not when:
            return '<span class="muted">—</span>'
        seconds = (self.now - when).total_seconds()
        local, today = when.astimezone(self.tz), self.now.astimezone(self.tz).date()
        if seconds < 60:
            text = "just now"
        elif seconds < 3600:
            text = f"{int(seconds // 60)}m ago"
        elif seconds < 86400 and local.date() == today:
            text = f"{int(seconds // 3600)}h ago"
        elif local.date() == today - timedelta(days=1):
            text = "yesterday"
        elif local.year == today.year:
            text = f"{local:%b} {local.day}"
        else:
            text = f"{local:%b} {local.day}, {local.year}"
        return f'<time datetime="{when.isoformat()}" title="{esc(self.full(when))}">{esc(text)}</time>'


def linkify(text: str) -> str:
    out, position = [], 0
    for match in URL_PATTERN.finditer(text):
        out.append(esc(text[position:match.start()]))
        url = match.group(0)
        out.append(f'<a href="{esc(url)}" rel="noreferrer noopener" target="_blank">{esc(url)}</a>')
        position = match.end()
    out.append(esc(text[position:]))
    return "".join(out)


def truncate(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def status_chip(status: str) -> str:
    key = status if status in STATUS_LABELS else "open"
    return f'<span class="chip s-{key}"><span class="dot" aria-hidden="true"></span>{esc(STATUS_LABELS.get(status, status))}</span>'


def priority_text(priority: str) -> str:
    level = PRIORITIES.get(priority, 9)
    css = "p-high" if level <= 1 else "p-low" if level >= 3 else "p-mid"
    return f'<span class="{css}">{esc(priority or "—")}</span>'


def issue_href(project: str, issue_id: str) -> str:
    return f"/r/{quote(project, safe='')}/{quote(issue_id, safe='')}"


def repo_href(project: str, days: int | None = None) -> str:
    suffix = f"?{urlencode({'range': days})}" if days and days != DEFAULT_RANGE else ""
    return f"/r/{quote(project, safe='')}{suffix}"


def issue_link(issue: Issue) -> str:
    return (f'<a class="issue" href="{issue_href(issue.project, issue.id)}"><span class="id">{esc(issue.id)}</span> '
            f'{esc(issue.title)}</a>')


def nice_ceiling(value: int) -> int:
    if value <= 1:
        return 1
    magnitude = 10 ** (len(str(value)) - 1)
    for step in (1, 2, 5, 10):
        if step * magnitude >= value:
            return step * magnitude
    return 10 * magnitude


def bucket_label(bucket: Bucket) -> str:
    if bucket.start == bucket.end:
        return f"{bucket.start:%b} {bucket.start.day}"
    if bucket.start.month == bucket.end.month:
        return f"{bucket.start:%b} {bucket.start.day}–{bucket.end.day}"
    return f"{bucket.start:%b} {bucket.start.day} – {bucket.end:%b} {bucket.end.day}"


def column_chart(buckets: list[Bucket], noun: str, label: str) -> str:
    """A single-series column chart with a per-column hover and focus tooltip."""
    width, height, left, right, top, bottom = 720, 180, 44, 8, 18, 30
    plot_w, plot_h = width - left - right, height - top - bottom
    ceiling = nice_ceiling(max((b.count for b in buckets), default=0))
    band = plot_w / max(len(buckets), 1)
    bar_w = max(2.0, min(24.0, band - 2))
    base = top + plot_h
    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{esc(label)}">']
    ticks = sorted({0, ceiling} | ({ceiling // 2} if ceiling % 2 == 0 and ceiling >= 2 else set()))
    for tick in ticks:
        y = base - tick / ceiling * plot_h
        parts.append(f'<line class="{"axis" if tick == 0 else "grid"}" x1="{left}" x2="{width - right}" '
                     f'y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{tick}</text>')
    peak = max(range(len(buckets)), key=lambda i: buckets[i].count, default=None)
    for i, bucket in enumerate(buckets):
        x0 = left + i * band
        x = x0 + (band - bar_w) / 2
        h = bucket.count / ceiling * plot_h
        tip_value = f"{bucket.count} {noun}"
        parts.append(f'<g class="col" tabindex="0" data-tip-value="{esc(tip_value)}" '
                     f'data-tip-label="{esc(bucket_label(bucket))}" aria-label="{esc(bucket_label(bucket))}: {esc(tip_value)}">')
        parts.append(f'<rect class="hit" x="{x0:.1f}" y="{top}" width="{band:.1f}" height="{plot_h}"/>')
        if h > 0:
            r = min(4.0, bar_w / 2, h)
            y = base - h
            parts.append(f'<path class="bar" d="M{x:.1f},{base:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} '
                         f'H{x + bar_w - r:.1f} Q{x + bar_w:.1f},{y:.1f} {x + bar_w:.1f},{y + r:.1f} V{base:.1f} Z"/>')
        if i == peak and bucket.count:
            parts.append(f'<text class="value" x="{x + bar_w / 2:.1f}" y="{base - h - 5:.1f}" '
                         f'text-anchor="middle">{bucket.count}</text>')
        parts.append("</g>")
    for i in sorted({0, len(buckets) // 2, len(buckets) - 1}):
        if 0 <= i < len(buckets):
            anchor = "start" if i == 0 else "end" if i == len(buckets) - 1 else "middle"
            x = left + i * band + (0 if anchor == "start" else band if anchor == "end" else band / 2)
            parts.append(f'<text class="tick" x="{x:.1f}" y="{height - 6}" text-anchor="{anchor}">'
                         f'{esc(bucket_label(buckets[i]))}</text>')
    parts.append("</svg>")
    rows = "".join(f"<tr><td>{esc(bucket_label(b))}</td><td class=\"num\">{b.count}</td></tr>" for b in reversed(buckets))
    return ("".join(parts) + f'<details class="table-view"><summary>Show as table</summary><div class="scroll">'
            f'<table><thead><tr><th>Period</th><th class="num">{esc(noun.capitalize())}</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div></details>")


def sparkline(buckets: list[Bucket], ceiling: int, label: str) -> str:
    width, height = 96, 22
    band = width / max(len(buckets), 1)
    bar_w = max(1.0, band - 2)
    bars = []
    for i, bucket in enumerate(buckets):
        h = 0 if not bucket.count else max(1.5, bucket.count / max(ceiling, 1) * (height - 2))
        if h:
            bars.append(f'<rect x="{i * band + (band - bar_w) / 2:.1f}" y="{height - h:.1f}" width="{bar_w:.1f}" '
                        f'height="{h:.1f}" rx="{min(1.0, bar_w / 2):.1f}"/>')
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" role="img" aria-label="{esc(label)}">'
            f'<line x1="0" x2="{width}" y1="{height - 0.5}" y2="{height - 0.5}"/>{"".join(bars)}</svg>')


def tiles(issue_counts: dict[str, int], closed_in_range: int, days: int) -> str:
    items = [(STATUS_LABELS[s], issue_counts[s], s) for s in ACTIVE]
    items.append((f"Closed in {days} days", closed_in_range, "closed"))
    return '<section class="tiles">' + "".join(
        f'<div class="tile"><div class="tile-label"><span class="chip s-{key}"><span class="dot" aria-hidden="true">'
        f'</span></span>{esc(label)}</div><div class="tile-value">{value:,}</div></div>'
        for label, value, key in items) + "</section>"


def range_filter(days: int, base: str) -> str:
    links = []
    for option in RANGES:
        href = base if option == DEFAULT_RANGE else f"{base}?{urlencode({'range': option})}"
        current = ' aria-current="true"' if option == days else ""
        links.append(f'<a href="{esc(href)}"{current}>Last {option} days</a>')
    return f'<nav class="filters" aria-label="Time range">{"".join(links)}</nav>'


def active_table(issues: list[Issue], clock: Clock, show_repo: bool) -> str:
    if not issues:
        return '<p class="empty">Nothing is open. Every task is closed.</p>'
    rows = []
    for issue in sorted(issues, key=active_sort_key):
        repo = (f'<td class="repo"><a href="{repo_href(issue.project)}">{esc(issue.project)}</a></td>'
                if show_repo else "")
        rows.append(f"<tr><td>{status_chip(issue.status)}</td>{repo}<td>{issue_link(issue)}</td>"
                    f'<td class="wide">{priority_text(issue.priority)}</td><td class="wide">{esc(issue.type)}</td>'
                    f'<td class="when">{clock.relative(issue.last_change)}</td></tr>')
    head = "<th>Repository</th>" if show_repo else ""
    return (f'<div class="scroll"><table><thead><tr><th>Status</th>{head}<th>Issue</th><th class="wide">Priority</th>'
            f'<th class="wide">Type</th><th class="when">Updated</th></tr></thead><tbody>{"".join(rows)}</tbody>'
            "</table></div>")


KIND_LABELS = {"progress": "Progress", "decision": "Decision", "blocker": "Blocker", "result": "Result",
               "security": "Security", "handoff": "Handoff", "review": "Review", "comment": "Comment",
               "status": "Status"}


def activity_feed(items: list[Activity], issues: dict[tuple[str, str], Issue], clock: Clock, show_repo: bool,
                  full: bool = False) -> str:
    if not items:
        return '<p class="empty">No activity yet.</p>'
    rows = []
    for item in items:
        issue = issues.get((item.project, item.issue_id))
        target = ("" if full else issue_link(issue) if issue else
                  f'<span class="id">{esc(item.issue_id)}</span>' if item.issue_id else "")
        repo = (f'<a class="repo" href="{repo_href(item.project)}">{esc(item.project)}</a>' if show_repo else "")
        kind = esc(KIND_LABELS.get(item.kind, item.kind.capitalize()))
        if full:
            body = f'<div class="text">{linkify(item.text)}</div>' if item.text and not item.details else ""
            for heading, entries in item.details:
                body += (f'<div class="detail"><div class="detail-label">{esc(heading)}</div><ul>'
                         + "".join(f"<li>{linkify(entry)}</li>" for entry in entries) + "</ul></div>")
        else:
            body = f'<div class="text">{esc(truncate(item.text))}</div>' if item.text else ""
        rows.append(f'<li class="event k-{esc(item.kind)}"><div class="event-head"><span class="kind">{kind}</span>'
                    f'{repo}{target}<span class="when">{clock.relative(item.when)}</span></div>{body}</li>')
    return f'<ol class="feed{"" if full else " compact"}">{"".join(rows)}</ol>'


def card(title: str, body: str, note: str = "") -> str:
    note_html = f'<span class="card-note">{note}</span>' if note else ""
    return f'<section class="card"><header class="card-head"><h2>{esc(title)}</h2>{note_html}</header>{body}</section>'


# Application --------------------------------------------------------------------------------------

STATIC_DIR_MARKER = "/static/"

CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --text: #0b0b0b; --text-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11, 11, 11, 0.10); --wash: rgba(11, 11, 11, 0.04);
  --series-1: #2a78d6; --violet: #6250d6; --good: #0ca30c; --critical: #d03b3b; --link: #1c5cab;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --text: #f0efec; --text-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255, 255, 255, 0.10); --wash: rgba(255, 255, 255, 0.05);
    --series-1: #3987e5; --violet: #9085e9; --link: #86b6ef;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--page); color: var(--text);
  font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 0 16px; }
.top { background: var(--surface); border-bottom: 1px solid var(--border); }
.top .wrap { display: flex; align-items: center; gap: 16px; min-height: 56px; flex-wrap: wrap; padding-block: 8px; }
.brand { font-weight: 650; color: var(--text); font-size: 17px; }
.brand span { color: var(--muted); font-weight: 500; }
.top form { margin-left: auto; flex: 1 1 220px; max-width: 360px; }
input[type=search], select { width: 100%; font: inherit; color: var(--text); background: var(--page);
  border: 1px solid var(--border); border-radius: 8px; padding: 7px 10px; }
main.wrap { padding-block: 20px 32px; display: grid; gap: 20px; }
h1 { font-size: 22px; line-height: 1.25; margin: 0; font-weight: 650; overflow-wrap: anywhere; }
h2 { font-size: 15px; margin: 0; font-weight: 600; }
.crumbs { color: var(--text-2); font-size: 13px; display: flex; gap: 6px; flex-wrap: wrap; }
.crumbs a { color: var(--text-2); }
.filters { display: flex; gap: 6px; flex-wrap: wrap; }
.filters a { padding: 5px 12px; border-radius: 999px; border: 1px solid var(--border); color: var(--text-2);
  background: var(--surface); font-size: 13px; }
.filters a:hover { background: var(--wash); text-decoration: none; }
.filters a[aria-current] { color: var(--text); font-weight: 600; border-color: var(--axis); }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; }
.tile-label { color: var(--text-2); font-size: 13px; display: flex; align-items: center; gap: 6px; }
.tile-label .chip { padding: 0; }
.tile-value { font-size: 28px; font-weight: 600; margin-top: 2px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px 16px;
  min-width: 0; }
.card-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 10px;
  flex-wrap: wrap; }
.card-note { color: var(--text-2); font-size: 13px; }
.grid-2 { display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); gap: 20px; align-items: start; }
.stack { display: grid; gap: 20px; min-width: 0; }
@media (max-width: 900px) { .grid-2 { grid-template-columns: minmax(0, 1fr); } }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th { text-align: left; font-weight: 500; color: var(--text-2); font-size: 12px; padding: 6px 8px;
  border-bottom: 1px solid var(--grid); white-space: nowrap; }
td { padding: 7px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }
tr:last-child td { border-bottom: 0; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
td.when, th.when { white-space: nowrap; color: var(--text-2); }
td.repo { white-space: nowrap; }
.zero { color: var(--muted); }
.issue { color: var(--text); }
.id { color: var(--text-2); font-variant-numeric: tabular-nums; font-size: 13px; white-space: nowrap; }
.muted { color: var(--muted); }
.chip { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; font-size: 13px; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); flex: none; }
.s-in_progress .dot { background: var(--series-1); }
.s-in_review .dot { background: var(--violet); }
.s-blocked .dot { background: var(--critical); border-radius: 2px; }
.s-closed .dot { background: var(--good); }
.p-high { font-weight: 650; }
.p-low { color: var(--text-2); }
.empty { color: var(--text-2); margin: 4px 0; }
.chart { width: 100%; height: auto; display: block; overflow: visible; }
.chart .grid { stroke: var(--grid); stroke-width: 1; }
.chart .axis { stroke: var(--axis); stroke-width: 1; }
.chart .tick { fill: var(--muted); font-size: 11px; }
.chart .value { fill: var(--text); font-size: 11px; font-weight: 600; }
.chart .bar { fill: var(--series-1); }
.chart .hit { fill: transparent; }
.chart .col { outline: none; }
.chart .col:hover .bar, .chart .col:focus-visible .bar { opacity: 0.75; }
.chart .col:focus-visible .hit { fill: var(--wash); }
.spark { width: 96px; height: 22px; display: block; }
.spark rect { fill: var(--muted); }
.spark line { stroke: var(--grid); stroke-width: 1; }
.table-view { margin-top: 8px; font-size: 13px; color: var(--text-2); }
.table-view summary { cursor: pointer; }
.table-view table { max-width: 320px; margin-top: 6px; }
.tip { position: fixed; z-index: 10; pointer-events: none; background: var(--surface); color: var(--text);
  border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; font-size: 13px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12); }
.tip strong { display: block; font-size: 14px; }
.tip span { color: var(--text-2); }
.feed { list-style: none; margin: 0; padding: 0; }
.event { padding: 9px 0; border-bottom: 1px solid var(--grid); min-width: 0; }
.event:last-child { border-bottom: 0; }
.event-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; font-size: 13px; }
.event-head .when { margin-left: auto; color: var(--text-2); white-space: nowrap; }
.event-head .issue { overflow-wrap: anywhere; }
.kind { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-2);
  border: 1px solid var(--border); border-radius: 4px; padding: 0 5px; }
.k-blocker .kind, .k-security .kind { color: var(--critical); }
.k-status .kind { color: var(--series-1); }
.event .text { margin-top: 3px; color: var(--text-2); font-size: 14px; overflow-wrap: anywhere; white-space: pre-wrap; }
.feed.compact .text { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden;
  white-space: normal; }
.detail { margin-top: 6px; font-size: 14px; }
.detail-label { font-size: 12px; color: var(--text-2); font-weight: 600; }
.detail ul { margin: 2px 0 0; padding-left: 18px; }
.detail li { overflow-wrap: anywhere; }
.meta { display: flex; flex-wrap: wrap; gap: 8px 20px; color: var(--text-2); font-size: 13px; align-items: center; }
.meta b { color: var(--text); font-weight: 600; }
.prose { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 14px; margin: 0; }
.warn { border-color: var(--critical); }
.warn p { margin: 4px 0; }
.search-form { display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 1fr) minmax(0, 1fr) auto; gap: 8px; }
@media (max-width: 640px) { .search-form { grid-template-columns: minmax(0, 1fr); } }
button { font: inherit; border: 1px solid var(--border); border-radius: 8px; background: var(--text); color: var(--page);
  padding: 7px 14px; cursor: pointer; }
footer.wrap { color: var(--muted); font-size: 12px; padding-bottom: 24px; display: flex; gap: 12px; flex-wrap: wrap; }
.health-ok { color: var(--text-2); }
.health-bad { color: var(--critical); font-weight: 600; }
@media (max-width: 640px) {
  .wide { display: none; }
  .chart .tick, .chart .value { font-size: 22px; }
  .tile-value { font-size: 24px; }
  h1 { font-size: 19px; }
}
"""

JS = """
(() => {
  const tip = document.createElement("div");
  tip.className = "tip";
  tip.hidden = true;
  document.body.append(tip);
  const show = (el, x, y) => {
    const value = document.createElement("strong");
    value.textContent = el.dataset.tipValue;
    const label = document.createElement("span");
    label.textContent = el.dataset.tipLabel;
    tip.replaceChildren(value, label);
    tip.hidden = false;
    const box = tip.getBoundingClientRect();
    tip.style.left = Math.max(8, Math.min(innerWidth - box.width - 8, x - box.width / 2)) + "px";
    tip.style.top = Math.max(8, y - box.height - 12) + "px";
  };
  const hide = () => { tip.hidden = true; };
  for (const el of document.querySelectorAll("[data-tip-value]")) {
    el.addEventListener("pointermove", (event) => show(el, event.clientX, event.clientY));
    el.addEventListener("pointerleave", hide);
    el.addEventListener("focus", () => {
      const box = el.getBoundingClientRect();
      show(el, box.left + box.width / 2, box.top);
    });
    el.addEventListener("blur", hide);
  }
  const every = Number(document.body.dataset.refresh || 0) * 1000;
  if (every) {
    const loaded = Date.now();
    const check = () => {
      const typing = document.activeElement && document.activeElement.matches("input, select, textarea");
      if (!document.hidden && !typing && Date.now() - loaded >= every) location.reload();
    };
    setInterval(check, 5000);
    document.addEventListener("visibilitychange", check);
  }
})();
"""

# A white check on the chart blue, legible at 16 px on light and dark tab bars.
FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           '<rect width="32" height="32" rx="7" fill="#2a78d6"/>'
           '<path d="M9 16.5l4.5 4.5L23 11.5" fill="none" stroke="#fff" stroke-width="3.5" '
           'stroke-linecap="round" stroke-linejoin="round"/></svg>')

ASSETS = {
    "app.css": (CSS.encode(), "text/css; charset=utf-8"),
    "app.js": (JS.encode(), "text/javascript; charset=utf-8"),
    "favicon.svg": (FAVICON.encode(), "image/svg+xml"),
}
ASSET_VERSION = hashlib.sha256(b"".join(body for body, _ in ASSETS.values())).hexdigest()[:10]
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
                                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = "text/html; charset=utf-8"
    cache: str = "no-store"


class Dashboard:
    def __init__(self, store: Store, tz: ZoneInfo, sync_url: str, version_file: Path | None,
                 now=lambda: datetime.now(timezone.utc), hosts: tuple[str, ...] = DEFAULT_HOSTS) -> None:
        self.store, self.tz, self.sync_url, self.version_file, self._now = store, tz, sync_url.rstrip("/"), version_file, now
        self.hosts = frozenset(h.lower().rstrip(".") for h in hosts)
        self._sync_health: tuple[float, bool] = (float("-inf"), False)
        self._sync_lock = threading.Lock()

    # Routing

    def host_allowed(self, host: str | None) -> bool:
        if not host:
            return True
        name = host.strip().lower()
        if name.startswith("[") and "]" in name:
            name = name[1:name.index("]")]
        elif name.count(":") == 1:
            name = name.split(":")[0]
        name = name.rstrip(".")
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            return name in self.hosts

    def handle(self, raw_path: str, host: str | None = None) -> Response:
        if not self.host_allowed(host):
            return Response(HTTPStatus.MISDIRECTED_REQUEST, b"Unknown host\n", "text/plain; charset=utf-8")
        parts = urlsplit(raw_path)
        query = {key: values[-1] for key, values in parse_qs(parts.query).items()}
        segments = [unquote(s) for s in parts.path.split("/") if s]
        try:
            if parts.path.startswith(STATIC_DIR_MARKER) and len(segments) == 2 and segments[1] in ASSETS:
                body, kind = ASSETS[segments[1]]
                return Response(200, body, kind, "public, max-age=86400")
            if parts.path == "/healthz":
                return self.healthz()
            if not segments:
                return self.overview(self.range_param(query))
            if segments == ["search"]:
                return self.search_page(query)
            if segments[0] == "r" and len(segments) == 2:
                return self.repo_page(segments[1], self.range_param(query))
            if segments[0] == "r" and len(segments) == 3:
                return self.issue_page(segments[1], segments[2])
            return self.not_found("No such page.")
        except sqlite3.Error as error:
            return self.page(HTTPStatus.SERVICE_UNAVAILABLE, "td-sync data unavailable", card(
                "td-sync data is unavailable", f"<p>{esc(str(error))}</p>"), refresh=True)
        except Exception:
            traceback.print_exc()
            return Response(HTTPStatus.INTERNAL_SERVER_ERROR, b"Internal error; see the service log\n",
                            "text/plain; charset=utf-8")

    @staticmethod
    def range_param(query: dict[str, str]) -> int:
        try:
            days = int(query.get("range", DEFAULT_RANGE))
        except ValueError:
            return DEFAULT_RANGE
        return days if days in RANGES else DEFAULT_RANGE

    # Pages

    def overview(self, days: int) -> Response:
        now, projects = self._now(), self.store.projects()
        clock = Clock(self.tz, now)
        issues = [i for p in projects for i in p.issues]
        start = range_start(days, now, self.tz)
        closed = [i.closed for i in issues if i.closed and i.closed >= start]
        activity = sorted((a for p in projects for a in p.activity), key=lambda a: a.when, reverse=True)
        by_key = {(i.project, i.id): i for i in issues}
        unit = "day" if days <= 30 else "week"
        chart = column_chart(bucketize(closed, days, now, self.tz), "closed",
                             f"Issues closed per {unit}, last {days} days, all repositories")
        body = [
            range_filter(days, "/"),
            self.errors(projects),
            tiles(counts(issues), len(closed), days),
            card(f"Closed per {unit}", chart, f"{len(closed):,} across {len(projects)} repositories"),
            '<div class="grid-2">',
            card("Active work", active_table([i for i in issues if i.status in ACTIVE], clock, True),
                 f"{sum(i.status in ACTIVE for i in issues)} not closed"),
            card("Recent activity", activity_feed(activity[:12], by_key, clock, True)),
            "</div>",
            card("Repositories", self.repo_table(projects, days, now, clock), f"{len(projects)} synced"),
        ]
        return self.page(200, "td dashboard", "".join(body), refresh=True)

    def repo_table(self, projects: list[Project], days: int, now: datetime, clock: Clock) -> str:
        start = range_start(days, now, self.tz)
        series = {p.id: bucketize([a.when for a in p.activity if a.when >= start], days, now, self.tz)
                  for p in projects}
        ceiling = max((b.count for buckets in series.values() for b in buckets), default=0)
        rows = []
        ordered = sorted(projects, key=lambda p: p.last_activity or datetime.min.replace(tzinfo=timezone.utc),
                         reverse=True)
        for project in ordered:
            c = counts(project.issues)
            closed_recent = sum(1 for i in project.issues if i.closed and i.closed >= start)
            cells = "".join(f'<td class="num{" zero" if not c[s] else ""}">{c[s]}</td>' for s in ACTIVE)
            events = sum(b.count for b in series[project.id])
            rows.append(
                f'<tr><td class="repo"><a href="{repo_href(project.slug, days)}">{esc(project.name)}</a></td>{cells}'
                f'<td class="num{" zero" if not closed_recent else ""}">{closed_recent}</td>'
                f'<td class="num wide">{c["closed"]}</td>'
                f'<td class="wide">{sparkline(series[project.id], ceiling, f"{events} activity entries in {days} days")}</td>'
                f'<td class="when">{clock.relative(project.last_activity)}</td></tr>')
        heads = "".join(f'<th class="num">{esc(STATUS_LABELS[s])}</th>' for s in ACTIVE)
        return (f'<div class="scroll"><table><thead><tr><th>Repository</th>{heads}'
                f'<th class="num">Closed ({days}d)</th><th class="num wide">Closed (all)</th>'
                f'<th class="wide">Activity ({days}d)</th><th class="when">Last activity</th></tr></thead>'
                f'<tbody>{"".join(rows)}</tbody></table></div>')

    def find_project(self, slug: str) -> Project | None:
        return next((p for p in self.store.projects() if p.slug == slug), None)

    def repo_page(self, slug: str, days: int) -> Response:
        project = self.find_project(slug)
        if not project:
            return self.not_found("No synced repository has that name.")
        now = self._now()
        clock = Clock(self.tz, now)
        start = range_start(days, now, self.tz)
        recent_closed = sorted((i for i in project.issues if i.closed and i.closed >= start),
                               key=lambda i: i.closed, reverse=True)
        unit = "day" if days <= 30 else "week"
        by_key = {(i.project, i.id): i for i in project.issues}
        closed_rows = "".join(
            f'<tr><td>{issue_link(i)}</td><td class="wide">{esc(i.type)}</td>'
            f'<td class="when">{clock.relative(i.closed)}</td></tr>' for i in recent_closed[:60])
        closed_table = (f'<div class="scroll"><table><thead><tr><th>Issue</th><th class="wide">Type</th>'
                        f'<th class="when">Closed</th></tr></thead><tbody>{closed_rows}</tbody></table></div>'
                        if recent_closed else f'<p class="empty">Nothing closed in the last {days} days.</p>')
        body = [
            f'<nav class="crumbs"><a href="/">Overview</a><span>/</span><span>{esc(project.name)}</span></nav>',
            f"<h1>{esc(project.name)}</h1>",
            range_filter(days, repo_href(project.slug)),
            self.errors([project]),
            tiles(counts(project.issues), len(recent_closed), days),
            card(f"Closed per {unit}", column_chart(bucketize([i.closed for i in recent_closed], days, now, self.tz),
                                                    "closed", f"Issues closed per {unit} in {project.name}")),
            '<div class="grid-2"><div class="stack">',
            card("Active work", active_table([i for i in project.issues if i.status in ACTIVE], clock, False)),
            card(f"Closed in the last {days} days", closed_table,
                 f'<a href="/search?{urlencode({"repo": project.slug, "status": "closed"})}">All closed</a>'),
            "</div>",
            card("Recent activity", activity_feed(project.activity[:20], by_key, clock, False)),
            "</div>",
        ]
        return self.page(200, f"{project.name} · td dashboard", "".join(body), refresh=True)

    def issue_page(self, slug: str, issue_id: str) -> Response:
        project = self.find_project(slug)
        issue = project.issue(issue_id) if project else None
        detail = self.store.issue_detail(project, issue_id) if issue else None
        if not project or not issue or detail is None:
            return self.not_found("No issue with that ID in this repository.")
        clock = Clock(self.tz, self._now())
        by_key = {(i.project, i.id): i for i in project.issues}
        children = sorted((i for i in project.issues if i.parent_id == issue.id), key=active_sort_key)
        history = sorted([a for a in project.activity if a.issue_id == issue.id] + detail["status_changes"],
                         key=lambda a: a.when)

        def related(ids: list[str]) -> str:
            return ", ".join(issue_link(by_key[(slug, i)]) if (slug, i) in by_key else f'<span class="id">{esc(i)}</span>'
                             for i in ids)

        meta = [status_chip(issue.status), f"<span>Priority {priority_text(issue.priority)}</span>",
                f"<span>{esc(issue.type.capitalize())}</span>"]
        if detail["points"]:
            meta.append(f"<span>{esc(str(detail['points']))} points</span>")
        meta += [f"<span>{esc(label)}</span>" for label in detail["labels"]]
        for label, when in (("Created", issue.created), ("Updated", issue.updated), ("Closed", issue.closed)):
            if when:
                meta.append(f"<span>{label} <b>{clock.relative(when)}</b></span>")
        links = []
        if issue.parent_id:
            links.append(f"<span>Parent {related([issue.parent_id])}</span>")
        if detail["depends_on"]:
            links.append(f"<span>Depends on {related(detail['depends_on'])}</span>")
        if detail["blocks"]:
            links.append(f"<span>Blocks {related(detail['blocks'])}</span>")
        body = [
            f'<nav class="crumbs"><a href="/">Overview</a><span>/</span><a href="{repo_href(project.slug)}">'
            f'{esc(project.name)}</a><span>/</span><span>{esc(issue.id)}</span></nav>',
            f'<h1>{esc(issue.title)} <span class="id">{esc(issue.id)}</span></h1>',
            f'<div class="meta">{"".join(meta)}</div>',
            f'<div class="meta">{"".join(links)}</div>' if links else "",
        ]
        if detail["description"]:
            body.append(card("Description", f'<p class="prose">{linkify(detail["description"])}</p>'))
        if detail["acceptance"]:
            body.append(card("Acceptance", f'<p class="prose">{linkify(detail["acceptance"])}</p>'))
        if children:
            body.append(card("Child issues", active_table(children, clock, False)))
        body.append(card("History", activity_feed(history, by_key, clock, False, full=True),
                         f"{len(history)} entries, oldest first"))
        return self.page(200, f"{issue.id} {issue.title} · td dashboard", "".join(body))

    def search_page(self, query: dict[str, str]) -> Response:
        text = query.get("q", "").strip()
        repo = query.get("repo", "")
        status = query.get("status", "")
        projects = self.store.projects()
        clock = Clock(self.tz, self._now())
        results: list[Issue] = []
        if text or repo or status:
            for project in projects:
                if repo and project.slug != repo:
                    continue
                ids = self.store.search(project, text) if text and not project.error else None
                results += [i for i in project.issues
                            if (ids is None or i.id in ids) and (not status or i.status == status)]
        results.sort(key=lambda i: i.last_change or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        repo_options = "".join(f'<option value="{esc(p.slug)}"{" selected" if p.slug == repo else ""}>{esc(p.name)}</option>'
                               for p in projects)
        status_options = "".join(f'<option value="{s}"{" selected" if s == status else ""}>{STATUS_LABELS[s]}</option>'
                                 for s in STATUSES)
        form = (f'<form class="search-form" action="/search" role="search">'
                f'<input type="search" name="q" value="{esc(text)}" placeholder="Search titles, descriptions, IDs" '
                f'aria-label="Search text"><select name="repo" aria-label="Repository"><option value="">All repositories</option>'
                f'{repo_options}</select><select name="status" aria-label="Status"><option value="">Any status</option>'
                f"{status_options}</select><button>Search</button></form>")
        shown = results[:300]
        rows = "".join(
            f'<tr><td>{status_chip(i.status)}</td><td class="repo"><a href="{repo_href(i.project)}">{esc(i.project)}</a></td>'
            f'<td>{issue_link(i)}</td><td class="wide">{priority_text(i.priority)}</td>'
            f'<td class="when">{clock.relative(i.last_change)}</td></tr>' for i in shown)
        if not (text or repo or status):
            listing = '<p class="empty">Search every synced repository, or filter by repository and status.</p>'
        elif not results:
            listing = '<p class="empty">No issues match.</p>'
        else:
            listing = (f'<div class="scroll"><table><thead><tr><th>Status</th><th>Repository</th><th>Issue</th>'
                       f'<th class="wide">Priority</th><th class="when">Updated</th></tr></thead><tbody>{rows}</tbody>'
                       "</table></div>")
        note = f"{len(results):,} matches" + (", newest 300 shown" if len(results) > len(shown) else "")
        body = ['<nav class="crumbs"><a href="/">Overview</a><span>/</span><span>Search</span></nav>',
                "<h1>Search</h1>", form, card("Results", listing, note if results else "")]
        return self.page(200, "Search · td dashboard", "".join(body), search=text)

    def healthz(self) -> Response:
        try:
            projects = self.store.projects()
        except sqlite3.Error as error:
            payload = {"status": "error", "error": f"server.db: {error}"}
            return Response(503, json.dumps(payload).encode(), "application/json")
        errors = {p.name: p.error for p in projects if p.error}
        try:
            # Render the overview too, so data that breaks the pages also fails the health check.
            self.overview(DEFAULT_RANGE)
        except Exception as error:
            traceback.print_exc()
            errors["overview"] = repr(error)
        payload = {"status": "error" if errors else "ok", "projects": len(projects),
                   "issues": sum(len(p.issues) for p in projects), "td_sync": "ok" if self.sync_ok() else "down",
                   "errors": errors}
        return Response(503 if errors else 200, json.dumps(payload).encode(), "application/json")

    def not_found(self, message: str) -> Response:
        return self.page(404, "Not found · td dashboard", card("Not found", f'<p>{esc(message)} <a href="/">Back to the overview</a></p>'))

    # Shared chrome

    def errors(self, projects: list[Project]) -> str:
        broken = [p for p in projects if p.error]
        if not broken:
            return ""
        items = "".join(f"<p><b>{esc(p.name)}</b>: {esc(p.error)}</p>" for p in broken)
        return f'<section class="card warn"><h2>Some repositories could not be read</h2>{items}</section>'

    def sync_ok(self) -> bool:
        with self._sync_lock:
            checked, healthy = self._sync_health
            if time.monotonic() - checked < SYNC_HEALTH_TTL:
                return healthy
        try:
            with urllib.request.urlopen(f"{self.sync_url}/healthz", timeout=1) as reply:
                healthy = reply.status == 200
        except Exception:  # Any reply but a 200, including a garbled one, means td-sync is not healthy.
            healthy = False
        with self._sync_lock:
            self._sync_health = (time.monotonic(), healthy)
        return healthy

    def sync_version(self) -> str:
        try:
            return self.version_file.read_text().strip() if self.version_file else ""
        except OSError:
            return ""

    def page(self, status: int, title: str, body: str, *, refresh: bool = False, search: str = "") -> Response:
        clock = Clock(self.tz, self._now())
        version = self.sync_version()
        health = ('<span class="health-ok">td-sync healthy</span>' if self.sync_ok()
                  else '<span class="health-bad">td-sync not responding</span>')
        refresh_attr = f' data-refresh="{REFRESH_SECONDS}"' if refresh else ""
        document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="icon" href="/static/favicon.svg?v={ASSET_VERSION}" type="image/svg+xml">
<link rel="stylesheet" href="/static/app.css?v={ASSET_VERSION}">
<script src="/static/app.js?v={ASSET_VERSION}" defer></script>
</head>
<body{refresh_attr}>
<header class="top"><div class="wrap">
<a class="brand" href="/">td <span>dashboard</span></a>
<form action="/search" role="search"><input type="search" name="q" value="{esc(search)}" placeholder="Search issues" aria-label="Search issues"></form>
</div></header>
<main class="wrap">{body}</main>
<footer class="wrap">{health}<span>{esc("td-sync " + version) if version else ""}</span>
<span>Updated {esc(clock.short(clock.now))}{" · refreshes every minute" if refresh else ""}</span></footer>
</body>
</html>
"""
        return Response(int(status), document.encode())


class Handler(BaseHTTPRequestHandler):
    server_version = "td-dashboard"
    dashboard: Dashboard

    def do_GET(self) -> None:
        self.respond(self.dashboard.handle(self.path, self.headers.get("Host")), body=True)

    def do_HEAD(self) -> None:
        self.respond(self.dashboard.handle(self.path, self.headers.get("Host")), body=False)

    def respond(self, response: Response, body: bool) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", response.cache)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(response.body)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # Accept IPv4 too on "::", since a .local name can resolve to either family.
        if self.address_family == socket.AF_INET6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def make_server(dashboard: Dashboard, host: str, port: int) -> Server:
    family = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)[0][0]
    server_class = type("DashboardServer", (Server,), {"address_family": family})
    handler = type("DashboardHandler", (Handler,), {"dashboard": dashboard})
    return server_class((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < MINIMUM_PYTHON:
        found, minimum = ".".join(map(str, sys.version_info[:3])), ".".join(map(str, MINIMUM_PYTHON))
        print(f"td dashboard needs Python {minimum} or later; this is {found}. Run it with a newer python3.",
              file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=Path("/var/lib/td-sync"),
                        help="td-sync data directory holding server.db and projects/")
    parser.add_argument("--host", default="::")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--sync-url", default="http://127.0.0.1:8080", help="td-sync base URL for its health check")
    parser.add_argument("--sync-version-file", type=Path, default=Path("/usr/local/lib/td-sync/version"))
    parser.add_argument("--timezone", default="America/Los_Angeles")
    parser.add_argument("--hosts", default=",".join(DEFAULT_HOSTS),
                        help="comma-separated host names to answer besides IP addresses")
    args = parser.parse_args(argv)
    dashboard = Dashboard(Store(args.data_dir), ZoneInfo(args.timezone), args.sync_url, args.sync_version_file,
                          hosts=tuple(h.strip() for h in args.hosts.split(",") if h.strip()))
    server = make_server(dashboard, args.host, args.port)
    print(f"td dashboard serving {args.data_dir} on port {args.port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
