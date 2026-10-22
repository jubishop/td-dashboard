"""The dashboard's routes and pages, independent of the HTTP server."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import secrets
import sqlite3
import threading
import time
import traceback
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from zoneinfo import ZoneInfo

from render import (
    ACTIONS, Clock, DEFAULT_RANGE, RANGES, action_form, active_table, activity_feed, card, column_chart, esc,
    freshness_flag, issue_chip, issue_href, issue_link, linkify, priority_text, range_filter, repo_href, sparkline,
    tiles,
)
from store import (
    ACTIVE, Issue, Project, STATUSES, STATUS_LABELS, Store, active_sort_key, bucketize, completed, counts,
    range_start,
)
from writer import ActionError, TdWriter


REFRESH_SECONDS = 60
SYNC_HEALTH_TTL = 15


# Host headers the dashboard answers besides IP literals; any other name could be DNS rebinding.
# A deployment names its own hosts with --hosts.
DEFAULT_HOSTS = ("localhost",)
STATIC_DIR_MARKER = "/static/"


STATIC_DIR = Path(__file__).resolve().parent / "static"
ASSETS = {name: ((STATIC_DIR / name).read_bytes(), kind) for name, kind in (
    ("app.css", "text/css; charset=utf-8"),
    ("app.js", "text/javascript; charset=utf-8"),
    ("favicon.svg", "image/svg+xml"),
)}
ASSET_VERSION = hashlib.sha256(b"".join(body for body, _ in ASSETS.values())).hexdigest()[:10]


SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
                                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = "text/html; charset=utf-8"
    cache: str = "no-store"
    location: str = ""


@dataclass(frozen=True)
class Notice:
    """The outcome of an action, shown once on the page the action returns to."""
    text: str
    error: bool = False
    undo: tuple[str, str] | None = None  # The issue's path and the page to return to after Undo.


MAX_FORM_BYTES = 8192
NOTE_LIMIT = 500
NOTICE_SECONDS = 600
MAIN_OPEN = b'<main class="wrap">'
VERBS = {"done": "close {} as done", "wontdo": "close {} as won't do", "open": "move {} back to open"}
DONE_TEXT = {"done": "Closed {} as done.", "wontdo": "Closed {} as won't do.", "open": "Moved {} back to open."}


def plain(status: int, text: str) -> Response:
    return Response(status, text.encode() + b"\n", "text/plain; charset=utf-8")


def same_origin(origin: str | None, host: str | None) -> bool:
    """Whether a browser sent the request from one of this dashboard's own pages, which blocks cross-site forms."""
    if not origin or not host:
        return False
    parts = urlsplit(origin)
    return parts.scheme in ("http", "https") and parts.netloc.lower() == host.strip().lower() and not parts.path


def local_path(value: str) -> str:
    """A same-site path to return to; anything else, such as //host or a full URL, becomes the overview."""
    if not value.startswith("/") or value.startswith("//") or "\\" in value or any(ord(c) < 32 for c in value):
        return "/"
    return value


