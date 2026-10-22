"""Serve the dashboard over HTTP."""

from __future__ import annotations

import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pages import Dashboard, MAX_FORM_BYTES, Response, SECURITY_HEADERS, plain


class Handler(BaseHTTPRequestHandler):
    server_version = "td-dashboard"
    dashboard: Dashboard

    def do_GET(self) -> None:
        self.respond(self.dashboard.handle(self.path, self.headers.get("Host")), body=True)

    def do_HEAD(self) -> None:
        self.respond(self.dashboard.handle(self.path, self.headers.get("Host")), body=False)

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_FORM_BYTES:
            self.close_connection = True
            self.respond(plain(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "The form is too large."), body=True)
            return
        response = self.dashboard.handle_post(self.path, self.headers.get("Host"), self.headers.get("Origin"),
                                              self.rfile.read(length))
        self.respond(response, body=True)

    def respond(self, response: Response, body: bool) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", response.cache)
        if response.location:
            self.send_header("Location", response.location)
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
