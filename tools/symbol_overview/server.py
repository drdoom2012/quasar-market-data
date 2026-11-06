"""The local web server: the page, ``/overview.json``, and a refusal of everything else (spec 5.1).

It binds the address it is given (the command line defaults to ``127.0.0.1``), serves ``GET`` only, and shows a client
no internals: a failure of the snapshot source is a generic ``500``.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol

from tools.symbol_overview.refresh import Snapshot

_PAGE = Path(__file__).with_name("page.html")


class SnapshotSource(Protocol):
    def get(self) -> Snapshot: ...


def make_server(source: SnapshotSource, host: str, port: int) -> ThreadingHTTPServer:
    page = _PAGE.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, document: object) -> None:
            self._send(status, json.dumps(document, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self) -> None:
            path, _, query = self.path.partition("?")
            if path == "/" and not query:
                self._send(200, page, "text/html; charset=utf-8")
            elif path == "/overview.json":
                if query:
                    self._json(400, {"api_error": {"code": "invalid_parameter", "detail": "no parameter is accepted"}})
                    return
                try:
                    snapshot = source.get()
                except Exception as error:  # noqa: BLE001 - a bug must not leak to the client, whatever it is
                    self.log_error("overview failed: %s", type(error).__name__)  # logged for the operator only
                    self._json(500, {"api_error": {"code": "internal", "detail": ""}})
                    return
                self._json(snapshot.status, snapshot.body)
            else:
                self._json(404, {"api_error": {"code": "not_found", "detail": ""}})

        def _refuse(self) -> None:
            body = json.dumps({"api_error": {"code": "method_not_allowed", "detail": "GET only"}}).encode()
            self._send(405, body, "application/json; charset=utf-8", {"Allow": "GET"})

        do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _refuse

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
