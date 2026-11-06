import http.client
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from tools.symbol_overview.refresh import Snapshot
from tools.symbol_overview.server import make_server

BODY: dict[str, object] = {"summary": {"total": 1}, "symbols": []}


class Source:
    def __init__(self, snapshot: Snapshot | Exception) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def get(self) -> Snapshot:
        self.calls += 1
        if isinstance(self.snapshot, Exception):
            raise self.snapshot
        return self.snapshot


@contextmanager
def running(source: Source) -> Iterator[http.client.HTTPConnection]:
    server = make_server(source, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        yield connection
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(connection: http.client.HTTPConnection, method: str, path: str) -> http.client.HTTPResponse:
    connection.request(method, path)
    return connection.getresponse()


def test_the_page_is_served_with_the_elements_the_script_fills() -> None:
    with running(Source(Snapshot(200, BODY))) as connection:
        response = request(connection, "GET", "/")
        text = response.read().decode()
    assert response.status == 200
    assert response.getheader("Content-Type", "").startswith("text/html")
    for element in ("n-ok", "problems", "ok-group", "refresh", "api-error"):
        assert f'id="{element}"' in text


def test_overview_json_passes_the_status_and_body_of_the_snapshot() -> None:
    with running(Source(Snapshot(200, BODY))) as connection:
        response = request(connection, "GET", "/overview.json")
        assert response.status == 200
        assert response.getheader("Cache-Control") == "no-store"
        assert response.getheader("Content-Type", "").startswith("application/json")
        assert json.loads(response.read()) == BODY


def test_a_page_error_snapshot_keeps_its_status() -> None:
    error: dict[str, object] = {"api_error": {"code": "api_error", "detail": "not_ready"}}
    with running(Source(Snapshot(502, error))) as connection:
        response = request(connection, "GET", "/overview.json")
        assert response.status == 502
        assert json.loads(response.read()) == error


def test_a_query_string_on_overview_json_is_refused_without_a_refresh() -> None:
    source = Source(Snapshot(200, BODY))
    with running(source) as connection:
        assert request(connection, "GET", "/overview.json?x=1").status == 400
    assert source.calls == 0


@pytest.mark.parametrize("path", ["/other", "/overview.json/", "/v1/candles", "//etc/passwd"])
def test_other_paths_are_404(path: str) -> None:
    with running(Source(Snapshot(200, BODY))) as connection:
        assert request(connection, "GET", path).status == 404


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
def test_other_methods_are_405_with_allow_get(method: str) -> None:
    source = Source(Snapshot(200, BODY))
    with running(source) as connection:
        response = request(connection, method, "/overview.json")
        assert response.status == 405
        assert response.getheader("Allow") == "GET"
    assert source.calls == 0


def test_a_bug_in_the_source_is_a_generic_500_without_internals() -> None:
    with running(Source(RuntimeError("secret detail /path/to/file"))) as connection:
        response = request(connection, "GET", "/overview.json")
        text = response.read().decode()
    assert response.status == 500
    assert "secret" not in text and "Traceback" not in text
    assert json.loads(text) == {"api_error": {"code": "internal", "detail": ""}}


def test_the_server_binds_only_the_loopback_address() -> None:
    server = make_server(Source(Snapshot(200, BODY)), "127.0.0.1", 0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()
