import json
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from quasar_market_data.api.app import create_app
from quasar_market_data.api.open_interest_rule import rule_from_settings, symbols_from_settings
from quasar_market_data.config.settings import Instrument, parse_settings
from quasar_market_data.domain.open_interest_coverage import OpenInterestRule
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import open_reader, open_writer
from tests.api.seed import AT, SAMPLE_RULE, SYMBOL, TIMEFRAME, VENUE, insert_candle, make_database, register
from tests.api.test_health_endpoints import BROKEN, assert_error
from tests.api.test_open_interest_endpoint import contract_example
from tests.config_files import config_text

TF = 300_000
S = 1792643400000
ETH = "ETH/USDT:USDT"
LTC = "LTC/USDT:USDT"
BTC_ETH = (Instrument(VENUE, SYMBOL, TIMEFRAME), Instrument(VENUE, ETH, TIMEFRAME))
LISTED_RULE = OpenInterestRule(cadence=1, expected_after_ms=500_000, lag_threshold_ms=60_000, period=Timeframe("5m"))
ENTRY_KEYS = ["venue", "symbol", "timeframe", "newest_open_time_ms", "open_interest_period"]


def slot(n: int) -> int:
    return S + n * TF


def client_for(path: Path, universe: Sequence[Instrument], **kwargs: Any) -> TestClient:
    kwargs.setdefault("open_interest_symbols", frozenset())  # open interest is off
    return TestClient(create_app(path, rule=SAMPLE_RULE, universe=universe, **kwargs), raise_server_exceptions=False)


def entry(symbol: str, newest: int | None, period: str | None = None) -> dict[str, Any]:
    return {
        "venue": VENUE,
        "symbol": symbol,
        "timeframe": TIMEFRAME,
        "newest_open_time_ms": newest,
        "open_interest_period": period,
    }


