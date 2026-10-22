from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import contextlib
import io
from contextlib import closing
from unittest import mock
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo


def load_dashboard() -> ModuleType:
    script = Path(__file__).parents[1] / "td_dashboard.py"
    spec = importlib.util.spec_from_file_location("td_dashboard", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["td_dashboard"] = module
    spec.loader.exec_module(module)
    return module


dash = load_dashboard()
PACIFIC = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 17, 18, 0, tzinfo=timezone.utc)  # 11:00 PDT

PROJECT_SCHEMA = """
create table issues (id text primary key, title text, description text, status text, type text, priority text,
    points integer, labels text, parent_id text, acceptance text, created_at datetime, updated_at datetime,
    closed_at datetime, deleted_at datetime);
create table logs (id text, issue_id text, session_id text, message text, type text, timestamp datetime);
create table handoffs (id text, issue_id text, session_id text, done text, remaining text, decisions text,
    uncertain text, timestamp datetime);
create table issue_reviews (id text, issue_id text, decision text, summary text, created_at datetime);
create table comments (id text, issue_id text, session_id text, text text, created_at datetime);
create table issue_dependencies (id text, issue_id text, depends_on_id text, relation_type text);
"""
EVENTS_SCHEMA = """
create table events (server_seq integer primary key autoincrement, device_id text, session_id text,
    client_action_id integer, action_type text, entity_type text, entity_id text, payload json,
    client_timestamp datetime, server_timestamp datetime default current_timestamp);
"""


def issue(id, title, status, created, updated=None, closed=None, **extra):
    row = {"id": id, "title": title, "description": "", "status": status, "type": "task", "priority": "P2",
           "points": 0, "labels": "", "parent_id": "", "acceptance": "", "created_at": created,
           "updated_at": updated or created, "closed_at": closed, "deleted_at": None}
    row.update(extra)
    return row


