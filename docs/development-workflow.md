---
status: current
---

# Development workflow

This page records this project's own commands and integrations. Shared rules
are in the [foundation pages](foundation/README.md).

## Setup

```sh
bin/setup
bin/doctor
bin/check --full
```

Setup needs Git and Python 3.12 or later, and checks also need ShellCheck.
`bin/setup` then runs an executable `bin/setup-application`, when present, for
application dependencies such as `npm ci`.
QMD and direnv are optional; setup reports skipped features. Install td and
run `td init` as described in [task tracking](foundation/task-tracking.md).
Create `.envrc` only when the project needs environment settings, and review
it before running `direnv allow`; search does not need it.

## Checks

Follow the [check schedule](foundation/engineering-policy.md#checks).
`bin/check-application` runs the unit tests in `tests/` with the standard
library's `unittest`, so `bin/check --full` runs them after the foundation
checks. Run them alone with:

```sh
bin/check-application
```

The tests build small td-sync data directories in temporary folders and
serve pages on local ports; they need no td, td-sync, or network.

## Runtime and toolchain versions

The application needs Python 3.12 or later and nothing outside the standard
library. `MINIMUM_PYTHON` in `td_dashboard.py` and `MINIMUM` in
`bin/check-application` declare it; both refuse an older Python with the
version found. CI installs the latest Python 3 release on every run. Deployed
hosts use their system `/usr/bin/python3`.

## Deployment

Deploys are manual: after a change merges, run `bin/deploy` from an
up-to-date checkout of `main`, as the [README](../README.md#deploy) describes.
Only `td_dashboard.py` and `systemd/td-dashboard.service` reach the host, so
changes to anything else need no deploy. Record a skipped deploy and the last
deployed revision in the merged change's task.

## Project Starter

`.project-starter.json` records the installed Project Starter release and the
hashes of its managed files. Update them with Project Starter's `bin/sync`,
and record any intentional local change as an override with its reason.
