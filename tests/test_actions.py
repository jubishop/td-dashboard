from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from test_td_dashboard import NOW, PACIFIC, DashboardCase, dash

# A stand-in for td at its process boundary. Each workspace keeps a local copy of its project's issues;
# "sync" pushes that copy into td-sync's project.db, as td-sync does on a push, then pulls it back.
FAKE_TD = r'''#!{python}
import json, os, sqlite3, sys
from pathlib import Path

ROOT = Path({root!r})
args = sys.argv[1:]
with open(ROOT / "calls.jsonl", "a") as log:
    env = {{k: os.environ.get(k) for k in ("HOME", "TD_AUTH_KEY", "TD_SYNC_URL", "TD_SESSION_ID", "TD_SYNC_AUTO")}}
    log.write(json.dumps({{"args": args, "cwd": os.getcwd(), "env": env}}) + "\n")
failure = ROOT / ("fail-" + args[0])
if failure.exists():
    sys.stderr.write("\x1b[38;5;214mError: " + failure.read_text() + "\x1b[m\n")
    sys.stderr.write("\x1b[38;5;214mWarning: sync: autosync is off - 1 change(s) not being pushed.\x1b[m\n")
    sys.exit(1)
work = Path.cwd()
local = work / ".todos" / "issues.json"
project_db = Path({data!r}) / "projects" / work.name / "project.db"

def load():
    return json.loads(local.read_text())

def save(issues):
    local.write_text(json.dumps(issues))

def flag(name, default=""):
    return args[args.index(name) + 1] if name in args else default

command = args[0]
if command == "init":
    (work / ".todos").mkdir()
    save({{}})
elif command == "sync-project":
    (work / ".todos" / "link").write_text(args[2])
elif command == "sync":
    with sqlite3.connect(project_db) as conn:
        for issue_id, row in load().items():
            conn.execute("update issues set status = ?, labels = ?, closed_at = ? where id = ?",
                         (row["status"], ",".join(row["labels"]), row["closed_at"], issue_id))
        pulled = {{r[0]: {{"status": r[1], "labels": [l for l in (r[2] or "").split(",") if l], "closed_at": r[3]}}
                  for r in conn.execute("select id, status, labels, closed_at from issues where deleted_at is null")}}
    save(pulled)
else:
    issues, issue_id = load(), args[1]
    if issue_id not in issues:
        sys.stderr.write("Warning: issue not found: " + issue_id + "\n")
        sys.exit(1)
    row = issues[issue_id]
    if command == "show":
        print(json.dumps({{"id": issue_id, "status": row["status"], "labels": row["labels"]}}))
    elif command == "update":
        if "--add-label" in args:
            row["labels"].append(flag("--add-label"))
        if "--remove-label" in args:
            row["labels"].remove(flag("--remove-label"))
    elif command == "approve" and row["status"] != "in_review":
        sys.stderr.write("cannot approve: not in review\n")
        sys.exit(1)
    elif command in ("close", "approve"):
        row["status"], row["closed_at"] = "closed", "2026-10-17T10:59:00-07:00"
    elif command in ("unstart", "unblock", "reject", "reopen"):
        row["status"], row["closed_at"] = "open", None
    save(issues)
'''

HOST, ORIGIN = "dash.local:8081", "http://dash.local:8081"


def add_log(root: Path, issue_id: str, when: str) -> None:
    path = root / "projects" / "p_home" / "project.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("insert into logs values ('lg9', ?, '', 'Still going', 'progress', ?)", (issue_id, when))
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