class Fixture:
    """A td-sync data directory with the tables and columns the dashboard reads."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "projects").mkdir(parents=True)
        with closing(sqlite3.connect(root / "server.db")) as conn, conn:
            conn.execute("create table projects (id text primary key, name text, slug text, deleted_at datetime)")

    def add_project(self, project_id, name, *, issues=(), logs=(), handoffs=(), reviews=(), comments=(),
                    dependencies=(), events=(), deleted=False, build=True):
        with closing(sqlite3.connect(self.root / "server.db")) as conn, conn:
            conn.execute("insert into projects values (?, ?, ?, ?)",
                         (project_id, name, name, "2026-10-16 00:00:00" if deleted else None))
        directory = self.root / "projects" / project_id
        directory.mkdir()
        if build or events:
            self.add_events(directory, events)
        if not build:
            return
        with closing(sqlite3.connect(directory / "project.db")) as conn, conn:
            conn.executescript(PROJECT_SCHEMA)
            for row in issues:
                conn.execute(f"insert into issues ({', '.join(row)}) values ({', '.join('?' * len(row))})",
                             tuple(row.values()))
            conn.executemany("insert into logs values (?, ?, '', ?, ?, ?)", logs)
            conn.executemany("insert into handoffs values (?, ?, '', ?, ?, ?, ?, ?)", handoffs)
            conn.executemany("insert into issue_reviews values (?, ?, ?, ?, ?)", reviews)
            conn.executemany("insert into comments values (?, ?, '', ?, ?)", comments)
            conn.executemany("insert into issue_dependencies values (?, ?, ?, 'depends_on')", dependencies)

    @staticmethod
    def add_events(directory: Path, events) -> None:
        with closing(sqlite3.connect(directory / "events.db")) as conn, conn:
            conn.executescript(EVENTS_SCHEMA)
            for seq, (action, entity_id, payload, stamp) in enumerate(events, 1):
                conn.execute("insert into events (device_id, session_id, client_action_id, action_type, entity_type,"
                             " entity_id, payload, client_timestamp) values ('d', 's', ?, ?, 'issues', ?, ?, ?)",
                             (seq, action, entity_id, json.dumps(payload), stamp))


def snapshot(status, nested=False):
    data = {"id": "td-a1", "status": status}
    return {"new_data": {"issue": data} if nested else data, "previous_data": {}, "schema_version": 1}


def standard_fixture(root: Path) -> Fixture:
    fixture = Fixture(root)
    fixture.add_project(
        "p_home", "home",
        issues=[
            issue("td-a1", "Build <the> dashboard", "in_progress", "2026-10-15T09:00:00-07:00",
                  "2026-10-17T10:30:00.123456-07:00", description="Plan at https://example.com/plan.",
                  acceptance="It runs", parent_id="td-epic"),
            issue("td-epic", "Pi services", "open", "2026-10-01T09:00:00-07:00", priority="P1", type="epic"),
            issue("td-b2", "Fix 100% CPU", "closed", "2026-10-10T09:00:00-07:00",
                  "2026-10-16T23:30:00-07:00", "2026-10-16T23:30:00-07:00"),
            issue("td-old", "Ancient", "closed", "2026-01-01T09:00:00-08:00", closed="2026-01-02T09:00:00-08:00"),
            issue("td-gone", "Deleted", "open", "2026-10-01T09:00:00-07:00", deleted_at="2026-10-02T00:00:00Z"),
        ],
        logs=[("lg1", "td-a1", "Started the server", "progress", "2026-10-17T10:00:00.5-07:00"),
              ("lg2", "td-a1", "Chose port 8081", "decision", "2026-10-17T10:15:00-07:00")],
        handoffs=[("ho1", "td-a1", json.dumps(["Wrote the store"]), json.dumps(["Deploy"]), None, None,
                   "2026-10-17T10:20:00-07:00")],
        reviews=[("rv1", "td-b2", "approved", "Looks right", "2026-10-16T23:29:00-07:00")],
        comments=[("cm1", "td-a1", "Nice", "2026-10-17T10:25:00-07:00")],
        dependencies=[("dp1", "td-a1", "td-b2")],
        events=[("create", "td-a1", snapshot("open"), "2026-10-15 09:00:00-07:00"),
                ("update", "td-a1", snapshot("open"), "2026-10-15 10:00:00-07:00"),
                ("update", "td-a1", snapshot("in_progress", nested=True), "2026-10-16 09:00:00-07:00"),
                ("update", "td-a1", snapshot("in_progress", nested=True), "2026-10-16 09:30:00-07:00")],
    )
    fixture.add_project(
        "p_news", "news",
        issues=[issue("td-n1", "Review feed", "in_review", "2026-10-12T09:00:00-07:00"),
                issue("td-n2", "Blocked on API", "blocked", "2026-10-11T09:00:00-07:00")],
    )
    fixture.add_project("p_dead", "dead", issues=[issue("td-d1", "Hidden", "open", "2026-10-01T09:00:00-07:00")],
                        deleted=True)
    return fixture


class DashboardCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "td-sync"
        self.fixture = standard_fixture(self.root)
        self.store = dash.Store(self.root)
        self.app = dash.Dashboard(self.store, PACIFIC, "http://127.0.0.1:9", None, now=lambda: NOW)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def get(self, path: str):
        response = self.app.handle(path)
        return response.status, response.body.decode()


class ParsingTests(unittest.TestCase):
    def test_parses_each_timestamp_shape_td_and_sqlite_write(self) -> None:
        cases = {
            "2026-10-17T10:19:57.381026-07:00": datetime(2026, 10, 17, 17, 19, 57, 381026, timezone.utc),
            "2026-10-17T10:19:57.38102-07:00": datetime(2026, 10, 17, 17, 19, 57, 381020, timezone.utc),
            "2026-10-16 21:54:32.114652022+00:00": datetime(2026, 10, 16, 21, 54, 32, 114652, timezone.utc),
            "2026-10-16 18:26:14": datetime(2026, 10, 16, 18, 26, 14, tzinfo=timezone.utc),
            "2026-10-16T14:54:29-07:00": datetime(2026, 10, 16, 21, 54, 29, tzinfo=timezone.utc),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(dash.parse_time(raw), expected)
        self.assertIsNone(dash.parse_time(""))
        self.assertIsNone(dash.parse_time("not a time"))

    def test_handoff_lists_tolerate_null_and_plain_text(self) -> None:
        self.assertEqual(dash.json_list(json.dumps(["a", "", "b"])), ("a", "b"))
        self.assertEqual(dash.json_list(None), ())
        self.assertEqual(dash.json_list("plain"), ("plain",))

    def test_linkify_escapes_text_and_leaves_trailing_punctuation_outside(self) -> None:
        html = dash.linkify("See <b>https://example.com/a?x=1&y=2</b>. Then (https://example.com/b).")
        self.assertIn('<a href="https://example.com/a?x=1&amp;y=2"', html)
        self.assertIn("&lt;b&gt;", html)
        self.assertIn('https://example.com/b</a>).', html)


class BucketTests(unittest.TestCase):
    def test_short_ranges_bucket_by_pacific_day(self) -> None:
        late = datetime(2026, 10, 17, 6, 30, tzinfo=timezone.utc)  # 23:30 PDT on Oct 16
        buckets = dash.bucketize([late], 7, NOW, PACIFIC)
        self.assertEqual(len(buckets), 7)
        self.assertEqual(buckets[-1].start.isoformat(), "2026-10-17")
        self.assertEqual([b.count for b in buckets][-2:], [1, 0])

    def test_long_ranges_bucket_by_week_ending_today(self) -> None:
        spans = dash.bucket_spans(90, NOW.astimezone(PACIFIC).date())
        self.assertEqual(spans[-1][1].isoformat(), "2026-10-17")
        self.assertEqual(spans[-1][0].isoformat(), "2026-10-11")
        self.assertEqual(sum((end - start).days + 1 for start, end in spans), 90)

    def test_nice_ceiling(self) -> None:
        self.assertEqual([dash.nice_ceiling(v) for v in (0, 1, 3, 7, 12, 51)], [1, 1, 5, 10, 20, 100])


class StoreTests(DashboardCase):
    def test_loads_live_projects_and_skips_deleted_projects_and_issues(self) -> None:
        projects = {p.slug: p for p in self.store.projects()}
        self.assertEqual(sorted(projects), ["home", "news"])
        self.assertEqual({i.id for i in projects["home"].issues}, {"td-a1", "td-epic", "td-b2", "td-old"})
        self.assertEqual(dash.counts(projects["home"].issues),
                         {"in_progress": 1, "in_review": 0, "blocked": 0, "open": 1, "closed": 2})
        kinds = [a.kind for a in projects["home"].activity]
        self.assertEqual(kinds[:4], ["comment", "handoff", "decision", "progress"])

    def test_reloads_a_project_only_after_its_database_changes(self) -> None:
        first = self.store.projects()[0]
        self.assertIs(self.store.projects()[0], first)
        path = self.root / "projects" / "p_home" / "project.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("update issues set status = 'closed', closed_at = '2026-10-17T10:59:00-07:00'"
                         " where id = 'td-a1'")
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        reloaded = self.store.projects()[0]
        self.assertIsNot(reloaded, first)
        self.assertEqual(reloaded.issue("td-a1").status, "closed")

    def test_status_history_collapses_repeated_snapshots(self) -> None:
        project = self.store.projects()[0]
        changes = self.store.status_changes(project, "td-a1")
        self.assertEqual([c.text for c in changes], ["Open → In progress"])
        self.assertEqual(changes[0].when, datetime(2026, 10, 16, 16, 0, tzinfo=timezone.utc))

    def test_search_matches_descriptions_and_treats_wildcards_literally(self) -> None:
        project = self.store.projects()[0]
        self.assertEqual(self.store.search(project, "example.com/plan"), {"td-a1"})
        self.assertEqual(self.store.search(project, "100%"), {"td-b2"})
        self.assertEqual(self.store.search(project, "%"), {"td-b2"})
        self.assertEqual(self.store.search(project, "_"), set())

    def test_reads_never_create_or_modify_files(self) -> None:
        before = {p: p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()}
        for path in ("/", "/r/home", "/r/home/td-a1", "/search?q=dash", "/healthz"):
            self.get(path)
        after = {p: p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_a_writer_can_lock_the_database_right_after_a_read(self) -> None:
        self.store.projects()
        conn = sqlite3.connect(self.root / "projects" / "p_home" / "project.db", timeout=0)
        try:
            conn.execute("begin exclusive")
            conn.execute("rollback")
        finally:
            conn.close()

    def test_a_newly_linked_project_without_pushes_is_empty_not_an_error(self) -> None:
        self.fixture.add_project("p_new", "fresh", build=False)
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn('href="/r/fresh"', body)
        self.assertNotIn("could not be read", body)
        self.assertEqual(self.app.handle("/healthz").status, 200)
        self.assertEqual(self.get("/r/fresh")[0], 200)
        self.assertFalse((self.root / "projects" / "p_new" / "project.db").exists())

    def test_events_without_a_built_database_are_reported(self) -> None:
        self.fixture.add_project("p_odd", "odd", build=False,
                                 events=[("create", "td-x", snapshot("open"), "2026-10-17 09:00:00-07:00")])
        _, body = self.get("/")
        self.assertIn("has events but has not built project.db", body)
        response = self.app.handle("/healthz")
        self.assertEqual(response.status, 503)
        self.assertIn("odd", json.loads(response.body)["errors"])

    def test_an_unreadable_database_keeps_showing_the_last_copy_read(self) -> None:
        self.store.projects()
        path = self.root / "projects" / "p_home" / "project.db"
        path.write_bytes(b"not a database" * 100)
        home = self.store.projects()[0]
        self.assertIn("td-a1", {i.id for i in home.issues})
        self.assertIn("showing the last copy read", home.error)
        self.assertEqual(self.app.handle("/healthz").status, 503)


class PageTests(DashboardCase):
    def test_answers_only_known_host_names_and_ip_addresses(self) -> None:
        app = dash.Dashboard(self.store, PACIFIC, "http://127.0.0.1:9", None, now=lambda: NOW,
                             hosts=("raspberry-pi.local", "raspberry-pi", "raspberry-pi.example.ts.net", "localhost"))
        for host in (None, "raspberry-pi.local:8081", "RASPBERRY-PI.LOCAL.", "raspberry-pi", "localhost:8081",
                     "raspberry-pi.example.ts.net:8081",
                     "192.168.1.64:8081", "[fe80::1]:8081", "127.0.0.1"):
            with self.subTest(host=host):
                self.assertEqual(app.handle("/healthz", host).status, 200)
        for host in ("evil.example", "evil.example:8081", "raspberry-pi.local.evil.example",
                     "other.example.ts.net", "raspberry-pi.example.ts.net.evil.example"):
            with self.subTest(host=host):
                self.assertEqual(app.handle("/", host).status, 421)

    def test_answers_only_localhost_by_name_unless_hosts_are_given(self) -> None:
        self.assertEqual(self.app.handle("/healthz", "localhost:8081").status, 200)
        self.assertEqual(self.app.handle("/healthz", "192.168.1.64:8081").status, 200)
        self.assertEqual(self.app.handle("/", "raspberry-pi.local:8081").status, 421)

    def test_placeholder_timestamps_do_not_break_pages(self) -> None:
        self.assertIsNone(dash.parse_time("0001-01-01 00:00:00+00:00"))
        path = self.root / "projects" / "p_news" / "project.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("update issues set created_at = '0001-01-01 00:00:00+00:00',"
                         " updated_at = '0001-01-01 00:00:00+00:00' where id = 'td-n1'")
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        for page in ("/", "/r/news", "/r/news/td-n1", "/search?q=feed"):
            with self.subTest(page=page):
                self.assertEqual(self.get(page)[0], 200)

    def test_unexpected_errors_return_500_and_fail_the_health_check(self) -> None:
        def broken(days):
            raise RuntimeError("boom")
        self.app.overview = broken
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.get("/")[0], 500)
            response = self.app.handle("/healthz")
        self.assertEqual(response.status, 503)
        self.assertIn("boom", json.loads(response.body)["errors"]["overview"])

    def test_points_are_never_markup(self) -> None:
        path = self.root / "projects" / "p_home" / "project.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("update issues set points = '<a href=//evil.example>x</a>' where id = 'td-a1'")
        _, body = self.get("/r/home/td-a1")
        self.assertNotIn("evil.example", body)

    def test_overview_summarizes_every_repository(self) -> None:
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("Build &lt;the&gt; dashboard", body)
        self.assertNotIn("<the>", body)
        self.assertIn('href="/r/news"', body)
        self.assertIn("Blocked on API", body)
        self.assertNotIn("Hidden", body)
        self.assertNotIn("Ancient", body.split("Repositories")[0])
        self.assertIn('data-refresh="60"', body)
        self.assertIn("1 across 2 repositories", body)
        self.assertIn("td-sync not responding", body)

    def test_range_filter_widens_the_closed_window(self) -> None:
        _, body = self.get("/?range=90")
        self.assertIn('aria-current="true">Last 90 days', body)
        self.assertIn("Closed per week", body)
        _, fallback = self.get("/?range=5")
        self.assertIn('aria-current="true">Last 30 days', fallback)

    def test_repository_page(self) -> None:
        status, body = self.get("/r/home")
        self.assertEqual(status, 200)
        self.assertIn("<h1>home</h1>", body)
        self.assertIn("Fix 100% CPU", body)
        self.assertNotIn("Review feed", body)
        self.assertEqual(self.get("/r/nope")[0], 404)
        self.assertEqual(self.get("/r/dead")[0], 404)

    def test_issue_page_shows_details_links_and_full_history(self) -> None:
        status, body = self.get("/r/home/td-a1")
        self.assertEqual(status, 200)
        self.assertIn('<a href="https://example.com/plan"', body)
        self.assertIn("It runs", body)
        self.assertIn('href="/r/home/td-epic"', body)
        self.assertIn("Depends on", body)
        self.assertIn("Open → In progress", body)
        self.assertIn("Wrote the store", body)
        self.assertIn("Remaining", body)
        self.assertLess(body.index("Open → In progress"), body.index("Started the server"))
        self.assertEqual(self.get("/r/home/td-nope")[0], 404)
        self.assertEqual(self.get("/r/home/td-gone")[0], 404)

    def test_search_filters_by_text_repository_and_status(self) -> None:
        _, body = self.get("/search?q=dashboard")
        self.assertIn("td-a1", body)
        self.assertNotIn("td-n1", body)
        _, body = self.get("/search?repo=news&status=blocked")
        self.assertIn("td-n2", body)
        self.assertNotIn("td-n1", body)
        _, body = self.get("/search")
        self.assertIn("Search every synced repository", body)

    def test_unknown_paths_are_404(self) -> None:
        self.assertEqual(self.get("/nope")[0], 404)
        self.assertEqual(self.get("/static/other.js")[0], 404)

    def test_missing_server_database_degrades_to_503(self) -> None:
        (self.root / "server.db").unlink()
        self.assertEqual(self.get("/")[0], 503)
        self.assertEqual(self.app.handle("/healthz").status, 503)

    def test_healthz_reports_counts(self) -> None:
        response = self.app.handle("/healthz")
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertEqual((payload["status"], payload["projects"], payload["issues"]), ("ok", 2, 6))
        self.assertEqual(payload["td_sync"], "down")


class ServerTests(DashboardCase):
    def setUp(self) -> None:
        super().setUp()
        self.server = dash.make_server(self.app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_serves_pages_assets_and_security_headers(self) -> None:
        with urllib.request.urlopen(self.base + "/") as reply:
            self.assertEqual(reply.status, 200)
            self.assertIn("script-src 'self'", reply.headers["Content-Security-Policy"])
            self.assertEqual(reply.headers["Cache-Control"], "no-store")
            page = reply.read().decode()
        self.assertIn(f"/static/app.css?v={dash.ASSET_VERSION}", page)
        with urllib.request.urlopen(f"{self.base}/static/app.js?v={dash.ASSET_VERSION}") as reply:
            self.assertTrue(reply.headers["Content-Type"].startswith("text/javascript"))
        self.assertIn(f'<link rel="icon" href="/static/favicon.svg?v={dash.ASSET_VERSION}" type="image/svg+xml">', page)
        with urllib.request.urlopen(f"{self.base}/static/favicon.svg?v={dash.ASSET_VERSION}") as reply:
            self.assertEqual(reply.headers["Content-Type"], "image/svg+xml")
            self.assertTrue(reply.read().startswith(b"<svg"))
        request = urllib.request.Request(self.base + "/healthz", method="HEAD")
        with urllib.request.urlopen(request) as reply:
            self.assertEqual(reply.read(), b"")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/missing")
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()

    def test_checks_td_sync_on_first_use_even_right_after_boot(self) -> None:
        probe = dash.Dashboard(self.store, PACIFIC, self.base, None, now=lambda: NOW)
        with mock.patch.object(dash.time, "monotonic", return_value=5.0):
            self.assertTrue(probe.sync_ok())

    def test_listens_on_ipv4_and_ipv6_together(self) -> None:
        server = dash.make_server(self.app, "::", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            for host in ("127.0.0.1", "[::1]"):
                with self.subTest(host=host), urllib.request.urlopen(f"http://{host}:{port}/healthz") as reply:
                    self.assertEqual(reply.status, 200)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
