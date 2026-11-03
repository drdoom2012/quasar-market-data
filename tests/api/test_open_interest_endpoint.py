"""``GET /v1/open-interest`` (contract api-v1 6.5): body, order of checks, the P-15 cases of the issue, independence."""

import asyncio
import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from quasar_market_data.api import app as app_module
from quasar_market_data.api.app import create_app
from quasar_market_data.api.database import read_open_interest
from quasar_market_data.api.open_interest_page import OpenInterestPage
from quasar_market_data.api.open_interest_rule import rule_from_settings
from quasar_market_data.api.params import OpenInterestRequest
from quasar_market_data.config.settings import parse_settings
from quasar_market_data.domain.open_interest_coverage import OpenInterestRule
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import open_writer
from tests.api.seed import (
    SAMPLE_RULE,
    SAMPLE_UNIVERSE,
    insert_open_interest,
    insert_open_interest_conflict,
    make_database,
)
from tests.config_files import config_text

P5 = Timeframe("5m")
TF = 300_000
E = 500_000
L = 60_000
RULE = OpenInterestRule(cadence=1, expected_after_ms=E, lag_threshold_ms=L, period=P5)
S = 1792672500000
H_SLOTS = 8_352  # 29 days of 5m
CONTRACT = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "api-v1.md"
Q = f"venue=binance-usdm&symbol=BTC/USDT:USDT&period=5m&start={S}"


def slot(n: int) -> int:
    return S + n * TF


NOW = S + 100 * TF


def client_for(path: Path, now: int | Callable[[], int] = NOW, **kwargs: Any) -> TestClient:
    kwargs.setdefault("rule", SAMPLE_RULE)
    kwargs.setdefault("universe", SAMPLE_UNIVERSE)
    kwargs.setdefault("open_interest", RULE)
    now_ms = now if callable(now) else (lambda: now)
    return TestClient(create_app(path, now_ms=now_ms, **kwargs), raise_server_exceptions=False)


