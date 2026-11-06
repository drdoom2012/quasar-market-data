"""The routes of the symbol detail: parameters are checked before any call, static files come from a fixed table."""

import http.client
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import pytest

from tests.tools.symbol_overview.test_server import BODY, Source
from tools.symbol_overview.detail_refresh import DetailAnswer
from tools.symbol_overview.refresh import Snapshot
from tools.symbol_overview.server import make_server

TOOL = Path(__file__).resolve().parents[3] / "tools" / "symbol_overview"
GOOD = "symbol=BTC%2FUSDT%3AUSDT&venue=binance-usdm&timeframe=5m&range=24h"


class DetailStub:
    def __init__(self, answer: DetailAnswer | Exception | None = None) -> None:
        self.answer = answer or DetailAnswer(200, b'{"ok":true}')
        self.calls: list[tuple[str, str, str, str]] = []

    def get(self, symbol: str, venue: str, timeframe: str, range_name: str) -> DetailAnswer:
        self.calls.append((symbol, venue, timeframe, range_name))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@contextmanager
def running(detail: DetailStub | None) -> Iterator[http.client.HTTPConnection]:
    server = make_server(Source(Snapshot(200, BODY)), "127.0.0.1", 0, detail)
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


def fetch(connection: http.client.HTTPConnection, path: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
    connection.request(method, path)
    response = connection.getresponse()
    return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()


def test_a_valid_request_reaches_the_source_with_the_decoded_arguments() -> None:
    stub = DetailStub()
    with running(stub) as connection:
        status, headers, body = fetch(connection, "/detail.json?" + GOOD)
    assert (status, body) == (200, b'{"ok":true}')
    assert headers["content-type"] == "application/json; charset=utf-8" and headers["cache-control"] == "no-store"
    assert stub.calls == [("BTC/USDT:USDT", "binance-usdm", "5m", "24h")]


@pytest.mark.parametrize(
    ("query", "word"),
    [
        ("", "query"),
        ("symbol=A&venue=v&timeframe=5m", "query"),  # a key missing
        (GOOD + "&x=1", "query"),  # an extra key
        (GOOD + "&range=1h", "query"),  # a repeated key
        (GOOD + "&", "query"),
        ("symbol=A&venue=v&timeframe=5m&range=24h&", "query"),
        ("symbol=&venue=v&timeframe=5m&range=24h", "symbol"),
        ("symbol=A&venue=&timeframe=5m&range=24h", "venue"),
        ("symbol=A&venue=v&timeframe=&range=24h", "timeframe"),
        ("symbol=A&venue=v&timeframe=5m&range=2h", "range"),
        ("symbol=A&venue=v&timeframe=5m&range=", "range"),
        ("symbol=" + "A" * 65 + "&venue=v&timeframe=5m&range=24h", "symbol"),
        ("symbol=%FF&venue=v&timeframe=5m&range=24h", "query"),  # invalid UTF-8
        ("symbol=A&venue=v&timeframe=5m&range=24h&symbol", "query"),
    ],
)
def test_a_bad_query_is_refused_with_a_fixed_word_and_no_call(query: str, word: str) -> None:
    stub = DetailStub()
    with running(stub) as connection:
        status, _, body = fetch(connection, "/detail.json?" + query)
    assert status == 400 and stub.calls == []
    assert json.loads(body) == {"api_error": {"code": "invalid_parameter", "detail": word}}


def test_the_users_text_is_never_echoed() -> None:
    with running(DetailStub()) as connection:
        _, _, body = fetch(connection, "/detail.json?symbol=<script>&venue=v&timeframe=5m&range=zzz")
    assert b"script" not in body and b"zzz" not in body


def test_the_answer_of_the_source_is_passed_through() -> None:
    stub = DetailStub(DetailAnswer(404, b'{"api_error":{"code":"unknown_series","detail":""}}'))
    with running(stub) as connection:
        status, _, body = fetch(connection, "/detail.json?" + GOOD)
    assert status == 404 and json.loads(body)["api_error"]["code"] == "unknown_series"


def test_a_failing_source_is_a_generic_500() -> None:
    with running(DetailStub(RuntimeError("secret path /x"))) as connection:
        status, _, body = fetch(connection, "/detail.json?" + GOOD)
    assert status == 500 and b"secret" not in body


def test_the_static_files_are_served_from_the_table() -> None:
    with running(DetailStub()) as connection:
        for path, file in (
            ("/detail.js", TOOL / "detail.js"),
            ("/vendor/lightweight-charts.js", TOOL / "vendor" / "lightweight-charts.js"),
        ):
            status, headers, body = fetch(connection, path)
            assert status == 200 and body == file.read_bytes()
            assert headers["content-type"] == "text/javascript; charset=utf-8"
            assert headers["x-content-type-options"] == "nosniff" and headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "path",
    ["/vendor/../server.py", "/vendor/x.js", "/detail.js?x=1", "/vendor/", "/vendor", "/Detail.js"],
)
def test_nothing_else_is_served(path: str) -> None:
    with running(DetailStub()) as connection:
        assert fetch(connection, path)[0] == 404


def test_detail_json_accepts_get_only() -> None:
    with running(DetailStub()) as connection:
        assert fetch(connection, "/detail.json?" + GOOD, "POST")[0] == 405


def test_without_a_detail_source_the_route_is_not_found() -> None:
    with running(None) as connection:
        assert fetch(connection, "/detail.json?" + GOOD)[0] == 404


def test_a_symbol_with_reserved_characters_arrives_decoded() -> None:
    stub = DetailStub()
    with running(stub) as connection:
        fetch(
            connection, "/detail.json?symbol=" + quote("A&B#C D/USDT:USDT", safe="") + "&venue=v&timeframe=5m&range=1h"
        )
    assert stub.calls[0][0] == "A&B#C D/USDT:USDT"


def test_a_bad_escape_is_refused_not_kept_literally() -> None:
    stub = DetailStub()
    with running(stub) as connection:
        for symbol in ("%ZZ", "100%", "A%2", "%"):
            status, _, body = fetch(connection, f"/detail.json?symbol={symbol}&venue=v&timeframe=5m&range=1h")
            assert status == 400 and json.loads(body)["api_error"]["detail"] == "query"
        assert (
            fetch(connection, "/detail.json?symbol=100%25&venue=v&timeframe=5m&range=1h")[0] == 200
        )  # an encoded % is fine
    assert stub.calls == [("100%", "v", "5m", "1h")]
