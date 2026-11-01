import json
import socket

import pytest

from tests.tools.symbol_overview.fake_api import FakeApi, FakeReply
from tests.tools.symbol_overview.helpers import T0, instrument
from tools.symbol_overview.fetch import RawResponse, TransportError, coverage_path, http_fetch
from tools.symbol_overview.model import MAX_BODY_BYTES
from tools.symbol_overview.window import window_for

W = window_for(T0)
OK = FakeReply(200, b"{}")


def test_coverage_path_has_exactly_the_documented_parameters_in_order() -> None:
    path = coverage_path(instrument(), W)
    assert path == (f"/v1/coverage?venue=binance-usdm&symbol=BTC/USDT:USDT&timeframe=5m&start={W.start}&end={W.end}")


@pytest.mark.parametrize(
    ("symbol", "encoded"),
    [
        ("A&B/USDT:USDT", "A%26B/USDT:USDT"),
        ("1000+X/USDT:USDT", "1000%2BX/USDT:USDT"),
        ("BTC USDT", "BTC%20USDT"),
        ("币安/USDT:USDT", "%E5%B8%81%E5%AE%89/USDT:USDT"),
    ],
)
def test_coverage_path_percent_encodes(symbol: str, encoded: str) -> None:
    path = coverage_path(instrument(symbol), W)
    assert f"symbol={encoded}&" in path
    with FakeApi({"/v1/coverage": lambda query: OK}) as api:
        http_fetch(api.url)(path)
        assert api.queries[0]["symbol"] == symbol  # the API decodes it back to the original
        assert set(api.queries[0]) == {"venue", "symbol", "timeframe", "start", "end"}


@pytest.mark.parametrize("path", ["/v1/candles?x=1", "/health", "//v1/coverage", "/v1/instruments/x", "v1/instruments"])
def test_http_fetch_refuses_other_paths(path: str) -> None:
    with FakeApi({}) as api:
        with pytest.raises(ValueError):
            http_fetch(api.url)(path)
        assert api.requests == []


def test_http_fetch_returns_status_content_type_and_body_for_a_non_200() -> None:
    error = json.dumps({"error": {"code": "not_ready"}}).encode()
    with FakeApi({"/health/ingest": lambda query: FakeReply(503, error)}) as api:
        assert http_fetch(api.url)("/health/ingest") == RawResponse(503, "application/json", error)


def test_timeout_is_a_transport_error() -> None:
    with FakeApi({"/health/ingest": lambda query: OK}, delay_s=0.5) as api:
        with pytest.raises(TransportError) as raised:
            http_fetch(api.url, timeout_s=0.05)("/health/ingest")
        assert raised.value.code == "timeout"


def test_connection_refused_is_a_transport_error() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(TransportError) as raised:
        http_fetch(f"http://127.0.0.1:{port}")("/health/ingest")
    assert raised.value.code == "connection"


def test_body_is_capped_at_the_limit_plus_one_byte() -> None:
    big = FakeReply(200, b"x" * (MAX_BODY_BYTES + 5000))
    with FakeApi({"/v1/instruments": lambda query: big}) as api:
        response = http_fetch(api.url)("/v1/instruments")
    assert len(response.body) == MAX_BODY_BYTES + 1


def test_a_redirect_is_not_followed() -> None:
    redirect = FakeReply(302, b"", headers={"Location": "/elsewhere"})
    with FakeApi({"/health/ingest": lambda query: redirect, "/elsewhere": lambda query: OK}) as api:
        assert http_fetch(api.url)("/health/ingest").status == 302
        assert api.requests == ["/health/ingest"]