def test_lists_configured_instruments_with_newest_or_null(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db", candles=[slot(0), slot(1), slot(2)])

    response = client_for(path, BTC_ETH).get("/v1/instruments")

    assert response.status_code == 200
    assert response.json() == {"instruments": [entry(SYMBOL, slot(2)), entry(ETH, None)]}
    assert [list(e) for e in response.json()["instruments"]] == [ENTRY_KEYS] * 2


def test_a_stored_series_that_is_not_configured_is_absent(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db", candles=[slot(0)], symbol=LTC)

    body = client_for(path, (Instrument(VENUE, SYMBOL, TIMEFRAME),)).get("/v1/instruments").json()

    assert body == {"instruments": [entry(SYMBOL, None)]}


def test_order_is_by_triple_whatever_the_universe_order(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db")
    universe = (
        Instrument("bybit", SYMBOL, "5m"),
        Instrument(VENUE, SYMBOL, "5m"),
        Instrument(VENUE, ETH, "5m"),
        Instrument(VENUE, ETH, "15m"),
    )
    expected = sorted((i.venue, i.symbol, i.timeframe) for i in universe)

    for given in (universe, tuple(reversed(universe))):
        body = client_for(path, given).get("/v1/instruments").json()
        assert [(e["venue"], e["symbol"], e["timeframe"]) for e in body["instruments"]] == expected


def test_create_app_holds_the_universe_sorted_by_triple(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = make_database(tmp_path / "market.db")
    seen: list[Sequence[Instrument]] = []

    def spy(db_path: Path, universe: Sequence[Instrument], **kwargs: Any) -> tuple[()]:
        seen.append(universe)
        return ()

    monkeypatch.setattr("quasar_market_data.api.app.read_instruments", spy)

    client_for(path, tuple(reversed(BTC_ETH))).get("/v1/instruments")

    assert seen == [tuple(sorted(BTC_ETH))]


# --- open_interest_period (contract 6.4.2, ADR-0003 A-6) -----------------------------------------------------


def settings_with_table(*, enabled: bool, listed: Sequence[str] | str) -> Any:
    """A file of BTC and ETH with an ``[open_interest]`` table, as the API reads it at start."""
    return parse_settings(
        config_text()
        + "\n[open_interest]\n"
        + f"enabled = {'true' if enabled else 'false'}\n"
        + 'period = "5m"\n'
        + "publication_delay_ms = 15000\n"
        + "round_offset_ms = 200000\n"
        + "budget_per_5min = 400\n"
        + f"symbols = {json.dumps(listed if isinstance(listed, str) else list(listed))}\n"
        + "backfill_days = 29\n",
        base_dir=Path("/cfg"),
    )


def periods(body: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    return [(e["symbol"], e["timeframe"], e["open_interest_period"]) for e in body["instruments"]]


def test_listed_symbols_carry_the_period_on_every_timeframe(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db")
    universe = (
        Instrument(VENUE, SYMBOL, "15m"),
        Instrument(VENUE, SYMBOL, "5m"),
        Instrument(VENUE, ETH, "5m"),
    )
    rule = OpenInterestRule(cadence=1, expected_after_ms=500_000, lag_threshold_ms=60_000, period=Timeframe("15m"))

    client = client_for(path, universe, open_interest=rule, open_interest_symbols=frozenset({(VENUE, SYMBOL)}))
    body = client.get("/v1/instruments").json()

    # the configured period of the rule, not the timeframe of the entry
    assert periods(body) == [(SYMBOL, "15m", "15m"), (SYMBOL, "5m", "15m"), (ETH, "5m", None)]


def test_unlisted_symbols_carry_null(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db", candles=[slot(0)])

    client = client_for(path, BTC_ETH, open_interest=LISTED_RULE, open_interest_symbols=frozenset({(VENUE, ETH)}))
    body = client.get("/v1/instruments").json()

    assert body == {"instruments": [entry(SYMBOL, slot(0), None), entry(ETH, None, "5m")]}
    other_venue = frozenset({("bybit", SYMBOL)})  # the venue is part of the pair
    body = client_for(path, BTC_ETH, open_interest=LISTED_RULE, open_interest_symbols=other_venue).get(
        "/v1/instruments"
    )
    assert periods(body.json()) == [(SYMBOL, "5m", None), (ETH, "5m", None)]


@pytest.mark.parametrize(
    "wiring",
    [
        pytest.param({}, id="absent-table-by-default"),
        pytest.param({"open_interest": None, "open_interest_symbols": frozenset()}, id="no-rule-no-symbols"),
        pytest.param(
            {"open_interest": None, "open_interest_symbols": frozenset({(VENUE, SYMBOL)})}, id="no-rule-but-a-set"
        ),
        pytest.param({"open_interest": LISTED_RULE, "open_interest_symbols": frozenset()}, id="rule-and-no-symbol"),
    ],
)
def test_disabled_or_absent_open_interest_gives_null_everywhere(tmp_path: Path, wiring: dict[str, Any]) -> None:
    path = make_database(tmp_path / "market.db", candles=[slot(0)])

    body = client_for(path, BTC_ETH, **wiring).get("/v1/instruments").json()

    assert periods(body) == [(SYMBOL, "5m", None), (ETH, "5m", None)]


def test_a_disabled_table_that_lists_the_symbol_gives_null_everywhere(tmp_path: Path) -> None:
    """``enabled = false`` with ``symbols`` listing the symbol, wired as ``api/cli.py`` wires it."""
    path = make_database(tmp_path / "market.db", candles=[slot(0)])
    settings = settings_with_table(enabled=False, listed=[SYMBOL])
    assert settings.open_interest is not None and SYMBOL in settings.open_interest.symbols  # listed, yet off

    client = client_for(
        path,
        settings.instruments(),
        open_interest=rule_from_settings(settings),
        open_interest_symbols=symbols_from_settings(settings),
    )

    assert periods(client.get("/v1/instruments").json()) == [(SYMBOL, "5m", None), (ETH, "5m", None)]


def test_an_enabled_table_gives_the_period_of_the_file(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db")
    settings = settings_with_table(enabled=True, listed=[ETH])

    client = client_for(
        path,
        settings.instruments(),
        open_interest=rule_from_settings(settings),
        open_interest_symbols=symbols_from_settings(settings),
    )

    assert periods(client.get("/v1/instruments").json()) == [(SYMBOL, "5m", None), (ETH, "5m", "5m")]


def test_order_and_other_fields_unchanged(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db", candles=[slot(0), slot(1)])
    universe = tuple(reversed(BTC_ETH))

    off = client_for(path, universe).get("/v1/instruments").json()["instruments"]
    on = (
        client_for(path, universe, open_interest=LISTED_RULE, open_interest_symbols=frozenset({(VENUE, SYMBOL)}))
        .get("/v1/instruments")
        .json()["instruments"]
    )

    assert [list(e) for e in off] == [list(e) for e in on] == [ENTRY_KEYS] * 2  # the new key is the last
    assert [{k: v for k, v in e.items() if k != "open_interest_period"} for e in on] == [
        {k: v for k, v in e.items() if k != "open_interest_period"} for e in off
    ]
    assert [(e["symbol"], e["newest_open_time_ms"]) for e in on] == [(SYMBOL, slot(1)), (ETH, None)]


def test_the_period_is_not_read_from_the_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = make_database(tmp_path / "market.db")  # no open interest row, no table content to learn it from
    seen: list[int] = []

    def spy(db_path: Path, universe: Sequence[Instrument], **kwargs: Any) -> tuple[()]:
        seen.append(len(universe))
        return ()

    monkeypatch.setattr("quasar_market_data.api.app.read_instruments", spy)

    client = client_for(path, BTC_ETH, open_interest=LISTED_RULE, open_interest_symbols=frozenset({(VENUE, SYMBOL)}))

    assert client.get("/v1/instruments").json() == {"instruments": []}  # the reader decides the entries, only
    assert seen == [2]


def test_the_contract_example_is_what_the_server_returns(tmp_path: Path) -> None:
    """The scenario of the 6.4.2 example: BTC is an open interest symbol with a stored candle, LTC is not stored."""
    example = contract_example("### 6.4.2 Response `200`")
    btc, ltc = example["instruments"]
    path = make_database(tmp_path / "market.db", candles=[btc["newest_open_time_ms"]])
    universe = (Instrument(VENUE, btc["symbol"], btc["timeframe"]), Instrument(VENUE, ltc["symbol"], ltc["timeframe"]))

    client = client_for(
        path, universe, open_interest=LISTED_RULE, open_interest_symbols=frozenset({(VENUE, btc["symbol"])})
    )
    response = client.get("/v1/instruments")

    assert response.status_code == 200
    assert response.json() == example
    assert response.text == json.dumps(example, ensure_ascii=False, separators=(",", ":"))  # key order, compact
    assert btc["open_interest_period"] == "5m" and ltc["open_interest_period"] is None


def test_body_is_exactly_the_instruments_key(tmp_path: Path) -> None:
    body = client_for(make_database(tmp_path / "market.db"), BTC_ETH).get("/v1/instruments").json()

    assert set(body) == {"instruments"}


@pytest.mark.parametrize(("reason", "make"), BROKEN)
def test_503_when_the_database_is_not_ready(tmp_path: Path, reason: str, make: Callable[[Path], Path]) -> None:
    response = client_for(make(tmp_path / "market.db"), BTC_ETH).get("/v1/instruments")

    error = assert_error(response, 503, "not_ready")
    assert error["details"] == {"reason": reason}
    assert response.headers.get("retry-after") == (None if reason == "schema_mismatch" else "5")


@pytest.mark.parametrize("query", ["x=1", "=", "a=1&a=2", "x", "%FF"])
def test_a_query_parameter_is_400(tmp_path: Path, query: str) -> None:
    client = client_for(make_database(tmp_path / "market.db"), BTC_ETH)

    assert_error(client.get(f"/v1/instruments?{query}"), 400, "invalid_parameter")


def test_an_empty_query_is_accepted(tmp_path: Path) -> None:
    assert client_for(make_database(tmp_path / "market.db"), BTC_ETH).get("/v1/instruments?").status_code == 200


def test_no_store_utf8_json_and_405_for_post(tmp_path: Path) -> None:
    client = client_for(make_database(tmp_path / "market.db"), BTC_ETH)

    response = client.get("/v1/instruments")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    for method in ("POST", "PUT", "DELETE"):
        refused = client.request(method, "/v1/instruments")
        assert assert_error(refused, 405, "method_not_allowed")["details"] == {"allow": "GET"}
    assert_error(client.get("/v1/instruments/"), 404, "not_found")


def _bulk(path: Path, rows: int) -> None:
    make_database(path)
    conn = open_writer(path)
    conn.execute("BEGIN")
    register(conn, "candles")
    conn.executemany(
        "INSERT INTO candles (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume, turnover,"
        f" ingested_at_ms) VALUES (?, ?, ?, ?, '1', '2', '1', '1', '1', NULL, {AT})",
        [(VENUE, SYMBOL, TIMEFRAME, slot(k)) for k in range(rows)],
    )
    conn.execute("COMMIT")
    conn.close()


def _traced(path: Path, universe: Sequence[Instrument]) -> tuple[list[str], int]:
    statements: list[str] = []
    steps = 0

    def tick() -> int:
        nonlocal steps
        steps += 1
        return 0

    def factory(p: Path) -> sqlite3.Connection:
        conn = open_reader(p)
        conn.set_trace_callback(statements.append)
        conn.set_progress_handler(tick, 1)
        return conn

    assert client_for(path, universe, open_connection=factory).get("/v1/instruments").status_code == 200
    return statements, steps


def test_reads_one_seek_per_configured_instrument(tmp_path: Path) -> None:
    small, large = tmp_path / "small.db", tmp_path / "large.db"
    _bulk(small, 3)
    _bulk(large, 30_000)
    universe = tuple(Instrument(VENUE, f"S{i}/USDT:USDT", TIMEFRAME) for i in range(7)) + BTC_ETH[:1]

    small_statements, small_steps = _traced(small, universe)
    large_statements, large_steps = _traced(large, universe)

    for statements in (small_statements, large_statements):
        assert len([s for s in statements if "MAX(open_time_ms)" in s]) == len(universe)
        assert not any("GROUP BY" in s for s in statements)
    assert large_steps < 2 * small_steps + 100  # the work does not grow with the rows of the series


def test_the_answer_is_one_snapshot(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db", candles=[slot(0)])
    fired: list[int] = []

    def factory(p: Path) -> Any:
        real = open_reader(p)

        class Proxy:
            def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
                cursor = real.execute(sql, params)
                if "MAX(open_time_ms)" in sql and not fired:
                    fired.append(1)  # after the first series was read, a writer commits to both series
                    writer = open_writer(p)
                    insert_candle(writer, slot(9), symbol=SYMBOL)
                    insert_candle(writer, slot(9), symbol=ETH)
                    writer.close()
                return cursor

            def close(self) -> None:
                real.close()

        return Proxy()

    client = client_for(path, BTC_ETH, open_connection=factory)
    first = client.get("/v1/instruments").json()
    second = client_for(path, BTC_ETH).get("/v1/instruments").json()

    assert first == {"instruments": [entry(SYMBOL, slot(0)), entry(ETH, None)]}  # ETH is not seen at slot 9
    assert second == {"instruments": [entry(SYMBOL, slot(9)), entry(ETH, slot(9))]}
