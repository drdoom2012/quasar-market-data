"""``GET /v1/indicators`` end to end (contract api-v1 section 6.3; design S18 sections 5.3 to 5.7)."""

import asyncio
import json
import logging
import math
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from quasar_market_data.api import app as app_module
from quasar_market_data.api import indicator_results
from quasar_market_data.api.app import create_app
from quasar_market_data.api.float_text import float_text
from quasar_market_data.api.indicator_engine import compute_function
from quasar_market_data.api.indicator_reader import IndicatorInputs
from quasar_market_data.api.rule import DEFAULT_RULE
from quasar_market_data.domain.indicator_series import SegmentPrices
from quasar_market_data.domain.indicators import IndicatorSpec, parse_indicators
from quasar_market_data.storage.connection import open_reader, open_writer
from quasar_market_data.storage.migrate import migrate
from tests.api.seed import AT, candle_values, insert_candle, insert_conflict, insert_unavailable
from tests.indicator_data import generated_closes

TF = 300_000
S = 1792643400000
SERIES = "venue=binance-usdm&symbol=BTC/USDT:USDT&timeframe=5m"
DELAY = DEFAULT_RULE.publication_delay_ms
LAG = DEFAULT_RULE.lag_threshold_ms
MONTH_MS = 30 * 24 * 3600 * 1000

_FIRST_SLOT = -5000  # slot number of the first generated close
_CLOSES = generated_closes(10_000, seed=18)


def slot(n: int) -> int:
    return S + n * TF


def expected(n: int) -> int:
    """The clock at which slot ``n`` has just become the newest expected slot (``expected_at`` of slot ``n``)."""
    return slot(n) + TF + DELAY


def close_at(n: int) -> str:
    """The close of slot ``n``; the same whichever other slots are stored."""
    return _CLOSES[n - _FIRST_SLOT]


def seed_db(
    path: Path,
    present: Iterable[int],
    *,
    conflicts: Iterable[int] = (),
    unavailable: Iterable[int] = (),
    high_of: Callable[[int], str] | None = None,
    low_of: Callable[[int], str] | None = None,
) -> Path:
    conn = open_writer(path)
    try:
        migrate(conn, applied_at_ms=AT)
        conn.execute("BEGIN")
        for n in present:
            insert_candle(
                conn,
                slot(n),
                close=close_at(n),
                high=None if high_of is None else high_of(n),
                low=None if low_of is None else low_of(n),
            )
        for n in conflicts:
            insert_conflict(conn, slot(n), 0)
        for n in unavailable:
            insert_unavailable(conn, slot(n))
        conn.execute("COMMIT")
    finally:
        conn.close()
    return path


def url(start: int, end: int | None = None, indicators: str = "rsi:period=14", series: str = SERIES) -> str:
    text = f"/v1/indicators?{series}&start={start}&indicators={indicators}"
    return text if end is None else f"{text}&end={end}"


def client_for(path: Path, **kwargs: Any) -> TestClient:
    return TestClient(create_app(path, **kwargs), raise_server_exceptions=False)


def get_ok(client: TestClient, target: str) -> dict[str, Any]:
    response = client.get(target)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def only(body: dict[str, Any]) -> dict[str, Any]:
    (indicator,) = body["indicators"]
    result: dict[str, Any] = indicator
    return result