def assert_error(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status
    body: dict[str, Any] = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == code
    error: dict[str, Any] = body["error"]
    return error


def contract_example(heading: str, *, fragment: bool = False) -> dict[str, Any]:
    """The first ```json block after ``heading`` in the contract: the example the server must reproduce.

    ``fragment=True`` wraps a block that is a single ``"key": {...}`` member in braces.
    """
    text = CONTRACT.read_text(encoding="utf-8")
    after = text[text.index(heading) :]
    block = re.search(r"```json\n(.*?)\n```", after, re.DOTALL)
    assert block is not None
    return json.loads("{" + block.group(1) + "}" if fragment else block.group(1))  # type: ignore[no-any-return]


def example_database(path: Path) -> Path:
    """Rows of the contract example (6.5.4): the series starts earlier, slots 0, 1 (conflicted) and 3 are stored."""
    make_database(path)
    conn = open_writer(path)
    try:
        insert_open_interest(conn, 1792643700000, open_interest="95000.1", open_interest_value="7000000000.5")
        insert_open_interest(conn, 1792672500000, open_interest="95677.779", open_interest_value="7446123150.675")
        insert_open_interest(conn, 1792672800000, open_interest="95690.12", open_interest_value="7447031002.4")
        insert_open_interest(conn, 1792673400000, open_interest="95702.5", open_interest_value="7447990111.25")
        insert_open_interest_conflict(conn, 1792672800000, 1)
    finally:
        conn.close()
    return path


# --- the body ------------------------------------------------------------------------------------------------


def test_the_contract_example_is_what_the_server_returns(tmp_path: Path) -> None:
    example = contract_example("### 6.5.4 Response `200`")
    client = client_for(example_database(tmp_path / "m.db"), example["as_of_ms"])

    response = client.get(f"/v1/open-interest?{Q}&end={example['end']}")

    assert response.status_code == 200
    assert response.json() == example
    assert response.text == json.dumps(example, ensure_ascii=False, separators=(",", ":"))  # key order, compact
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/json; charset=utf-8"


def test_the_rule_echoes_what_the_states_were_computed_with(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest=[slot(0)])
    rule = OpenInterestRule(5, 1_700_000, 90_000, P5)

    body = client_for(path, open_interest=rule).get(f"/v1/open-interest?{Q}").json()

    assert body["rule"] == {
        "cadence": 5,
        "expected_after_ms": 1_700_000,
        "lag_threshold_ms": 90_000,
        "retrieval_horizon_ms": 29 * 86_400_000,
    }


@pytest.mark.parametrize(("budget", "symbols", "cadence"), [(100, 10, 1), (200, 300, 5), (400, 300, 3)])
def test_the_rule_in_the_body_is_the_one_derived_from_the_file(
    tmp_path: Path, budget: int, symbols: int, cadence: int
) -> None:
    """P-15: the API and the Collector derive the same ``c`` from the same file (b = 100, 200, 400)."""
    text = config_text(symbols=tuple(f"C{i:04d}/USDT:USDT" for i in range(symbols))) + (
        '\n[open_interest]\nenabled = true\nperiod = "5m"\npublication_delay_ms = 15000\n'
        f"round_offset_ms = 200000\nbudget_per_5min = {budget}\n"
    )
    rule = rule_from_settings(parse_settings(text, base_dir=tmp_path))
    path = make_database(tmp_path / "m.db", open_interest=[slot(0)])

    body = client_for(path, open_interest=rule).get(f"/v1/open-interest?{Q}").json()

    assert body["rule"]["cadence"] == cadence
    assert body["rule"]["expected_after_ms"] == cadence * TF + 200_000


# --- disabled, order of checks, errors --------------------------------------------------------------------------


def test_disabled_is_404_and_opens_no_database(tmp_path: Path) -> None:
    def no_connection(path: Path) -> sqlite3.Connection:
        raise AssertionError("the database must not be opened")

    client = client_for(
        make_database(tmp_path / "m.db", open_interest=[slot(0)]), open_interest=None, open_connection=no_connection
    )

    error = assert_error(client.get(f"/v1/open-interest?{Q}"), 404, "open_interest_disabled")
    assert error["details"] == {}
    assert_error(client.get(f"/v1/open-interest?{Q}&limit=0"), 400, "invalid_parameter")  # parameters first


def test_the_other_endpoints_work_when_open_interest_is_disabled(tmp_path: Path) -> None:
    client = client_for(make_database(tmp_path / "m.db", candles=[slot(0)]), open_interest=None)

    assert client.get("/health/ingest").status_code == 200
    assert client.get("/v1/instruments").status_code == 200


def test_order_of_checks(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    unknown = Q.replace("BTC/USDT:USDT", "X/Y")
    present = make_database(tmp_path / "m.db", open_interest=[slot(0)])

    assert_error(client_for(missing).get("/v1/open-interest?limit=0"), 400, "invalid_parameter")
    assert_error(client_for(missing, open_interest=None).get(f"/v1/open-interest?{Q}"), 404, "open_interest_disabled")
    assert_error(
        client_for(missing, open_interest=None, now=0).get(f"/v1/open-interest?{Q}"), 404, "open_interest_disabled"
    )
    assert_error(client_for(missing, now=0).get(f"/v1/open-interest?{Q}"), 500, "internal_error")  # clock before 503
    assert_error(client_for(missing).get(f"/v1/open-interest?{unknown}"), 503, "not_ready")
    error = assert_error(client_for(present).get(f"/v1/open-interest?{unknown}"), 404, "unknown_series")
    assert error["details"] == {"venue": "binance-usdm", "symbol": "X/Y", "period": "5m"}
    assert error["message"] == "no open interest row of this series has been stored"


def test_a_series_with_only_candles_is_an_unknown_series(tmp_path: Path) -> None:
    client = client_for(make_database(tmp_path / "m.db", candles=[slot(0)]))

    assert_error(client.get(f"/v1/open-interest?{Q}"), 404, "unknown_series")


def test_method_path_and_trailing_slash_errors(tmp_path: Path) -> None:
    client = client_for(make_database(tmp_path / "m.db", open_interest=[slot(0)]))

    post = client.post(f"/v1/open-interest?{Q}")
    assert_error(post, 405, "method_not_allowed")
    assert post.headers["allow"] == "GET"
    head = client.head(f"/v1/open-interest?{Q}")  # a HEAD response carries no body
    assert (head.status_code, head.headers["allow"]) == (405, "GET")
    assert_error(client.get(f"/v1/open-interest/?{Q}"), 404, "not_found")


# --- the cases of ADR-0003 P-15 named by the issue --------------------------------------------------------------


def test_a_conflicted_slot_is_flagged_in_the_row_and_counted(tmp_path: Path) -> None:
    path = make_database(
        tmp_path / "m.db", open_interest=[slot(0), slot(1), slot(2)], open_interest_conflicts=[slot(1)]
    )

    body = client_for(path).get(f"/v1/open-interest?{Q}&end={slot(3)}").json()

    assert [row["conflicted"] for row in body["rows"]] == [False, True, False]
    assert body["coverage"]["conflicted_slots"] == 1 and body["coverage"]["gap_free"] is False


def test_a_row_absent_after_the_newest_walks_not_yet_expected_due_stale_and_is_never_present_until_stored(
    tmp_path: Path,
) -> None:
    """The whole series stopped at slot 0; slot 1 never arrives. Then it arrives, and only then it is present."""
    path = make_database(tmp_path / "m.db", open_interest=[slot(0)])
    window = f"/v1/open-interest?{Q}&end={slot(2)}"

    def slot_one_state(now: int) -> str:
        body = client_for(path, now).get(window).json()
        counts: dict[str, int] = {k: v for k, v in body["coverage"].items() if k.endswith("_slots") and v}
        assert "present_slots" in counts  # slot 0
        others = [name.removesuffix("_slots") for name in counts if name != "present_slots"]
        assert len(others) == 1
        return others[0]

    t = slot(1)
    assert slot_one_state(t + E - 1) == "not_yet_expected"
    assert slot_one_state(t + E) == "due"
    assert slot_one_state(t + E + L) == "due"
    assert slot_one_state(t + E + L + 1) == "stale"
    assert slot_one_state(t + 100 * E) == "stale"

    conn = open_writer(path)
    insert_open_interest(conn, slot(1))
    conn.close()

    body = client_for(path, t + E + L + 1).get(window).json()
    assert body["coverage"]["present_slots"] == 2 and body["coverage"]["gap_free"] is True


def test_an_absent_row_with_newer_rows_is_not_yet_expected_then_missing_never_due_or_stale(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest=[slot(0), slot(2)])
    window = f"/v1/open-interest?{Q}&end={slot(3)}"

    early = client_for(path, slot(1) + E - 1).get(window).json()["coverage"]
    late = client_for(path, slot(1) + E).get(window).json()["coverage"]

    assert (early["not_yet_expected_slots"], early["missing_slots"]) == (1, 0)
    assert (late["not_yet_expected_slots"], late["missing_slots"]) == (0, 1)
    assert early["due_slots"] == early["stale_slots"] == late["due_slots"] == late["stale_slots"] == 0


def test_history_ending_forty_days_before_as_of_counts_every_state_exactly(tmp_path: Path) -> None:
    as_of = S + 7 * TF  # aligned
    newest = as_of - 40 * 86_400_000
    path = make_database(tmp_path / "m.db", open_interest=[newest])
    start = as_of - 10_000 * TF

    body = client_for(path, as_of).get(
        f"/v1/open-interest?venue=binance-usdm&symbol=BTC/USDT:USDT&period=5m&start={start}&end={as_of}"
    )

    c = body.json()["coverage"]
    assert body.status_code == 200
    assert (c["slots"], c["no_longer_recoverable_slots"], c["not_yet_expected_slots"]) == (10_000, 1_648, 1)
    assert (c["due_slots"], c["stale_slots"], c["present_slots"], c["missing_slots"]) == (0, 8_351, 0, 0)
    too_long = f"venue=binance-usdm&symbol=BTC/USDT:USDT&period=5m&start={as_of - 41 * 86_400_000}&end={as_of}"
    assert_error(client_for(path, as_of).get(f"/v1/open-interest?{too_long}"), 400, "invalid_parameter")


def test_a_defaulted_end_cannot_show_the_clock_states_but_an_explicit_end_can(tmp_path: Path) -> None:
    """Decision OA10: a stopped series answers ``gap_free: true`` for a defaulted window."""
    path = make_database(tmp_path / "m.db", open_interest=[slot(0), slot(1)])
    now = slot(1) + 50 * E

    defaulted = client_for(path, now).get(f"/v1/open-interest?{Q}").json()["coverage"]
    explicit = client_for(path, now).get(f"/v1/open-interest?{Q}&end={slot(3)}").json()["coverage"]

    assert defaulted["gap_free"] is True and defaulted["stale_slots"] == 0
    assert explicit["gap_free"] is False and explicit["stale_slots"] == 1 and explicit["not_yet_expected_slots"] == 0


# --- independence -------------------------------------------------------------------------------------------


def test_the_database_is_read_off_the_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = read_open_interest
    on_loop: list[bool] = []

    def spy(path: Path, request: OpenInterestRequest, **kwargs: Any) -> OpenInterestPage:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(path, request, **kwargs)

    monkeypatch.setattr(app_module, "read_open_interest", spy)
    client = client_for(make_database(tmp_path / "m.db", open_interest=[slot(0)]))

    assert client.get(f"/v1/open-interest?{Q}").status_code == 200
    assert on_loop == [False]


def test_the_other_v1_endpoints_are_byte_identical_with_open_interest_rows_present(tmp_path: Path) -> None:
    candles = [slot(i) for i in range(200)]
    plain = make_database(tmp_path / "plain.db", candles=candles)
    with_oi = make_database(
        tmp_path / "oi.db",
        candles=candles,
        open_interest=[slot(i) for i in range(1, 200)],
        open_interest_conflicts=[slot(5)],
    )
    cq = f"venue=binance-usdm&symbol=BTC/USDT:USDT&timeframe=5m&start={slot(0)}"
    urls = [
        f"/v1/candles?{cq}&limit=50",
        f"/v1/coverage?{cq}&end={slot(250)}",
        f"/v1/indicators?{cq}&end={slot(150)}&indicators=rsi:period=2;ema:period=3",
        "/v1/instruments",
    ]

    for url in urls:
        a = client_for(plain, slot(300)).get(url)
        b = client_for(with_oi, slot(300)).get(url)
        assert a.status_code == b.status_code == 200, url
        assert a.text == b.text, url


def test_a_handler_does_not_write(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest=[slot(0)])
    before = sqlite3.connect(path).execute("SELECT COUNT(*) FROM open_interest").fetchone()

    client_for(path).get(f"/v1/open-interest?{Q}")

    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM open_interest").fetchone() == before


def test_candles_and_open_interest_share_a_database_without_interfering(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0)], open_interest=[slot(0)])
    cq = f"venue=binance-usdm&symbol=BTC/USDT:USDT&timeframe=5m&start={slot(0)}"

    assert client_for(path).get(f"/v1/open-interest?{Q}").status_code == 200
    assert client_for(path).get(f"/v1/candles?{cq}").status_code == 200
