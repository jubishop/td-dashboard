# td-dashboard

A web dashboard for every [td](https://github.com/marcus/td) project on a
td-sync server: status, history, and quick task cleanup.

It is a small Python app in `td_dashboard/` that uses only the standard
library. It runs on the td-sync host and reads td-sync's data directory
read-only. The [design](docs/design.md) covers its pages, data access, and
service.

## Run it

```sh
python3 td_dashboard --data-dir /var/lib/td-sync --port 8081 --hosts pi.local,localhost
```

It needs Python 3.12 or later and read access to td-sync's data directory.
`--hosts` lists the host names it answers besides IP addresses, and defaults
to `localhost`. `--help` lists the other options.

## Deploy

`systemd/td-dashboard.service` runs it as a hardened `DynamicUser` in the
`td-sync` group, which can read td-sync's data. Each host names its site
settings in `/etc/default/td-dashboard`, which deploys never overwrite:

```sh
TD_DASHBOARD_OPTIONS=--hosts=pi.local,pi,localhost
```

Then, from a checkout of `main`:

```sh
bin/deploy
```

It copies the committed app and unit to the host over SSH, checks that the
host's Python can load the app, installs both, restarts the service, and
waits for `/healthz`. It targets `raspberry-pi` and `http://raspberry-pi.local:8081`
by default; set `TD_DASHBOARD_HOST` and `TD_DASHBOARD_URL` for another host.
On failure it prints the health response and the service log.

`/healthz` returns JSON counts and td-sync's status, and 503 when td-sync's
data cannot be read or a page fails to render.

## Development

Run `bin/setup` after cloning; it needs Git and Python 3.12 or later. QMD and
direnv are optional, and setup reports skipped features. See the
[development workflow](docs/development-workflow.md) for this project's
commands and the [check schedule](docs/foundation/engineering-policy.md#checks)
for when to run `bin/check --documents-only`, `bin/check`, and
`bin/check --full`. The checks need ShellCheck; `bin/doctor` diagnoses setup.

Hosted projects record their primary remote in
[Git remotes](docs/git-remotes.md); `bin/setup` never publishes repositories.

## Knowledge

- [Memory](memory/README.md): durable guidance and non-derivable context.
- [Docs](docs/README.md): designs, decisions, research, and reference guides.
- [Task tracking](docs/foundation/task-tracking.md): install td, run `td init`
  in the primary checkout, and use `td status` or `td monitor` for progress.

Keep the existing issue tracker for shared work and link related issues from
td. This project uses [Project Starter](https://github.com/jubishop/project-starter);
`.project-starter.json` records its release.

## License

td-dashboard is released under the [MIT License](LICENSE). Project Starter
files keep their own terms in [LICENSE.project-starter](LICENSE.project-starter).