def assert_error(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status
    body: dict[str, Any] = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    error: dict[str, Any] = body["error"]
    return error


def without(values: Iterable[int], *missing: int) -> list[int]:
    return [n for n in values if n not in missing]


# --- shape -----------------------------------------------------------------------------------------------------------


def test_response_shape_and_key_order(tmp_path: Path) -> None:
    db = seed_db(tmp_path / "m.db", range(-200, 10))
    client = client_for(db, now_ms=lambda: expected(9))

    response = client.get(url(slot(0), slot(10), "rsi:period=014;rsi:period=2"))

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    body = response.json()
    assert list(body) == [
        "venue", "symbol", "timeframe", "start", "end", "as_of_ms", "rule", "series", "indicators",
    ]  # fmt: skip
    assert body["venue"] == "binance-usdm"
    assert body["symbol"] == "BTC/USDT:USDT"
    assert body["timeframe"] == "5m"
    assert (body["start"], body["end"], body["as_of_ms"]) == (slot(0), slot(10), expected(9))
    assert body["rule"] == {"publication_delay_ms": DELAY, "lag_threshold_ms": LAG}
    assert body["series"] == {"oldest_open_time_ms": slot(-200), "newest_open_time_ms": slot(9)}
    first, second = body["indicators"]
    assert list(first) == [
        "id", "name", "params", "outputs", "warmup_slots", "anchor_open_time_ms", "latest", "points",
    ]  # fmt: skip
    assert (first["id"], first["name"], first["params"], first["outputs"]) == (
        "rsi:period=14",
        "rsi",
        {"period": 14},
        ["rsi"],
    )
    assert first["warmup_slots"] == 140
    assert second["id"] == "rsi:period=2"
    assert second["warmup_slots"] == 20
    for indicator in body["indicators"]:
        assert [p["open_time_ms"] for p in indicator["points"]] == [slot(n) for n in range(10)]
        for point in indicator["points"]:
            assert list(point) == ["open_time_ms", "values", "reason"]
            assert point["reason"] is None
            assert list(point["values"]) == ["rsi"]
            assert isinstance(point["values"]["rsi"], str)
        assert indicator["latest"] == indicator["points"][-1]


def test_stored_records_give_their_reasons(tmp_path: Path) -> None:
    db = seed_db(tmp_path / "m.db", without(range(-200, 40), 30, 20), conflicts=[10], unavailable=[20])
    body = get_ok(client_for(db, now_ms=lambda: expected(39)), url(slot(0), slot(40)))

    reasons = {p["open_time_ms"]: p["reason"] for p in only(body)["points"]}
    assert (reasons[slot(10)], reasons[slot(20)], reasons[slot(30)]) == ("conflicted", "unavailable", "missing")
    assert reasons[slot(11)] == "warmup"  # a new segment starts after the conflicted slot
    for point in only(body)["points"]:
        assert (point["values"] is None) == (point["reason"] is not None)


# --- latest (design 5.5, L1-L8) ----------------------------------------------------------------------------------------


def test_l1_default_end_ingest_current_latest_has_a_value(tmp_path: Path) -> None:
    db = seed_db(tmp_path / "m.db", range(-500, 101))
    body = get_ok(client_for(db, now_ms=lambda: expected(100)), url(slot(0)))

    indicator = only(body)
    assert body["end"] == slot(101)
    assert len(indicator["points"]) == 101
    assert indicator["anchor_open_time_ms"] == slot(-140)
    latest = indicator["latest"]
    assert latest == indicator["points"][-1]
    assert latest["open_time_ms"] == slot(100)
    assert latest["reason"] is None
    assert set(latest["values"]) == {"rsi"}


def test_l2_default_end_ingest_stopped_within_the_lag_is_due(tmp_path: Path) -> None:
    db = seed_db(tmp_path / "m.db", range(-500, 100))  # newest stored: slot 99
    body = get_ok(client_for(db, now_ms=lambda: expected(100)), url(slot(0)))

    indicator = only(body)
    assert indicator["latest"] == {"open_time_ms": slot(100), "values": None, "reason": "due"}
    assert indicator["points"][-1] == indicator["latest"]
    assert indicator["points"][-2]["reason"] is None
    assert indicator["points"][-2]["values"] is not None


def test_l3_default_end_ingest_stopped_beyond_the_threshold_is_stale(tmp_path: Path) -> None:
    db = seed_db(tmp_path / "m.db", range(-500, 96))  # newest stored: slot 95
    body = get_ok(client_for(db, now_ms=lambda: expected(100) + LAG + 1), url(slot(0)))

    indicator = only(body)
    assert indicator["latest"] == {"open_time_ms": slot(100), "values": None, "reason": "stale"}
    trailing = indicator["points"][96:]
    assert [p["open_time_ms"] for p in trailing] == [slot(n) for n in range(96, 101)]
    assert all(p == {"open_time_ms": p["open_time_ms"], "values": None, "reason": "stale"} for p in trailing)
    assert indicator["points"][95]["values"] is not None  # the last stored value stays where it is


def test_l4_default_end_beyond_2016_slots_is_400_not_a_capped_window(tmp_path: Path) -> None:
    db = seed_db(tmp_path / "m.db", range(-500, 10))
    too_long = client_for(db, now_ms=lambda: expected(2016)).get(url(slot(0)))

    details = assert_error(too_long, 400, "invalid_parameter")["details"]
    assert details["maximum"] == 2016
    assert details["parameter"] == "start"

    exactly = get_ok(client_for(db, now_ms=lambda: expected(2015)), url(slot(0)))
    assert exactly["end"] == slot(2016)
    assert len(only(exactly)["points"]) == 2016


def test_l5_explicit_end_a_month_in_the_past_has_a_value_without_a_clock_reason(tmp_path: Path) -> None:
    db = seed_db(tmp_path / "m.db", range(-500, 100))  # the window's last slot is the newest stored candle
    body = get_ok(client_for(db, now_ms=lambda: slot(100) + MONTH_MS), url(slot(0), slot(100)))

    indicator = only(body)
    assert indicator["latest"]["open_time_ms"] == slot(99)
    assert indicator["latest"]["reason"] is None
    assert indicator["latest"]["values"] is not None
    assert all(p["reason"] is None for p in indicator["points"])


def test_l6_explicit_end_in_the_past_last_slot_missing(tmp_path: Path) -> None:
    db = seed_db(tmp_path / "m.db", without(range(-500, 200), 99))
    body = get_ok(client_for(db, now_ms=lambda: slot(200) + MONTH_MS), url(slot(0), slot(100)))

    indicator = only(body)
    assert indicator["latest"] == {"open_time_ms": slot(99), "values": None, "reason": "missing"}
    assert indicator["points"][-2]["values"] is not None


def test_l7_explicit_end_in_the_future_is_not_yet_expected(tmp_path: Path) -> None:
    db = seed_db(tmp_path / "m.db", range(-500, 51))
    body = get_ok(client_for(db, now_ms=lambda: expected(50)), url(slot(0), slot(60)))

    indicator = only(body)
    assert indicator["latest"] == {"open_time_ms": slot(59), "values": None, "reason": "not_yet_expected"}
    assert all(p["reason"] == "not_yet_expected" for p in indicator["points"][51:])
    assert indicator["points"][50]["values"] is not None


@pytest.mark.parametrize(
    ("newest_stored", "newest_expected"),
    [
        pytest.param(-11, -20, id="newest_expected_far_before_start"),
        # newest expected slot + one timeframe == start exactly: the fallback must still give one slot at start
        pytest.param(-1, -1, id="newest_expected_plus_one_timeframe_is_start"),
    ],
)
def test_l8_default_end_not_after_start_is_one_point_at_start(
    tmp_path: Path, newest_stored: int, newest_expected: int
) -> None:
    db = seed_db(tmp_path / "m.db", range(-500, newest_stored + 1))
    body = get_ok(client_for(db, now_ms=lambda: expected(newest_expected)), url(slot(0)))

    indicator = only(body)
    assert (body["start"], body["end"]) == (slot(0), slot(1))
    assert indicator["points"] == [{"open_time_ms": slot(0), "values": None, "reason": "not_yet_expected"}]
    assert indicator["latest"] == indicator["points"][0]
    assert indicator["anchor_open_time_ms"] is None


# --- independence, reproducibility and tolerance -----------------------------------------------------------------------


def test_a10_indicators_do_not_influence_each_other(tmp_path: Path) -> None:
    db = seed_db(tmp_path / "m.db", range(-2000, 300))
    client = client_for(db, now_ms=lambda: expected(299))

    together = get_ok(client, url(slot(0), slot(300), "rsi:period=14;rsi:period=100"))["indicators"]
    rsi14 = only(get_ok(client, url(slot(0), slot(300), "rsi:period=14")))
    rsi100 = only(get_ok(client, url(slot(0), slot(300), "rsi:period=100")))

    for combined, single in ((together[0], rsi14), (together[1], rsi100)):
        assert json.dumps(combined["points"]) == json.dumps(single["points"])
        assert combined["anchor_open_time_ms"] == single["anchor_open_time_ms"]
        assert combined["latest"] == single["latest"]
        assert all(p["values"] is not None for p in single["points"])
    assert rsi14["anchor_open_time_ms"] == slot(-140)
    assert rsi100["anchor_open_time_ms"] == slot(-1000)


def test_r1_same_request_same_bytes(tmp_path: Path) -> None:
    db = seed_db(tmp_path / "m.db", without(range(-300, 50), 20), conflicts=[30])
    client = client_for(db, now_ms=lambda: expected(60) + LAG)
    target = url(slot(0), indicators="rsi:period=14;rsi:period=2")

    first = client.get(target)
    second = client.get(target)

    assert first.status_code == 200
    assert first.content == second.content
    reasons = {p["reason"] for p in first.json()["indicators"][0]["points"]}
    assert {"missing", "conflicted", "warmup", "due", "stale", None} <= reasons


def test_c2_moving_start_changes_values_only_within_tolerance(tmp_path: Path) -> None:
    db = seed_db(tmp_path / "m.db", range(-3000, 600))
    client = client_for(db, now_ms=lambda: expected(599))
    indicators = "rsi:period=2;rsi:period=14;rsi:period=100"
    at_start = url(slot(0), slot(500), indicators)
    one_later = url(slot(1), slot(500), indicators)

    a, b = client.get(at_start), client.get(one_later)
    assert client.get(at_start).content == a.content
    assert client.get(one_later).content == b.content

    largest = 0.0
    for left, right in zip(a.json()["indicators"], b.json()["indicators"], strict=True):
        overlap = left["points"][1:]
        assert [p["open_time_ms"] for p in overlap] == [p["open_time_ms"] for p in right["points"]]
        for p, q in zip(overlap, right["points"], strict=True):
            difference = abs(float(p["values"]["rsi"]) - float(q["values"]["rsi"]))
            largest = max(largest, difference)
    assert largest < 1e-2


def test_values_equal_the_engine_on_the_segment(tmp_path: Path) -> None:
    db = seed_db(tmp_path / "m.db", without(range(-200, 301), 50))
    body = get_ok(client_for(db, now_ms=lambda: expected(300)), url(slot(0), slot(300)))
    points = {p["open_time_ms"]: p for p in only(body)["points"]}
    rsi = compute_function(parse_indicators("rsi:period=14")[0])

    def engine(first: int, last: int) -> Sequence[float]:
        closes = tuple(float(close_at(n)) for n in range(first, last + 1))
        return rsi(SegmentPrices(closes, closes, closes))[0]

    before_gap = engine(-140, 49)  # the first segment, cut at the read range's start
    for n in range(50):
        assert points[slot(n)]["values"] == {"rsi": float_text(before_gap[n + 140])}
    assert points[slot(50)]["reason"] == "missing"
    after_gap = engine(51, 299)
    for n in range(51, 191):
        assert points[slot(n)]["reason"] == "warmup"
    for n in range(191, 300):
        assert points[slot(n)]["values"] == {"rsi": float_text(after_gap[n - 51])}
    assert only(body)["anchor_open_time_ms"] == slot(-140)


# --- review focus ------------------------------------------------------------------------------------------------------


def test_clock_behind_stored_data_ends_the_window_before_the_newest_candle(tmp_path: Path) -> None:
    db = seed_db(tmp_path / "m.db", range(-200, 301))
    body = get_ok(client_for(db, now_ms=lambda: expected(200)), url(slot(0)))

    indicator = only(body)
    assert body["end"] == slot(201)
    assert body["series"]["newest_open_time_ms"] == slot(300)
    assert len(indicator["points"]) == 201
    assert all(p["values"] is not None for p in indicator["points"])
    assert indicator["latest"]["open_time_ms"] == slot(200)


def test_window_before_the_oldest_candle_is_all_missing(tmp_path: Path) -> None:
    db = seed_db(tmp_path / "m.db", range(1000, 1010))
    body = get_ok(client_for(db, now_ms=lambda: expected(1009)), url(slot(0), slot(100)))

    indicator = only(body)
    assert len(indicator["points"]) == 100
    assert all(p["values"] is None and p["reason"] == "missing" for p in indicator["points"])
    assert indicator["anchor_open_time_ms"] is None
    assert indicator["latest"]["reason"] == "missing"


def test_largest_request(tmp_path: Path) -> None:
    db = seed_db(tmp_path / "m.db", range(-1100, 2016))
    statements: list[str] = []

    def traced(path: Path) -> sqlite3.Connection:
        conn = open_reader(path)
        conn.set_trace_callback(statements.append)
        return conn

    indicators = ";".join(f"rsi:period={p}" for p in range(91, 101))
    client = client_for(db, now_ms=lambda: expected(2015), open_connection=traced)
    body = get_ok(client, url(slot(0), indicators=indicators))

    assert body["end"] == slot(2016)
    assert len(body["indicators"]) == 10
    for indicator in body["indicators"]:
        assert len(indicator["points"]) == 2016
        assert all(p["values"] is not None for p in indicator["points"])
    candle_reads = [s for s in statements if s.startswith("SELECT open_time_ms, high, low, close FROM candles")]
    assert len(candle_reads) == 1
    assert f"open_time_ms >= {slot(-1000)} AND open_time_ms < {slot(2016)}" in candle_reads[0]


# --- errors ------------------------------------------------------------------------------------------------------------


def test_400_before_a_broken_database(tmp_path: Path) -> None:
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database" * 100)
    client = client_for(broken, now_ms=lambda: expected(0))

    response = client.get(url(slot(0), slot(1), "rsi:period=1"))

    assert assert_error(response, 400, "invalid_parameter")["details"]["parameter"] == "indicators"
    assert client.get(url(slot(0), slot(1))).status_code == 503  # the file really is broken


def test_a_clock_out_of_range_is_500_before_the_database(tmp_path: Path) -> None:
    response = client_for(tmp_path / "missing.db", now_ms=lambda: 0).get(url(slot(0), slot(1)))

    assert_error(response, 500, "internal_error")


def test_the_window_of_a_defaulted_end_is_checked_before_the_database(tmp_path: Path) -> None:
    response = client_for(tmp_path / "missing.db", now_ms=lambda: expected(2016)).get(url(slot(0)))

    assert assert_error(response, 400, "invalid_parameter")["details"]["maximum"] == 2016


def test_a_missing_database_is_503(tmp_path: Path) -> None:
    response = client_for(tmp_path / "missing.db", now_ms=lambda: expected(0)).get(url(slot(0), slot(1)))

    assert assert_error(response, 503, "not_ready")["details"] == {"reason": "database_missing"}
    assert response.headers["retry-after"] == "5"


def test_an_unknown_series_is_404(tmp_path: Path) -> None:
    db = seed_db(tmp_path / "m.db", range(5))
    other = SERIES.replace("BTC/USDT:USDT", "ETH/USDT:USDT")

    response = client_for(db, now_ms=lambda: expected(4)).get(url(slot(0), slot(1), series=other))

    assert assert_error(response, 404, "unknown_series")["details"] == {
        "venue": "binance-usdm",
        "symbol": "ETH/USDT:USDT",
        "timeframe": "5m",
    }


def test_other_methods_and_paths(tmp_path: Path) -> None:
    client = client_for(seed_db(tmp_path / "m.db", range(5)), now_ms=lambda: expected(4))

    head = client.head(url(slot(0), slot(1)))
    assert head.status_code == 405
    assert head.headers["allow"] == "GET"
    assert_error(client.post("/v1/indicators"), 405, "method_not_allowed")
    assert_error(client.get(url(slot(0), slot(1)).replace("/v1/indicators?", "/v1/indicators/?")), 404, "not_found")


def test_no_store_header(tmp_path: Path) -> None:
    db = seed_db(tmp_path / "m.db", range(5))
    client = client_for(db, now_ms=lambda: expected(4))
    responses = {
        200: client.get(url(slot(0), slot(1))),
        400: client.get(url(slot(0), slot(1), "rsi:period=1")),
        404: client.get(url(slot(0), slot(1), series=SERIES.replace("BTC", "ETH"))),
        405: client.post("/v1/indicators"),
        500: client_for(db, now_ms=lambda: 0).get(url(slot(0), slot(1))),
        503: client_for(tmp_path / "missing.db", now_ms=lambda: expected(4)).get(url(slot(0), slot(1))),
    }
    for status, response in responses.items():
        assert response.status_code == status
        assert response.headers["cache-control"] == "no-store", status


def test_a_non_finite_value_is_a_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def infinite(spec: IndicatorSpec) -> Any:
        return lambda segment: ([math.inf] * len(segment.close),)

    db = seed_db(tmp_path / "m.db", range(-200, 10))
    client = client_for(db, now_ms=lambda: expected(9))
    target = url(slot(0), slot(10))
    assert client.get(target).status_code == 200  # the same request without the patch

    monkeypatch.setattr(indicator_results, "compute_function", infinite)
    with caplog.at_level(logging.ERROR, logger="quasar_market_data.api.app"):
        response = client.get(target)

    assert_error(response, 500, "internal_error")
    (record,) = [r for r in caplog.records if r.name == "quasar_market_data.api.app"]
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], ValueError)
    assert "non-finite" in str(record.exc_info[1])


