"""Serve a read-only web dashboard of every td project that td-sync holds.

td-sync keeps each project's materialized td database at
<data-dir>/projects/<id>/project.db, its event log beside it in events.db, and
the project names in <data-dir>/server.db. Every read opens SQLite read-only
and holds its shared lock for a few milliseconds; td-sync's writers wait up to
five seconds for a lock, so the dashboard never makes a push fail.
"""

import sys

MINIMUM_PYTHON = (3, 12)
if sys.version_info < MINIMUM_PYTHON:  # Before the imports, so an older Python stops with this message.
    found, minimum = ".".join(map(str, sys.version_info[:3])), ".".join(map(str, MINIMUM_PYTHON))
    sys.exit(f"td dashboard needs Python {minimum} or later; this is {found}. Run it with a newer python3.")

import argparse
from pathlib import Path
from zoneinfo import ZoneInfo

from pages import DEFAULT_HOSTS, Dashboard
from server import make_server
from store import Store


def main(argv: list[str] | None = None) -> int:
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
