import asyncio
import json
import logging
import socket
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.testclient import TestClient

from quasar_market_data.api import app as app_module
from quasar_market_data.api.app import create_app
from quasar_market_data.api.candle_reader import CandlesPage
from quasar_market_data.api.database import read_candles
from quasar_market_data.api.params import CandlesRequest
from quasar_market_data.storage.connection import open_writer
from tests.api.seed import insert_candle, make_database, make_database_from_fixture

TF = 300_000
S = 1792643400000
Q = f"venue=binance-usdm&symbol=BTC/USDT:USDT&timeframe=5m&start={S}"


def slot(n: int) -> int:
    return S + n * TF


def client_for(path: Path, **kwargs: Any) -> TestClient:
    return TestClient(create_app(path, **kwargs), raise_server_exceptions=False)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return make_database(tmp_path / "market.db", candles=[slot(0), slot(1), slot(2)])


def assert_error(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status
    body: dict[str, Any] = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == code
    error: dict[str, Any] = body["error"]
    return error


def test_the_contract_example_is_what_the_server_returns(tmp_path: Path) -> None:
    client = client_for(make_database_from_fixture(tmp_path / "market.db"))

    response = client.get(f"/v1/candles?{Q}&limit=3")

    assert response.status_code == 200
    assert response.json() == {
        "venue": "binance-usdm",
        "symbol": "BTC/USDT:USDT",
        "timeframe": "5m",
        "start": 1792643400000,
        "end": 1792673400000,
        "series": {"oldest_open_time_ms": 1792643400000, "newest_open_time_ms": 1792673100000},
        "candles": [
            {
                "open_time_ms": 1792643400000,
                "open": "82855.4",
                "high": "82866.1",
                "low": "82768.1",
                "close": "82768.2",
                "volume": "369.221",
                "turnover": "30587379.8759",
                "conflicted": False,
            },
            {
                "open_time_ms": 1792643700000,
                "open": "82768.2",
                "high": "82768.2",
                "low": "82664.7",
                "close": "82709",
                "volume": "580.95",
                "turnover": "48054366.2802",
                "conflicted": False,
            },
            {
                "open_time_ms": 1792644000000,
                "open": "82709.1",
                "high": "82765.3",
                "low": "82686.2",
                "close": "82744.5",
                "volume": "380.864",
                "turnover": "31506256.9708",
                "conflicted": False,
            },
        ],
        "next": 1792644300000,
        "coverage": {
            "span": {"start": 1792643400000, "end": 1792644300000},
            "slots": 3,
            "present_slots": 3,
            "conflicted_slots": 0,
            "unavailable_slots": 0,
            "missing_slots": 0,
            "beyond_stored_slots": 0,
            "gap_free": True,
        },
    }
    assert list(response.json()) == [
        "venue", "symbol", "timeframe", "start", "end", "series", "candles", "next", "coverage"
    ]  # fmt: skip


def test_success_headers(db: Path, tmp_path: Path) -> None:
    client = client_for(db)
    responses = [
        client.get(f"/v1/candles?{Q}"),
        client.get("/v1/candles"),  # 400
        client.get(f"/v1/candles?{Q.replace('BTC/USDT:USDT', 'X/Y')}"),  # 404 unknown series
        client.get("/nope"),  # 404 not found
        client.post("/v1/candles"),  # 405
        client_for(tmp_path / "missing.db").get(f"/v1/candles?{Q}"),  # 503
    ]

    assert [r.status_code for r in responses] == [200, 400, 404, 404, 405, 503]
    for response in responses:
        assert response.headers["content-type"] == "application/json; charset=utf-8"
        assert response.headers["cache-control"] == "no-store"


def test_decimals_are_json_strings_and_turnover_may_be_null(tmp_path: Path) -> None:
    client = client_for(make_database_from_fixture(tmp_path / "market.db"))

    text = client.get(f"/v1/candles?{Q}&limit=2").text

    assert '"close":"82709"' in text  # no ".0", still a string
    assert '"volume":"580.95"' in text
    odd = make_database(tmp_path / "odd.db", candles=[slot(1)])  # an odd slot has no turnover
    assert '"turnover":null' in client_for(odd).get(f"/v1/candles?{Q}").text


def test_a_non_ascii_symbol_round_trips_as_utf_8(tmp_path: Path) -> None:
    symbol = "测试/USDT:USDT"
    client = client_for(make_database(tmp_path / "market.db", candles=[slot(0)], symbol=symbol))

    response = client.get(f"/v1/candles?venue=binance-usdm&symbol={quote(symbol, safe='/:')}&timeframe=5m&start={S}")

    assert response.status_code == 200
    assert "测试/USDT:USDT" in response.content.decode("utf-8")
    assert "\\u" not in response.text
    assert response.json()["symbol"] == symbol


def test_default_limit_is_1000_and_limit_is_enforced(tmp_path: Path) -> None:
    client = client_for(make_database(tmp_path / "market.db", candles=[slot(i) for i in range(1001)]))

    first = client.get(f"/v1/candles?{Q}").json()
    assert len(first["candles"]) == 1000
    assert first["next"] == slot(1000)
    assert len(client.get(f"/v1/candles?{Q}&limit=2").json()["candles"]) == 2


def test_400_for_a_non_numeric_limit_not_a_framework_422(db: Path) -> None:
    error = assert_error(client_for(db).get(f"/v1/candles?{Q}&limit=abc"), 400, "invalid_parameter")

    assert error["details"] == {"parameter": "limit", "received": "abc"}


def test_400_for_a_bad_parameter(db: Path) -> None:
    error = assert_error(client_for(db).get(f"/v1/candles?{Q}&limit={'9' * 100}"), 400, "invalid_parameter")

    assert error["details"]["parameter"] == "limit"
    assert error["details"]["received"] == "9" * 64


def test_404_unknown_series_has_the_three_detail_keys(db: Path) -> None:
    error = assert_error(client_for(db).get(f"/v1/candles?{Q.replace('BTC/USDT:USDT', 'X/Y')}"), 404, "unknown_series")

    assert error["details"] == {"venue": "binance-usdm", "symbol": "X/Y", "timeframe": "5m"}


@pytest.mark.parametrize("path", ["/nope", "/docs", "/redoc", "/openapi.json", "/healthz", "/v1/candles/", "/"])
def test_404_not_found_for_unknown_paths_including_docs_and_openapi(db: Path, path: str) -> None:
    response = client_for(db).get(path, follow_redirects=False)

    assert_error(response, 404, "not_found")


def test_405_for_other_methods(db: Path) -> None:
    client = client_for(db)
    for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        response = client.request(method, "/v1/candles")
        error = assert_error(response, 405, "method_not_allowed")
        assert response.headers["allow"] == "GET"
        assert error["details"] == {"allow": "GET"}

    head = client.head("/v1/candles")
    assert head.status_code == 405
    assert head.headers["allow"] == "GET"
    assert head.headers["content-type"] == "application/json; charset=utf-8"
    assert head.content == b""  # HTTP forbids a body on HEAD

    assert_error(client.post("/nope"), 404, "not_found")


def test_503_database_missing_schema_mismatch_unreadable(db: Path, tmp_path: Path) -> None:
    missing = client_for(tmp_path / "missing.db").get(f"/v1/candles?{Q}")
    assert assert_error(missing, 503, "not_ready")["details"] == {"reason": "database_missing"}
    assert missing.headers["retry-after"] == "5"

    foreign = tmp_path / "foreign.db"
    raw = sqlite3.connect(foreign)
    raw.execute("PRAGMA application_id = 1")
    raw.execute("CREATE TABLE t (x)")
    raw.commit()
    raw.close()
    mismatch = client_for(foreign).get(f"/v1/candles?{Q}")
    assert assert_error(mismatch, 503, "not_ready")["details"] == {"reason": "schema_mismatch"}
    assert "retry-after" not in mismatch.headers

    def denied(path: Path) -> sqlite3.Connection:
        raise PermissionError("denied")

    unreadable = client_for(db, open_connection=denied).get(f"/v1/candles?{Q}")
    assert assert_error(unreadable, 503, "not_ready")["details"] == {"reason": "database_unreadable"}
    assert unreadable.headers["retry-after"] == "5"


def test_500_hides_internals(db: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    def boom(*args: Any, **kwargs: Any) -> CandlesPage:
        raise RuntimeError("secret /var/db/x SELECT")

    monkeypatch.setattr(app_module, "read_candles", boom)
    caplog.set_level(logging.ERROR, logger="quasar_market_data.api.app")

    response = client_for(db).get(f"/v1/candles?{Q}")

    error = assert_error(response, 500, "internal_error")
    assert error["details"] == {}
    assert not any(text in response.text for text in ("secret", "/var", "SELECT"))
    records = [r for r in caplog.records if r.name == "quasar_market_data.api.app" and r.levelno == logging.ERROR]
    assert len(records) == 1
    correlation_id = records[0].getMessage().rsplit(" ", 1)[-1].rstrip(")")
    assert len(correlation_id) == 32
    assert correlation_id not in response.text


def test_a_programming_error_in_the_reader_is_a_500_not_a_503(db: Path) -> None:
    def broken(path: Path) -> sqlite3.Connection:
        class Bad:
            def execute(self, *args: Any) -> Any:
                raise sqlite3.ProgrammingError("Incorrect number of bindings")

            def close(self) -> None:
                pass

        return Bad()  # type: ignore[return-value]

    assert_error(client_for(db, open_connection=broken).get(f"/v1/candles?{Q}"), 500, "internal_error")


def test_order_of_checks(db: Path, tmp_path: Path) -> None:
    missing = client_for(tmp_path / "missing.db")
    unknown = Q.replace("BTC/USDT:USDT", "X/Y")

    assert_error(missing.get("/v1/candles?limit=0"), 400, "invalid_parameter")
    assert_error(missing.get(f"/v1/candles?{unknown}"), 503, "not_ready")
    assert_error(client_for(db).get(f"/v1/candles?{unknown}"), 404, "unknown_series")


def test_the_database_is_read_off_the_event_loop(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = read_candles
    on_loop: list[bool] = []

    def spy(path: Path, request: CandlesRequest, **kwargs: Any) -> CandlesPage:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(path, request, **kwargs)

    monkeypatch.setattr(app_module, "read_candles", spy)

    assert client_for(db).get(f"/v1/candles?{Q}").status_code == 200
    assert on_loop == [False]


def test_the_app_has_no_global_state(tmp_path: Path) -> None:
    one = client_for(make_database(tmp_path / "one.db", candles=[slot(0)]))
    two = client_for(make_database(tmp_path / "two.db", candles=[slot(0), slot(1), slot(2)]))

    assert len(one.get(f"/v1/candles?{Q}").json()["candles"]) == 1
    assert len(two.get(f"/v1/candles?{Q}").json()["candles"]) == 3
    assert len(one.get(f"/v1/candles?{Q}").json()["candles"]) == 1


def test_the_network_guard_blocks_connects() -> None:
    with pytest.raises(AssertionError, match="must not open connections"):
        socket.create_connection(("127.0.0.1", 9))
    with socket.socket() as sock:
        with pytest.raises(AssertionError):
            sock.connect(("127.0.0.1", 9))
        with pytest.raises(AssertionError):
            sock.connect_ex(("127.0.0.1", 9))


def test_a_handler_that_connects_is_caught(
    db: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def connects(*args: Any, **kwargs: Any) -> CandlesPage:
        socket.create_connection(("127.0.0.1", 9))
        raise AssertionError("unreachable")

    monkeypatch.setattr(app_module, "read_candles", connects)
    caplog.set_level(logging.ERROR, logger="quasar_market_data.api.app")

    response = client_for(db).get(f"/v1/candles?{Q}")

    assert_error(response, 500, "internal_error")
    assert "the API tests must not open connections" in caplog.text


def test_a_candle_committed_by_a_writer_is_served(db: Path) -> None:
    writer = open_writer(db)
    insert_candle(writer, slot(3))
    writer.close()
    assert len(client_for(db).get(f"/v1/candles?{Q}").json()["candles"]) == 4


def test_response_is_compact_utf8_json(db: Path) -> None:
    response = client_for(db).get(f"/v1/candles?{Q}")

    assert json.loads(response.content) == response.json()
    assert b": " not in response.content


def test_the_allow_header_of_another_route_is_echoed_whatever_its_case(db: Path) -> None:
    app = create_app(db)

    @app.get("/two-methods")
    async def two_methods() -> None:
        raise StarletteHTTPException(405, headers={"Allow": "GET, POST"})

    response = TestClient(app, raise_server_exceptions=False).get("/two-methods")

    error = assert_error(response, 405, "method_not_allowed")
    assert response.headers["allow"] == "GET, POST"
    assert error["details"] == {"allow": "GET, POST"}


def test_the_slot_at_the_maximum_time_can_be_asked_for(tmp_path: Path) -> None:
    maximum = 4102444800000
    client = client_for(make_database(tmp_path / "market.db", candles=[slot(0)]))

    response = client.get(f"/v1/candles?venue=binance-usdm&symbol=BTC/USDT:USDT&timeframe=5m&start={maximum}")

    assert response.status_code == 200
    body = response.json()
    assert (body["start"], body["end"], body["candles"]) == (maximum, maximum + 300_000, [])
    follow_up = client.get(
        f"/v1/candles?venue=binance-usdm&symbol=BTC/USDT:USDT&timeframe=5m&start={maximum}&end={body['end']}"
    )
    assert follow_up.status_code == 200