def test_inputs_starting_after_a_read_range_are_refused() -> None:
    (spec,) = parse_indicators("rsi:period=14")
    inputs = IndicatorInputs(
        venue="binance-usdm",
        symbol="BTC/USDT:USDT",
        timeframe="5m",
        start=slot(0),
        end=slot(10),
        read_start=slot(-100),  # later than slot(-140), the read start of rsi:period=14
        oldest_open_time_ms=slot(-100),
        newest_open_time_ms=slot(9),
        prices={slot(n): (close_at(n), close_at(n), close_at(n)) for n in range(-100, 10)},
        conflicts=frozenset(),
        unavailable=frozenset(),
    )

    with pytest.raises(ValueError, match="after the read range"):
        indicator_results.indicator_results(inputs, [spec], as_of_ms=expected(9), rule=DEFAULT_RULE)


def test_flat_range_after_movement_reads_zero_only_when_the_whole_range_is_flat(tmp_path: Path) -> None:
    """Characterization of the observed TA-Lib behaviour, follow-up F-20; update together with the user's decision.

    Closes move up to slot -1 and are constant from slot 0. RSI(2) at slot 50 is "0" only when the range from the
    anchor to that slot is flat (average gain and loss both exactly zero); otherwise it keeps the last ratio.
    """
    conn = open_writer(tmp_path / "m.db")
    try:
        migrate(conn, applied_at_ms=AT)
        conn.execute("BEGIN")
        for n in range(-300, 60):
            insert_candle(conn, slot(n), close=close_at(min(n, -1)))
        conn.execute("COMMIT")
    finally:
        conn.close()
    client = client_for(tmp_path / "m.db", now_ms=lambda: expected(59))

    def rsi_at_50(start: int) -> str:
        (indicator,) = get_ok(client, url(slot(start), slot(51), "rsi:period=2"))["indicators"]
        point = indicator["points"][-1]
        assert point["open_time_ms"] == slot(50)
        value: str = point["values"]["rsi"]
        return value

    values = [rsi_at_50(start) for start in (0, 10, 21, 40)]

    assert values == ["63.953414949372856", "63.955521365130394", "0", "0"]
    assert abs(float(values[0]) - float(values[1])) < 0.01


