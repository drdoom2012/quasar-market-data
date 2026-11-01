"""A fake of the Quasar API for the tool's tests: a real HTTP server on 127.0.0.1, canned answers by path.

It records every request, and the highest number of requests being served at the same moment, so a test can prove
how the tool talks to the API (parameters, concurrency, number of refreshes).
"""

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Self
from urllib.parse import parse_qsl, urlsplit


@dataclass(frozen=True)
class FakeReply:
    status: int
    body: bytes
    content_type: str | None = "application/json"
    headers: Mapping[str, str] = field(default_factory=dict)


Route = Callable[[dict[str, str]], FakeReply]


class FakeApi:
    def __init__(self, routes: Mapping[str, Route], *, delay_s: float = 0.0) -> None:
        self.routes = routes
        self.delay_s = delay_s
        self.url = ""
        self.requests: list[str] = []  # path plus query, in arrival order
        self.queries: list[dict[str, str]] = []
        self.max_in_flight = 0
        self._in_flight = 0
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Self:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                api._handle(self)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        assert self._server is not None and self._thread is not None
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def count(self, path: str) -> int:
        """How many requests had exactly this path (query ignored)."""
        return sum(1 for request in self.requests if urlsplit(request).path == path)

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        with self._lock:
            self.requests.append(handler.path)
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            split = urlsplit(handler.path)
            query = dict(parse_qsl(split.query, keep_blank_values=True))
            with self._lock:
                self.queries.append(query)
            if self.delay_s:
                time.sleep(self.delay_s)
            route = self.routes.get(split.path)
            reply = route(query) if route else FakeReply(404, b"{}")
            handler.send_response(reply.status)
            if reply.content_type is not None:
                handler.send_header("Content-Type", reply.content_type)
            for name, value in reply.headers.items():
                handler.send_header(name, value)
            handler.send_header("Content-Length", str(len(reply.body)))
            handler.end_headers()
            handler.wfile.write(reply.body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client gave up (a timeout test)
        finally:
            with self._lock:
                self._in_flight -= 1