class Dashboard:
    def __init__(self, store: Store, tz: ZoneInfo, sync_url: str, version_file: Path | None,
                 now=lambda: datetime.now(timezone.utc), hosts: tuple[str, ...] = DEFAULT_HOSTS,
                 writer: TdWriter | None = None) -> None:
        self.store, self.tz, self.sync_url, self.version_file, self._now = store, tz, sync_url.rstrip("/"), version_file, now
        self.hosts = frozenset(h.lower().rstrip(".") for h in hosts)
        self.writer = writer
        self._sync_health: tuple[float, bool] = (float("-inf"), False)
        self._sync_lock = threading.Lock()
        self._notices: dict[str, tuple[float, Notice]] = {}
        self._notice_lock = threading.Lock()

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
        response = self.route(raw_path, host)
        token = parse_qs(urlsplit(raw_path).query).get("notice", [""])[-1]
        notice = self.recall(token) if token else None
        if notice and response.content_type.startswith("text/html"):
            response.body = response.body.replace(MAIN_OPEN, MAIN_OPEN + self.notice_html(notice).encode(), 1)
        return response

    def route(self, raw_path: str, host: str | None) -> Response:
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

    def handle_post(self, raw_path: str, host: str | None, origin: str | None, body: bytes) -> Response:
        """Apply a task action from a form, then send the browser back to its page with a notice."""
        if not self.host_allowed(host):
            return Response(HTTPStatus.MISDIRECTED_REQUEST, b"Unknown host\n", "text/plain; charset=utf-8")
        if not same_origin(origin, host):
            return plain(HTTPStatus.FORBIDDEN, "Changes must come from this dashboard's own pages.")
        if not self.writable():
            return plain(HTTPStatus.METHOD_NOT_ALLOWED, "This dashboard does not change tasks.")
        segments = [unquote(s) for s in urlsplit(raw_path).path.split("/") if s]
        if len(segments) != 3 or segments[0] != "r":
            return self.not_found("No such page.")
        form = {key: values[-1] for key, values in parse_qs(body.decode("utf-8", "replace"), max_num_fields=10).items()}
        action, expect = form.get("action", ""), form.get("expect", "")
        if action not in ACTIONS or expect not in STATUSES:
            return plain(HTTPStatus.BAD_REQUEST, "Unknown action or status.")
        try:
            project = self.find_project(segments[1])
        except sqlite3.Error as error:
            return plain(HTTPStatus.SERVICE_UNAVAILABLE, f"td-sync data is unavailable: {error}")
        issue = project.issue(segments[2]) if project else None
        if not project or not issue:
            return self.not_found("No issue with that ID in this repository.")
        back = local_path(form.get("back", ""))
        note = " ".join(form.get("note", "").split())[:NOTE_LIMIT]
        failed = f"Could not {VERBS[action].format(issue.id)}"
        try:
            self.writer.apply(project.id, issue.id, action, expect, note)
        except ActionError as error:
            notice = Notice(f"{failed}: {error}", error=True)
        except Exception:
            traceback.print_exc()
            notice = Notice(f"{failed}: unexpected error; see the service log", error=True)
        else:
            undo = (issue_href(project.slug, issue.id), back) if action != "open" else None
            notice = Notice(DONE_TEXT[action].format(issue.id), undo=undo)
        query = urlencode({"notice": self.remember(notice)})
        return Response(HTTPStatus.SEE_OTHER, b"", location=f"{back}{'&' if '?' in back else '?'}{query}")

    def writable(self) -> bool:
        return self.writer is not None and self.writer.status()[0] == "on"

    def remember(self, notice: Notice) -> str:
        token = secrets.token_urlsafe(12)
        now = time.monotonic()
        with self._notice_lock:
            for key, (made, _) in list(self._notices.items()):
                if now - made > NOTICE_SECONDS:
                    del self._notices[key]
            self._notices[token] = (now, notice)
        return token

    def recall(self, token: str) -> Notice | None:
        with self._notice_lock:
            made, notice = self._notices.get(token, (0.0, None))
        return notice if notice and time.monotonic() - made <= NOTICE_SECONDS else None

    @staticmethod
    def notice_html(notice: Notice) -> str:
        undo = ""
        if notice.undo:
            path, back = notice.undo
            undo = (f'<form method="post" action="{esc(path)}" class="act">'
                    f'<input type="hidden" name="expect" value="closed"><input type="hidden" name="back" value="{esc(back)}">'
                    f'<button name="action" value="open" class="secondary">Undo</button></form>')
        role = "alert" if notice.error else "status"
        return (f'<section class="notice{" error" if notice.error else ""}" role="{role}"><p>{esc(notice.text)}</p>'
                f"{undo}</section>")

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
        closed = [c for c in map(completed, issues) if c and c >= start]
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
            card("Active work", active_table([i for i in issues if i.status in ACTIVE], clock, True,
                                             self.back("/", days)),
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
            closed_recent = sum(1 for c in map(completed, project.issues) if c and c >= start)
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

    def back(self, path: str, days: int | None = None) -> str | None:
        """The page an action from a list returns to, or None when the dashboard cannot change tasks."""
        if not self.writable():
            return None
        return f"{path}?{urlencode({'range': days})}" if days and days != DEFAULT_RANGE else path

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
        done = [i.closed for i in recent_closed if not i.wont_do]
        unit = "day" if days <= 30 else "week"
        by_key = {(i.project, i.id): i for i in project.issues}
        closed_rows = "".join(
            f'<tr><td>{issue_link(i)}{" " + issue_chip(i) if i.wont_do else ""}</td><td class="wide">{esc(i.type)}</td>'
            f'<td class="when">{clock.relative(i.closed)}</td></tr>' for i in recent_closed[:60])
        closed_table = (f'<div class="scroll"><table><thead><tr><th>Issue</th><th class="wide">Type</th>'
                        f'<th class="when">Closed</th></tr></thead><tbody>{closed_rows}</tbody></table></div>'
                        if recent_closed else f'<p class="empty">Nothing closed in the last {days} days.</p>')
        body = [
            f'<nav class="crumbs"><a href="/">Overview</a><span>/</span><span>{esc(project.name)}</span></nav>',
            f"<h1>{esc(project.name)}</h1>",
            range_filter(days, repo_href(project.slug)),
            self.errors([project]),
            tiles(counts(project.issues), len(done), days),
            card(f"Closed per {unit}", column_chart(bucketize(done, days, now, self.tz),
                                                    "closed", f"Issues closed per {unit} in {project.name}")),
            '<div class="grid-2"><div class="stack">',
            card("Active work", active_table([i for i in project.issues if i.status in ACTIVE], clock, False,
                                             self.back(repo_href(project.slug, days)))),
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

        meta = [issue_chip(issue) + freshness_flag(issue, clock.now),
                f"<span>Priority {priority_text(issue.priority)}</span>",
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
        if self.writable():
            live = ""
            if freshness_flag(issue, clock.now).startswith('<span class="flag live"'):
                live = (f'<p class="live-note">Last activity {clock.relative(issue.last_seen)}: '
                        "an agent may still be working on it.</p>")
            body.append(card("Change status", live + f'<div class="issue-actions">'
                             f'{action_form(issue, issue_href(project.slug, issue.id), note=True)}</div>'))
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
            f'<tr><td>{issue_chip(i)}</td><td class="repo"><a href="{repo_href(i.project)}">{esc(i.project)}</a></td>'
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
        actions = "off"
        if self.writer:
            state, detail = self.writer.status()
            actions = {"on": "on", "off": f"off: {detail}"}.get(state, "error")
            if state == "error":
                errors["actions"] = detail
        payload = {"status": "error" if errors else "ok", "projects": len(projects),
                   "issues": sum(len(p.issues) for p in projects), "td_sync": "ok" if self.sync_ok() else "down",
                   "actions": actions, "errors": errors}
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
