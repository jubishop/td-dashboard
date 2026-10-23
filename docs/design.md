---
status: current
---

# Dashboard design

The dashboard shows every td project that one td-sync server holds: status,
active work, and history across all synced repositories. It runs beside
td-sync and reads td-sync's data directory without ever writing it. It can
also close and reopen tasks, as [task actions](task-actions.md) describes,
through the real td.

## History

Jubi chose a custom dashboard over td-watch on 2026-10-17. td-watch is a
private SvelteKit app by td's author (`marcus/td-watch`), and Jubi's GitHub
account can push to it. It is a multi-user admin app with API-key login and
no single all-repository overview. It also needs Node, and its main branch
can expect unreleased td-sync features.

It began in Jubi's `home-infra` repository on 2026-10-17 and runs on a
Raspberry Pi beside td-sync. On 2026-10-22 it moved to this public repository
because it was becoming an application of its own, with
[task actions](task-actions.md) planned. Host-specific settings stay with each
deployment, not in the code.

## Pages

- `/`: counts by status across every repository, closures per day (per week
  for 90 days), active work, recent activity, and a table of repositories
  with activity sparklines. A 7, 30, or 90-day filter scopes the closures and
  sparklines.
- `/r/<repository>`: the same for one repository, plus its recent closures.
- `/r/<repository>/<issue>`: description, acceptance, parent, children,
  dependencies, and a full history of logs, handoffs, reviews, comments, and
  status changes.
- `/search`: text across IDs, titles, descriptions, acceptance, and labels,
  with repository and status filters.
- `/healthz`: JSON counts, `td_sync` (`ok` or `down`, from td-sync's own
  `/healthz`), and `actions` (`on`, `off` with its reason, or `error`). It
  returns 503 when `server.db` cannot be read, the overview fails to render,
  any project reports an error, or actions are set up but td cannot run. A
  down td-sync does not change the status.

The overview and repository pages reload every minute while visible, except
while a form field has focus. Times use `--timezone`, Pacific by default.

## Data

The dashboard reads the td-sync data directory. Each project's `project.db`
uses td's own schema, which td-sync keeps current on every push; `server.db`
names the projects. Every read opens SQLite read-only in one short
transaction. td-sync uses rollback journals with a five-second busy timeout,
so a read delays a push by milliseconds at most. A project is reloaded only
when its `project.db` changes.

td-sync builds a project's `project.db` on its first push, so a newly linked
repository shows as empty until then. If td-sync crashes mid-write, read-only
opens fail until td-sync rolls the journal back, which it does the next time
it opens that database, on the project's next push. Meanwhile the dashboard
shows the last copy it read with a warning, and `/healthz` returns 503. Any
td change in that repository clears it.

Status changes come from issue snapshots in each project's `events.db`.
Consecutive snapshots with the same status collapse, so the duplicate
snapshots td 0.67.0 pushed do not appear. Status changes from before a
project's first sync are missing; its logs and handoffs cover that period.

A td-sync upgrade can change td's schema. The dashboard reads only a few
columns, but if a query fails, that project shows an error on every page and
`/healthz` returns 503 until the query is fixed.

On 2026-10-17, with 23 projects and 293 issues, every page rendered in 3–23
ms on a Raspberry Pi, and the service used about 33 MB of memory.

## Modules

`td_dashboard/` holds one module per area, and each imports only those
before it:

- `store.py` reads td-sync's data directory.
- `writer.py` changes tasks with td.
- `render.py` builds HTML fragments: chips, tables, charts, and forms.
- `pages.py` routes requests and builds pages, without the HTTP server.
- `server.py` serves `pages.py` over HTTP, and `__main__.py` parses options.
- `static/` holds the stylesheet, script, and favicon.

## Changes

`writer.py` runs td as a td-sync device of the dashboard's own, with its own
device ID and one td workspace per project under `--state-dir`. Each action
syncs, checks that the task still has the status the page showed, runs td's
own commands, and syncs again. td-sync applies the push to `project.db` before
answering it, so the page the browser returns to already shows the change.
Approvals name `--reviewer`, when set, with td's `--reviewed-by`, as
[task actions](task-actions.md#actions) records.
td runs with its key in the environment, never in a file the dashboard
writes, and with its own background sync off.

Pages post actions as forms. A request is refused unless its `Origin` names
the same host as its `Host`, so another site cannot submit one. Pages send
`Referrer-Policy: same-origin`, because under `no-referrer` browsers send
`Origin: null` even with their own forms. After an action, the browser returns
to its page with a one-time notice that offers Undo for Done and Won't do.

## Service

`systemd/td-dashboard.service` runs the app directory installed at
`/usr/local/lib/td-dashboard` with the system's `/usr/bin/python3 -I`. Python
puts that directory on the import path, so its modules import each other by
name:

- It runs as a `DynamicUser` in the `td-sync` group, which can read
  td-sync's `0750` data directory and `0640` files. `ProtectSystem=strict`
  keeps the whole file system read-only for it. It restarts on failure and is
  capped at 256 MB.
- `/etc/default/td-dashboard` holds the host's settings in
  `TD_DASHBOARD_OPTIONS`, such as `--hosts`.
- `StateDirectory=` gives it `/var/lib/td-dashboard` for td's workspaces, and
  `ImportCredential=` passes the API key in
  `/etc/credstore/td-dashboard.api-key`, if present.

Like td-sync, it is plain HTTP with no login, so anyone who can reach it can
read task titles and history, and, with actions on, close and reopen tasks. It answers only IP addresses and the names in
`--hosts`, so a web page cannot read it through DNS rebinding. Pages send a
strict Content Security Policy and load only their own scripts and styles.