class ActionCase(DashboardCase):
    def setUp(self) -> None:
        super().setUp()
        self.td_root = Path(self.temp.name) / "fake-td"
        self.td_root.mkdir()
        self.td = self.td_root / "td"
        self.td.write_text(FAKE_TD.format(python=sys.executable, root=str(self.td_root), data=str(self.root)))
        self.td.chmod(0o755)
        self.key = Path(self.temp.name) / "api-key"
        self.key.write_text("ak_test_key\n")
        self.state = Path(self.temp.name) / "state"
        self.writer = dash.TdWriter(str(self.td), self.state, "http://127.0.0.1:8080", self.key)
        self.app = dash.Dashboard(self.store, PACIFIC, "http://127.0.0.1:9", None, now=lambda: NOW,
                                  hosts=("dash.local",), writer=self.writer)

    def post(self, path: str, origin: str | None = ORIGIN, host: str | None = HOST, **form: str):
        return self.app.handle_post(path, host, origin, urlencode(form).encode())

    def act(self, issue_path: str, action: str, expect: str, back: str = "/", note: str = ""):
        response = self.post(issue_path, action=action, expect=expect, back=back, note=note)
        self.assertEqual(response.status, 303, response.body)
        return response.location, self.get(response.location)[1]

    def commands(self) -> list[list[str]]:
        """td commands that change or read tasks, leaving out workspace setup and syncs."""
        return [c["args"] for c in self.calls() if c["args"][0] not in ("init", "sync-project", "sync")]

    def calls(self) -> list[dict]:
        log = self.td_root / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def status(self, slug: str, issue_id: str) -> str:
        project = next(p for p in self.store.projects() if p.slug == slug)
        return project.issue(issue_id).status


