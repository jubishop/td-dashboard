# td-dashboard

A web dashboard for every td project on a td-sync server: status, history,
and quick task cleanup.

The project has no application yet, so it has no entry points.

## Development

Run `bin/setup` after cloning; it needs Git and Python 3.9 or later. QMD and
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
