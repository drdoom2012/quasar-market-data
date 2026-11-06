"""The local web server: the page, ``/overview.json``, the symbol detail and a refusal of everything else (spec 5.1).

It binds the address it is given (the command line defaults to ``127.0.0.1``), serves ``GET`` only, and shows a client
no internals: a failure of the snapshot source is a generic ``500``. Beside the page it serves two fixed static files
(the detail script and the vendored chart library) from a table read at start, never from a path in the request, and
``/detail.json`` for one series, whose four query parameters are checked before anything is asked of the API.
"""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qsl

from tools.symbol_overview.detail_refresh import DetailAnswer
from tools.symbol_overview.model import RANGE_SLOTS
from tools.symbol_overview.refresh import Snapshot

_DIR = Path(__file__).parent
_PAGE = _DIR / "page.html"
# The only static files, by exact path. Nothing is ever looked up from the request.
_STATIC = {
    "/detail.js": _DIR / "detail.js",
    "/vendor/lightweight-charts.js": _DIR / "vendor" / "lightweight-charts.js",
}
_JAVASCRIPT = "text/javascript; charset=utf-8"
_DETAIL_KEYS = ("symbol", "venue", "timeframe", "range")
_MAX_VALUE_LENGTH = 64  # the API's own limit for a symbol
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


class SnapshotSource(Protocol):
    def get(self) -> Snapshot: ...


class DetailSource(Protocol):
    def get(self, symbol: str, venue: str, timeframe: str, range_name: str) -> DetailAnswer: ...


def _detail_query(query: str) -> tuple[str, str, str, str] | str:
    """The four values of a detail request, or the fixed word that names what is wrong (never the user's text)."""
    if _BAD_ESCAPE.search(query):
        return "query"  # parse_qsl would keep a bad %XX literally; a symbol is never guessed at
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, errors="strict")
    except ValueError:  # a bad pair or invalid UTF-8
        return "query"
    names = [name for name, _ in pairs]
    if sorted(names) != sorted(_DETAIL_KEYS):
        return "query"
    values = dict(pairs)
    for key in _DETAIL_KEYS:
        if not values[key] or len(values[key]) > _MAX_VALUE_LENGTH:
            return key
    if values["range"] not in RANGE_SLOTS:
        return "range"
    return values["symbol"], values["venue"], values["timeframe"], values["range"]


def make_server(
    source: SnapshotSource, host: str, port: int, detail: DetailSource | None = None
) -> ThreadingHTTPServer:
    page = _PAGE.read_bytes()
    static = {path: file.read_bytes() for path, file in _STATIC.items()}

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
            elif path in static and not query:
                self._send(200, static[path], _JAVASCRIPT, {"X-Content-Type-Options": "nosniff"})
            elif path == "/detail.json" and detail is not None:
                self._detail(query)
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

        def _detail(self, query: str) -> None:
            parsed = _detail_query(query)
            if isinstance(parsed, str):
                self._json(400, {"api_error": {"code": "invalid_parameter", "detail": parsed}})
                return
            assert detail is not None
            try:
                answer = detail.get(*parsed)
            except Exception as error:  # noqa: BLE001 - a bug must not leak to the client, whatever it is
                self.log_error("detail failed: %s", type(error).__name__)  # logged for the operator only
                self._json(500, {"api_error": {"code": "internal", "detail": ""}})
                return
            self._send(answer.status, answer.body, "application/json; charset=utf-8")

        def _refuse(self) -> None:
            body = json.dumps({"api_error": {"code": "method_not_allowed", "detail": "GET only"}}).encode()
            self._send(405, body, "application/json; charset=utf-8", {"Allow": "GET"})

        do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _refuse

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