class ActionTests(ActionCase):
    def test_done_closes_a_task_and_the_pages_show_it_right_away(self) -> None:
        location, body = self.act("/r/home/td-a1", "done", "in_progress")
        self.assertTrue(location.startswith("/?notice="))
        self.assertEqual(self.status("home", "td-a1"), "closed")
        self.assertIn("Closed td-a1 as done", body)
        self.assertIn('name="action" value="open"', body.split('class="notice')[1].split("</section>")[0])
        self.assertNotIn("Build &lt;the&gt; dashboard", body.split("Active work")[1].split("</section>")[0])
        self.assertEqual(self.commands(), [["show", "td-a1", "--json"],
                                           ["close", "td-a1", "-m", "Closed as done from the dashboard"]])

    def test_each_change_syncs_before_and_after_with_its_own_device(self) -> None:
        self.act("/r/home/td-a1", "done", "in_progress")
        calls = self.calls()
        self.assertEqual([c["args"][0] for c in calls], ["init", "sync-project", "sync", "show", "close", "sync"])
        self.assertEqual(calls[1]["args"], ["sync-project", "link", "p_home"])
        env = calls[-1]["env"]
        self.assertEqual(env["TD_AUTH_KEY"], "ak_test_key")
        self.assertEqual(env["TD_SYNC_URL"], "http://127.0.0.1:8080")
        self.assertEqual(env["TD_SESSION_ID"], "td-dashboard")
        self.assertTrue(Path(env["HOME"]).is_relative_to(self.state))
        auth = json.loads((Path(env["HOME"]) / ".config/td/auth.json").read_text())
        self.assertRegex(auth["device_id"], r"^[0-9a-f]{32}$")
        self.assertNotIn("ak_test_key", json.dumps(auth))

    def test_a_workspace_is_set_up_once_per_project_and_keeps_its_device(self) -> None:
        self.act("/r/home/td-a1", "done", "in_progress")
        auth = (self.state / "home/.config/td/auth.json").read_text()
        self.act("/r/home/td-epic", "wontdo", "open")
        dash.TdWriter(str(self.td), self.state, "http://127.0.0.1:8080", self.key)
        self.assertEqual([c["args"][0] for c in self.calls()].count("init"), 1)
        self.assertEqual((self.state / "home/.config/td/auth.json").read_text(), auth)

    def test_done_approves_a_task_in_review(self) -> None:
        self.act("/r/news/td-n1", "done", "in_review")
        self.assertEqual(self.commands()[-1], ["approve", "td-n1", "--reason", "Approved from the dashboard"])
        self.assertEqual(self.status("news", "td-n1"), "closed")

    def test_wont_do_labels_and_closes_and_stays_out_of_closure_counts(self) -> None:
        before = self.get("/")[1].split("Closed in 30 days")[1][:80]
        _, body = self.act("/r/home/td-a1", "wontdo", "in_progress", note="Superseded by td-epic")
        self.assertEqual(self.commands()[1:], [["update", "td-a1", "--add-label", "wont-do"],
                                               ["close", "td-a1", "-m", "Won't do: Superseded by td-epic"]])
        self.assertIn("Closed td-a1 as won&#x27;t do.", body)
        self.assertEqual(self.get("/")[1].split("Closed in 30 days")[1][:80], before)
        self.assertIn("1 across 2 repositories", self.get("/")[1])
        _, page = self.get("/r/home/td-a1")
        self.assertIn('class="chip s-wontdo"', page)
        _, repo = self.get("/r/home")
        self.assertIn("Won&#x27;t do", repo.split("Closed in the last")[1])

    def test_wont_do_without_a_note_records_a_default_reason(self) -> None:
        self.act("/r/home/td-epic", "wontdo", "open")
        self.assertEqual(self.commands()[-1], ["close", "td-epic", "-m", "Won't do (from the dashboard)"])

    def test_back_to_open_uses_the_command_for_each_status(self) -> None:
        cases = [("/r/home/td-a1", "in_progress", "unstart"), ("/r/news/td-n2", "blocked", "unblock"),
                 ("/r/news/td-n1", "in_review", "reject"), ("/r/home/td-b2", "closed", "reopen")]
        for path, status, command in cases:
            with self.subTest(status=status):
                _, body = self.act(path, "open", status, note="Needs another pass")
                issue_id = path.rsplit("/", 1)[1]
                self.assertEqual(self.commands()[-1], [command, issue_id, "--reason", "Needs another pass"])
                self.assertEqual(self.status(path.split("/")[2], issue_id), "open")
                self.assertIn(f"Moved {issue_id} back to open", body)
                self.assertNotIn('name="action" value="open"', body.split('class="notice')[1].split("</section>")[0])

    def test_undo_reopens_a_wont_do_task_and_removes_its_label(self) -> None:
        self.act("/r/home/td-a1", "wontdo", "in_progress")
        self.act("/r/home/td-a1", "open", "closed")
        self.assertEqual(self.commands()[-2:], [["reopen", "td-a1", "--reason", "Back to open from the dashboard"],
                                                ["update", "td-a1", "--remove-label", "wont-do"]])
        _, page = self.get("/r/home/td-a1")
        self.assertNotIn("s-wontdo", page)

    def test_a_task_that_changed_since_the_page_loaded_is_left_alone(self) -> None:
        _, body = self.act("/r/home/td-a1", "done", "open")
        self.assertEqual(self.commands(), [["show", "td-a1", "--json"]])
        self.assertIn("td-a1 is In progress now, not Open; nothing changed", body)
        self.assertIn('class="notice error"', body)
        self.assertEqual(self.status("home", "td-a1"), "in_progress")

    def test_td_errors_show_without_color_codes_or_sync_warnings(self) -> None:
        (self.td_root / "fail-close").write_text("issue is locked by another session")
        _, body = self.act("/r/home/td-a1", "done", "in_progress")
        banner = body.split('class="notice error"')[1].split("</section>")[0]
        self.assertIn("Could not close td-a1 as done: Error: issue is locked by another session", banner)
        self.assertNotIn("\x1b", banner)
        self.assertNotIn("Warning: sync", banner)
        self.assertEqual([c["args"][0] for c in self.calls()][-1], "sync")

    def test_unexpected_failures_become_an_error_notice(self) -> None:
        self.state.write_text("not a directory")
        with contextlib.redirect_stderr(io.StringIO()) as log:
            _, body = self.act("/r/home/td-a1", "done", "in_progress")
        self.assertIn("Could not close td-a1 as done: unexpected error; see the service log", body)
        self.assertIn("Traceback", log.getvalue())
        self.assertEqual(self.calls(), [])

    def test_a_failed_close_takes_the_wont_do_label_back_off(self) -> None:
        (self.td_root / "fail-close").write_text("nope")
        self.act("/r/home/td-a1", "wontdo", "in_progress")
        self.assertEqual(self.commands()[-1], ["update", "td-a1", "--remove-label", "wont-do"])

    def test_actions_that_do_not_apply_to_the_status_are_refused(self) -> None:
        _, body = self.act("/r/home/td-b2", "done", "closed")
        self.assertIn("td-b2 is already closed", body)
        _, body = self.act("/r/home/td-epic", "open", "open")
        self.assertIn("td-epic is already open", body)
        self.assertEqual([c[0] for c in self.commands()], ["show", "show"])

    def test_notices_are_escaped_and_expire_with_unknown_tokens_ignored(self) -> None:
        (self.td_root / "fail-close").write_text("<script>alert(1)</script>")
        _, body = self.act("/r/home/td-a1", "done", "in_progress")
        self.assertNotIn("<script>alert", body)
        self.assertIn("&lt;script&gt;", body)
        _, body = self.get("/?notice=forged")
        self.assertNotIn('class="notice', body)


