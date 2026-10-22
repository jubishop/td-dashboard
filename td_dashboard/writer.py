"""Change tasks with the real td, as a td-sync device of the dashboard's own."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import threading
from pathlib import Path

from store import STATUS_LABELS, WONT_DO


PROJECT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TD_TIMEOUT = 120
BACK_TO_OPEN = {"in_progress": "unstart", "blocked": "unblock", "in_review": "reject", "closed": "reopen"}


class ActionError(Exception):
    """An action td refused or that no longer applies; the message is for the person who asked."""


def td_message(output: str) -> str:
    """td's error output as one plain line, without color codes or its reminders about unpushed changes."""
    lines = [ANSI.sub("", line).strip() for line in output.splitlines()]
    message = " ".join(line for line in lines if line and not line.startswith("Warning: sync:"))
    return message if len(message) <= 300 else message[:299].rstrip() + "…"


class TdWriter:
    """Changes tasks with the real td, as a td-sync device of its own with one td workspace per project.

    td-sync rebuilds project.db from td's event log, so the dashboard never edits it. Instead td records each
    change locally and pushes it; td-sync applies it to project.db during the push, so the next page shows it,
    and every other device pulls it on its next td command. td-sync keeps the last of two concurrent writes.
    """

    def __init__(self, td: str, state_dir: Path, sync_url: str, key_file: Path) -> None:
        self.td, self.state_dir, self.sync_url, self.key_file = td, state_dir, sync_url.rstrip("/"), key_file
        self.home = state_dir / "home"
        self._lock = threading.Lock()

    def status(self) -> tuple[str, str]:
        """("on", ""), ("off", why) until an API key is installed, or ("error", problem)."""
        if not self._key():
            return "off", "no API key"
        if not shutil.which(self.td):
            return "error", f"td is not executable: {self.td}"
        return "on", ""

    def apply(self, project_id: str, issue_id: str, action: str, expect: str, note: str) -> None:
        """Apply an action if the issue still has the status the page showed, then push it to td-sync."""
        key = self._key()
        if not key:
            raise ActionError("this dashboard has no td-sync API key")
        if not PROJECT_ID.fullmatch(project_id):
            raise ActionError(f"unexpected project ID {project_id!r}")
        with self._lock:
            self._ensure_device()
            work = self._workspace(project_id, key)

            def run(*args: str) -> str:
                return self._run(work, key, *args)

            run("sync")
            try:
                current = json.loads(run("show", issue_id, "--json"))
                status, labels = current["status"], current.get("labels") or []
            except (ValueError, KeyError, TypeError) as error:
                raise ActionError(f"td show gave unreadable output ({error})") from error
            if status != expect:
                raise ActionError(f"{issue_id} is {STATUS_LABELS.get(status, status)} now, not "
                                  f"{STATUS_LABELS.get(expect, expect)}; nothing changed")
            if action != "open" and status == "closed":
                raise ActionError(f"{issue_id} is already closed")
            if action == "open" and status == "open":
                raise ActionError(f"{issue_id} is already open")
            failure = None
            try:
                self._change(run, issue_id, action, status, labels, note)
            except ActionError as error:
                failure = error
            try:
                run("sync")
            except ActionError as error:
                if not failure:
                    raise ActionError(f"td saved the change but could not push it to td-sync ({error}); "
                                      "it goes with the next change") from error
            if failure:
                raise failure

    @staticmethod
    def _change(run, issue_id: str, action: str, status: str, labels: list[str], note: str) -> None:
        if action == "done" and status == "in_review":
            run("approve", issue_id, "--reason", note or "Approved from the dashboard")
        elif action == "done":
            run("close", issue_id, "-m", note or "Closed as done from the dashboard")
        elif action == "wontdo":
            labeled = WONT_DO not in labels
            if labeled:
                run("update", issue_id, "--add-label", WONT_DO)
            try:
                run("close", issue_id, "-m", f"Won't do: {note}" if note else "Won't do (from the dashboard)")
            except ActionError:
                if labeled:
                    try:
                        run("update", issue_id, "--remove-label", WONT_DO)
                    except ActionError:
                        pass  # The close error matters more; the label stays on an open task.
                raise
        else:
            run(BACK_TO_OPEN[status], issue_id, "--reason", note or "Back to open from the dashboard")
            if status == "closed" and WONT_DO in labels:
                run("update", issue_id, "--remove-label", WONT_DO)

    def _key(self) -> str:
        try:
            return self.key_file.read_text().strip()
        except OSError:
            return ""

    def _ensure_device(self) -> None:
        """Give td a lasting device ID; the key stays in the environment, never in this file."""
        path = self.home / ".config" / "td" / "auth.json"
        try:
            auth = json.loads(path.read_text())
        except (OSError, ValueError):
            auth = {}
        device = auth.get("device_id") if isinstance(auth, dict) and auth.get("device_id") else secrets.token_hex(16)
        wanted = {"api_key": "", "user_id": "", "email": "", "server_url": self.sync_url, "device_id": device,
                  "expires_at": ""}
        if auth != wanted:
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_suffix(".tmp")
            partial.write_text(json.dumps(wanted, indent=2))
            partial.chmod(0o600)
            partial.replace(path)

    def _workspace(self, project_id: str, key: str) -> Path:
        """This device's td database for a project, initialized and linked on first use."""
        work = self.state_dir / "projects" / project_id
        linked = work / ".linked"
        if not linked.exists():
            work.mkdir(parents=True, exist_ok=True)
            if not (work / ".todos").exists():
                self._run(work, key, "init")
            self._run(work, key, "sync-project", "link", project_id)
            linked.touch()
        return work

    def _run(self, work: Path, key: str, *args: str) -> str:
        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
               "TD_SYNC_URL": self.sync_url, "TD_AUTH_KEY": key, "TD_FEATURE_SYNC_CLI": "1",
               # Each change syncs explicitly before and after, so td's own background sync stays off.
               "TD_SYNC_AUTO": "0", "TD_SESSION_ID": "td-dashboard", "TD_ANALYTICS": "0",
               "NO_COLOR": "1", "TERM": "dumb"}
        try:
            result = subprocess.run([self.td, *args], cwd=work, env=env, capture_output=True, text=True,
                                    timeout=TD_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ActionError(f"td {args[0]} failed: {error}") from error
        if result.returncode != 0:
            raise ActionError(td_message(result.stderr + "\n" + result.stdout)
                              or f"td {args[0]} exited with status {result.returncode}")
        return result.stdout