def test_rsi_ignores_high_and_low(tmp_path: Path) -> None:
    def raised(n: int) -> str:
        return str(Decimal(candle_values(slot(n))[1]) + 1000)

    def lowered(n: int) -> str:
        return str(Decimal(candle_values(slot(n))[2]) - 1)

    plain = seed_db(tmp_path / "plain.db", range(-200, 300))
    skewed = seed_db(tmp_path / "skewed.db", range(-200, 300), high_of=raised, low_of=lowered)
    target = url(slot(0), slot(300), "rsi:period=14")

    a = client_for(plain, now_ms=lambda: expected(299)).get(target)
    b = client_for(skewed, now_ms=lambda: expected(299)).get(target)

    assert a.status_code == b.status_code == 200
    assert a.content == b.content


class _Spy:
    def __init__(self) -> None:
        self.segments: list[SegmentPrices] = []

    def wrap(self, spec: IndicatorSpec) -> Any:
        real = compute_function(spec)

        def compute(segment: SegmentPrices) -> Sequence[Sequence[float]]:
            self.segments.append(segment)
            return real(segment)

        return compute


def _spied_segments(db: Path, monkeypatch: pytest.MonkeyPatch, *, as_of: int) -> list[SegmentPrices]:
    spy = _Spy()
    monkeypatch.setattr(indicator_results, "compute_function", spy.wrap)
    get_ok(client_for(db, now_ms=lambda: expected(as_of)), url(slot(0), slot(300), "rsi:period=14"))
    return spy.segments