class RequestCheckTests(ActionCase):
    def test_requests_from_other_sites_are_refused(self) -> None:
        for origin in ("http://evil.example", None, "null", "http://dash.local:9999", "https://dash.local.evil.example"):
            with self.subTest(origin=origin):
                response = self.post("/r/home/td-a1", origin=origin, action="done", expect="in_progress", back="/")
                self.assertEqual(response.status, 403)
        self.assertEqual(self.post("/r/home/td-a1", host="evil.example", origin="http://evil.example",
                                   action="done", expect="in_progress").status, 421)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.status("home", "td-a1"), "in_progress")

    def test_the_origin_may_be_an_ip_address_matching_the_host(self) -> None:
        response = self.app.handle_post("/r/home/td-a1", "192.168.1.64:8081", "http://192.168.1.64:8081",
                                        urlencode({"action": "done", "expect": "in_progress"}).encode())
        self.assertEqual(response.status, 303)

    def test_malformed_requests_are_rejected_before_td_runs(self) -> None:
        cases = [("/r/home/td-a1", {"action": "delete", "expect": "open"}, 400),
                 ("/r/home/td-a1", {"action": "done", "expect": "gone"}, 400),
                 ("/r/home/td-nope", {"action": "done", "expect": "open"}, 404),
                 ("/r/nope/td-a1", {"action": "done", "expect": "open"}, 404),
                 ("/", {"action": "done", "expect": "open"}, 404)]
        for path, form, status in cases:
            with self.subTest(path=path, form=form):
                self.assertEqual(self.post(path, **form).status, status)
        self.assertEqual(self.calls(), [])

    def test_only_local_return_paths_are_followed(self) -> None:
        for back in ("//evil.example/", "https://evil.example/", "/\\evil.example", "r/home"):
            with self.subTest(back=back):
                response = self.post("/r/home/td-epic", action="open", expect="in_progress", back=back)
                self.assertTrue(response.location.startswith("/?notice="), response.location)
        response = self.post("/r/home/td-epic", action="open", expect="in_progress", back="/r/home?range=7")
        self.assertTrue(response.location.startswith("/r/home?range=7&notice="))

    def test_a_dashboard_without_a_writer_offers_and_accepts_no_changes(self) -> None:
        reader = dash.Dashboard(self.store, PACIFIC, "http://127.0.0.1:9", None, now=lambda: NOW, hosts=("dash.local",))
        for path in ("/", "/r/home", "/r/home/td-a1"):
            self.assertNotIn('method="post"', reader.handle(path, HOST).body.decode())
        response = reader.handle_post("/r/home/td-a1", HOST, ORIGIN, b"action=done&expect=in_progress")
        self.assertEqual(response.status, 405)


class ActionPageTests(ActionCase):
    def test_active_work_rows_offer_each_action_for_their_status(self) -> None:
        _, body = self.get("/")
        active = body.split("Active work")[1].split("</section>")[0]
        self.assertIn('<details class="menu">', active)
        self.assertIn('action="/r/home/td-a1"', active)
        self.assertIn('name="expect" value="in_progress"', active)
        self.assertIn('name="back" value="/"', active)
        epic = active.split('action="/r/home/td-epic"')[1].split("</form>")[0]
        self.assertIn('name="action" value="done"', epic)
        self.assertNotIn('name="action" value="open"', epic)
        _, repo = self.get("/r/news?range=7")
        self.assertIn('name="back" value="/r/news?range=7"', repo)

    def test_the_issue_page_has_the_actions_and_a_note(self) -> None:
        _, body = self.get("/r/home/td-a1")
        self.assertIn('name="note"', body)
        for label in ("Done", "Won&#x27;t do", "Back to open"):
            self.assertIn(f">{label}</button>", body)
        self.assertNotIn("may still be working", body)
        _, closed = self.get("/r/home/td-b2")
        self.assertIn(">Reopen</button>", closed)
        self.assertNotIn('name="action" value="done"', closed)

    def test_the_issue_page_warns_before_changing_a_task_with_recent_activity(self) -> None:
        add_log(self.root, "td-a1", "2026-10-17T10:50:00-07:00")
        _, body = self.get("/r/home/td-a1")
        actions = body.split("Change status")[1].split("</section>")[0]
        self.assertIn("an agent may still be working on it", actions)

    def test_healthz_reports_whether_actions_are_ready(self) -> None:
        self.assertEqual(json.loads(self.app.handle("/healthz").body)["actions"], "on")
        self.key.unlink()
        response = self.app.handle("/healthz")
        self.assertEqual((response.status, json.loads(response.body)["actions"]), (200, "off: no API key"))
        self.assertNotIn('method="post"', self.get("/")[1])
        self.key.write_text("ak_test_key\n")
        self.td.unlink()
        response = self.app.handle("/healthz")
        self.assertEqual(response.status, 503)
        self.assertIn("td is not executable", json.loads(response.body)["errors"]["actions"])


class FreshnessTests(DashboardCase):
    def row(self, body: str, issue_id: str) -> str:
        return body.split("Active work")[1].split(f'<span class="id">{issue_id}</span>')[0].rsplit("<tr>", 1)[1]

    def test_updated_shows_the_latest_activity_on_a_task(self) -> None:
        add_log(self.root, "td-a1", "2026-10-17T10:52:00-07:00")
        _, body = self.get("/")
        row = body.split("Active work")[1].split("</tr>")[1]
        self.assertIn("td-a1", row)
        self.assertIn(">8m ago</time>", row)

    def test_recent_activity_marks_a_task_live(self) -> None:
        add_log(self.root, "td-a1", "2026-10-17T10:50:00-07:00")
        _, body = self.get("/")
        self.assertIn('class="flag live"', self.row(body, "td-a1"))
        _, page = self.get("/r/home/td-a1")
        self.assertIn('class="flag live"', page.split('class="meta"')[1])

    def test_a_day_without_activity_marks_work_in_progress_or_review_stale(self) -> None:
        _, body = self.get("/")
        self.assertIn('class="flag stale"', self.row(body, "td-n1"))
        self.assertNotIn('class="flag', self.row(body, "td-a1"))
        self.assertNotIn('class="flag', self.row(body, "td-n2"))
        later = dash.Dashboard(self.store, PACIFIC, "http://127.0.0.1:9", None,
                               now=lambda: datetime(2026, 10, 18, 18, 1, tzinfo=timezone.utc))
        self.assertIn('class="flag stale"', self.row(later.handle("/").body.decode(), "td-a1"))


class ActionServerTests(ActionCase):
    def setUp(self) -> None:
        super().setUp()
        self.server = dash.make_server(self.app, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def request(self, body: bytes, origin: str):
        request = urllib.request.Request(self.base + "/r/home/td-a1", data=body, method="POST",
                                         headers={"Origin": origin, "Host": HOST})

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        try:
            with urllib.request.build_opener(NoRedirect).open(request) as reply:
                return reply.status, reply.headers
        except urllib.error.HTTPError as error:
            error.close()
            return error.code, error.headers

    def test_a_form_post_redirects_back_with_a_notice(self) -> None:
        status, headers = self.request(b"action=done&expect=in_progress&back=%2Fr%2Fhome", ORIGIN)
        self.assertEqual(status, 303)
        location = urlsplit(headers["Location"])
        self.assertEqual(location.path, "/r/home")
        self.assertIn("notice", parse_qs(location.query))
        self.assertEqual(self.status("home", "td-a1"), "closed")

    def test_pages_let_browsers_send_their_origin_with_their_own_forms(self) -> None:
        # Under no-referrer, browsers send "Origin: null" even on same-origin posts, which the check refuses.
        with urllib.request.urlopen(self.base + "/") as reply:
            self.assertEqual(reply.headers["Referrer-Policy"], "same-origin")

    def test_oversized_and_cross_site_posts_are_refused(self) -> None:
        self.assertEqual(self.request(b"note=" + b"x" * 20_000, ORIGIN)[0], 413)
        self.assertEqual(self.request(b"action=done&expect=in_progress", "http://evil.example")[0], 403)
        self.assertEqual(self.calls(), [])