def test_a_conflicted_slot_never_feeds_high_or_low(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    extreme_text = "900000.5"  # more than 10x the ordinary stored highs (about 80100)

    def stored_with_extreme_high(name: str, conflicts: Iterable[int]) -> Path:
        return seed_db(
            tmp_path / name,
            range(-200, 300),
            conflicts=conflicts,
            high_of=lambda n: extreme_text if n == 200 else candle_values(slot(n))[1],
        )

    # control: the spy does see the extreme high when the slot is a plain present candle
    control = _spied_segments(stored_with_extreme_high("control.db", ()), monkeypatch, as_of=299)
    assert [len(s.close) for s in control] == [440]
    assert float(extreme_text) in control[0].high

    segments = _spied_segments(stored_with_extreme_high("conflicted.db", [200]), monkeypatch, as_of=299)

    assert all(float(extreme_text) not in s.high for s in segments)
    assert all(max(s.high) < float(extreme_text) for s in segments)
    # none spans the conflicted slot: the read range -140..299 splits into -140..199 and 201..299
    assert [(len(s.high), len(s.low), len(s.close)) for s in segments] == [(340, 340, 340), (99, 99, 99)]
    assert segments[0].close == tuple(float(close_at(n)) for n in range(-140, 200))
    assert segments[1].close == tuple(float(close_at(n)) for n in range(201, 300))
    # RSI reads the closes of the surrounding present slots only
    body_points = get_ok(client_for(tmp_path / "conflicted.db", now_ms=lambda: expected(299)), url(slot(0), slot(300)))
    assert {p["open_time_ms"]: p["reason"] for p in only(body_points)["points"]}[slot(200)] == "conflicted"


# --- S19: five more indicators (design S19 sections 5.4, 5.6, 6) -------------------------------------------------------

MIXED = (
    "rsi:period=100;ema:period=200;macd:fast=100,slow=200,signal=50;atr:period=100;adx:period=100;"
    "bbands:period=200,stddev=5;ema:period=2;macd:fast=12,slow=26,signal=9;adx:period=14;bbands:period=20,stddev=2"
)


def test_mixed_request_each_indicator_equals_itself_alone(tmp_path: Path) -> None:
    """Review focus 4: all six names at the largest warm-ups, ten items, read range 2016 + 1500 slots."""
    db = seed_db(tmp_path / "m.db", range(-1600, 2016))
    statements: list[str] = []

    def traced(path: Path) -> sqlite3.Connection:
        conn = open_reader(path)
        conn.set_trace_callback(statements.append)
        return conn

    together_client = client_for(db, now_ms=lambda: expected(2015), open_connection=traced)
    together = get_ok(together_client, url(slot(0), slot(2016), MIXED))["indicators"]

    candle_reads = [s for s in statements if s.startswith("SELECT open_time_ms, high, low, close FROM candles")]
    assert len(candle_reads) == 1
    assert f"open_time_ms >= {slot(-1500)} AND open_time_ms < {slot(2016)}" in candle_reads[0]
    assert (2016 + 1500) == (slot(2016) - slot(-1500)) // TF

    items = MIXED.split(";")
    assert len(together) == len(items) == 10
    alone_client = client_for(db, now_ms=lambda: expected(2015))
    for combined, item in zip(together, items, strict=True):
        alone = only(get_ok(alone_client, url(slot(0), slot(2016), item)))
        assert combined["id"] == item
        assert len(combined["points"]) == 2016
        assert json.dumps(combined) == json.dumps(alone), item
        assert all(p["values"] is not None for p in combined["points"]), item


def test_three_output_indicator_shape_and_latest(tmp_path: Path) -> None:
    indicators = "macd:fast=12,slow=26,signal=9;adx:period=14;bbands:period=20,stddev=2"
    db = seed_db(tmp_path / "m.db", range(-500, 101))
    body = get_ok(client_for(db, now_ms=lambda: expected(100)), url(slot(0), indicators=indicators))
    macd, adx, bbands = body["indicators"]

    assert macd["outputs"] == ["macd", "signal", "histogram"]
    assert adx["outputs"] == ["adx", "plus_di", "minus_di"]
    assert bbands["outputs"] == ["upper", "middle", "lower"]
    for indicator in (macd, adx, bbands):
        assert indicator["latest"] == indicator["points"][-1]
        for point in indicator["points"]:
            assert list(point["values"]) == indicator["outputs"]
            assert all(isinstance(text, str) for text in point["values"].values())

    stopped = seed_db(tmp_path / "due.db", range(-500, 100))  # L2: newest stored slot 99
    for indicator in get_ok(client_for(stopped, now_ms=lambda: expected(100)), url(slot(0), indicators=indicators))[
        "indicators"
    ]:
        assert indicator["latest"] == {"open_time_ms": slot(100), "values": None, "reason": "due"}
    stale = seed_db(tmp_path / "stale.db", range(-500, 96))  # L3: newest stored slot 95
    for indicator in get_ok(
        client_for(stale, now_ms=lambda: expected(100) + LAG + 1), url(slot(0), indicators=indicators)
    )["indicators"]:
        assert indicator["latest"] == {"open_time_ms": slot(100), "values": None, "reason": "stale"}


_S19_ITEMS = [
    ("ema:period=14", 84),
    ("macd:fast=12,slow=26,signal=9", 210),
    ("atr:period=14", 140),
    ("adx:period=14", 182),
    ("bbands:period=20,stddev=2", 19),
]


def _reasons(indicator: dict[str, Any]) -> dict[int, str | None]:
    return {p["open_time_ms"]: p["reason"] for p in indicator["points"]}


def _first_value(indicator: dict[str, Any]) -> int:
    first: int = next(p["open_time_ms"] for p in indicator["points"] if p["values"] is not None)
    return first


@pytest.mark.parametrize(("item", "w"), _S19_ITEMS, ids=[i for i, _ in _S19_ITEMS])
def test_s19_segment_scenarios(tmp_path: Path, item: str, w: int) -> None:
    """A3, A4, A6 and A8 of the S18 design with the warm-up of the indicator under test."""
    end = 200 + w + 60
    now = lambda: expected(end - 1)

    def run(name: str, present: Iterable[int]) -> dict[str, Any]:
        return only(get_ok(client_for(seed_db(tmp_path / name, present), now_ms=now), url(slot(0), slot(end), item)))

    # A3: a missing slot inside the warm-up range moves the anchor past it
    h = w // 2
    a3 = run("a3.db", without(range(-500, end), -h))
    assert a3["anchor_open_time_ms"] == slot(-h + 1)
    first = w - h + 1  # the segment starts at -h + 1; its first value is W slots later
    assert [n for n in range(end) if _reasons(a3)[slot(n)] == "warmup"] == list(range(first))
    assert _first_value(a3) == slot(first)
    # A8: exactly W present slots before the first value, W - 1 before the last warm-up point
    assert _reasons(a3)[slot(first - 1)] == "warmup"
    assert _reasons(a3)[slot(first)] is None

    # A4: a missing slot at s + 200 restarts the warm-up; the values equal those of a database cut at s + 201
    a4 = run("a4.db", without(range(-500, end), 200))
    assert _reasons(a4)[slot(200)] == "missing"
    assert all(_reasons(a4)[slot(n)] == "warmup" for n in range(201, 201 + w))
    assert next(p for p in a4["points"] if p["open_time_ms"] > slot(200) and p["values"] is not None)[
        "open_time_ms"
    ] == slot(201 + w)
    cut = only(
        get_ok(client_for(seed_db(tmp_path / "a4cut.db", range(201, end)), now_ms=now), url(slot(201), slot(end), item))
    )
    after = [p for p in a4["points"] if p["open_time_ms"] >= slot(201)]
    assert json.dumps(after) == json.dumps(cut["points"])

    # A6: start itself missing; the segment starts at s + 1
    a6 = run("a6.db", without(range(-500, end), 0))
    assert _reasons(a6)[slot(0)] == "missing"
    assert all(_reasons(a6)[slot(n)] == "warmup" for n in range(1, 1 + w))
    assert _first_value(a6) == slot(1 + w)
    assert a6["anchor_open_time_ms"] == slot(1)


def test_a_non_finite_value_in_a_later_output_is_a_500(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review focus 5: finite ``macd`` and ``signal`` with an infinite ``histogram`` never give a partial ``values``."""

    def later_output(spec: IndicatorSpec) -> Any:
        def compute(segment: SegmentPrices) -> Sequence[Sequence[float]]:
            count = len(segment.close)
            return ([1.0] * count, [2.0] * count, [math.inf] * count)

        return compute

    db = seed_db(tmp_path / "m.db", range(-300, 10))
    client = client_for(db, now_ms=lambda: expected(9))
    target = url(slot(0), slot(10), "macd:fast=12,slow=26,signal=9")
    assert client.get(target).status_code == 200  # the same request without the patch

    monkeypatch.setattr(indicator_results, "compute_function", later_output)
    response = client.get(target)

    assert_error(response, 500, "internal_error")
    assert "values" not in response.text


def test_the_body_is_built_off_the_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """J7: the body is built (and encoded) in the thread pool, so a large response cannot stall ``/health``."""
    on_loop: list[bool] = []
    real = app_module._indicators_body

    def wrapper(*args: Any, **kwargs: Any) -> dict[str, object]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_loop.append(False)
        else:
            on_loop.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(app_module, "_indicators_body", wrapper)
    db = seed_db(tmp_path / "m.db", range(-200, 10))

    get_ok(client_for(db, now_ms=lambda: expected(9)), url(slot(0), slot(10)))

    assert on_loop == [False]
