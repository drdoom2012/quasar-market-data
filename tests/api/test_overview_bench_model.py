"""The pure parts of the overview refresh benchmark (OI-6a plan, task 1): call order, deadline, metrics, writer."""

import json
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from quasar_market_data.storage.connection import open_writer
from quasar_market_data.storage.migrate import migrate
from tests.api.bench_overview_refresh import (
    CallRecord,
    RefreshResult,
    evaluate,
    evaluate_sustained,
    run_refresh,
    summarize,
    writer_tick,
)
from tools.symbol_overview.fetch import RawResponse, TransportError
from tools.symbol_overview.window import window_for

TF = 300_000
T0 = 1_792_677_030_000
NEWEST = 1_792_676_700_000  # aligned to 5 minutes
JSON_TYPE = "application/json; charset=utf-8"
MIB = 1024 * 1024
COVERAGE = "/v1/coverage"
OI_COVERAGE = "/v1/open-interest/coverage"


def ok(document: object) -> RawResponse:
    return RawResponse(200, JSON_TYPE, json.dumps(document).encode())


class FakeApi:
    """A fake ``Fetch``: the two head answers, ``{}`` for every coverage call, and per-(symbol, route) overrides."""

    def __init__(
        self,
        instruments: Sequence[tuple[str, int | None]],
        *,
        overrides: dict[tuple[str, str], RawResponse | Exception] | None = None,
        on_call: Callable[[str], None] | None = None,
    ) -> None:
        self.instruments = instruments
        self.overrides = overrides or {}
        self.on_call = on_call
        self.paths: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, path: str) -> RawResponse:
        with self._lock:
            self.paths.append(path)
        if self.on_call is not None:
            self.on_call(path)
        route = path.partition("?")[0]
        if route == "/health/ingest":
            return ok({"as_of_ms": T0, "rule": {"publication_delay_ms": 15_000, "lag_threshold_ms": 60_000}})
        if route == "/v1/instruments":
            return ok(
                {
                    "instruments": [
                        {"venue": "binance-usdm", "symbol": symbol, "timeframe": "5m", "newest_open_time_ms": newest}
                        for symbol, newest in self.instruments
                    ]
                }
            )
        for (symbol, overridden_route), outcome in self.overrides.items():
            if overridden_route == route and f"symbol={symbol}&" in path:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        return ok({})


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


SIX = [(f"S{i}/USDT:USDT", NEWEST) for i in range(5)] + [("S5/USDT:USDT", None)]


def record(
    kind: str, duration_s: float, *, status: int = 200, body_bytes: int = 10, decode_s: float = 0.0
) -> CallRecord:
    return CallRecord(kind, "/x", 0.0, duration_s, status, body_bytes, decode_s)


def result(duration_s: float, *calls: CallRecord, candle_not_asked: int = 0, oi_not_asked: int = 0) -> RefreshResult:
    failed = sum(1 for call in calls if call.status != 200)
    return RefreshResult(duration_s, tuple(calls), candle_not_asked, oi_not_asked, failed)


def test_the_calls_run_in_the_documented_order() -> None:
    api = FakeApi(SIX)

    refreshed = run_refresh(api, oi_mode="real", monotonic=Clock())

    kinds = [call.kind for call in refreshed.calls]
    assert kinds == ["health", "instruments"] + ["candle"] * 5 + ["oi"] * 6
    last_candle = max(i for i, path in enumerate(api.paths) if path.startswith(COVERAGE))
    first_oi = min(i for i, path in enumerate(api.paths) if path.startswith(OI_COVERAGE))
    assert last_candle < first_oi  # the candle phase is over before any open interest call starts
    assert (refreshed.candle_not_asked, refreshed.oi_not_asked, refreshed.failed) == (0, 0, 0)


def test_the_real_mode_asks_the_shifted_window_and_the_proxy_asks_the_candle_window() -> None:
    window = window_for(T0)

    real = run_refresh(FakeApi(SIX), oi_mode="real", monotonic=Clock())
    proxy = run_refresh(FakeApi(SIX), oi_mode="proxy", monotonic=Clock())

    real_oi = [call.path for call in real.calls if call.kind == "oi"]
    assert len(real_oi) == 6
    for path in real_oi:
        assert path.startswith(f"{OI_COVERAGE}?")
        assert "period=5m" in path
        assert path.endswith(f"start={window.start + TF}&end={window.end + TF}")
    proxy_oi = [call.path for call in proxy.calls if call.kind == "oi"]
    for path in proxy_oi:
        assert path.startswith(f"{COVERAGE}?")
        assert path.endswith(f"start={window.start}&end={window.end}")


def test_no_call_starts_after_the_deadline_in_either_phase() -> None:
    clock = Clock()
    candle_calls: list[str] = []

    def passes_the_deadline_after_the_third_candle_call(path: str) -> None:
        if path.startswith(COVERAGE):
            candle_calls.append(path)
            if len(candle_calls) == 3:
                clock.now = 100.0

    api = FakeApi(SIX, on_call=passes_the_deadline_after_the_third_candle_call)

    refreshed = run_refresh(api, oi_mode="real", workers=1, deadline_s=60.0, monotonic=clock)

    assert len(candle_calls) == 3
    assert not any(path.startswith(OI_COVERAGE) for path in api.paths)
    assert (refreshed.candle_not_asked, refreshed.oi_not_asked) == (2, 6)
    assert [call.kind for call in refreshed.calls] == ["health", "instruments", "candle", "candle", "candle"]


def test_a_deadline_between_the_phases_leaves_the_candle_phase_complete() -> None:
    clock = Clock()
    candle_calls: list[str] = []

    def passes_the_deadline_after_the_last_candle_call(path: str) -> None:
        if path.startswith(COVERAGE):
            candle_calls.append(path)
            if len(candle_calls) == 5:
                clock.now = 100.0

    api = FakeApi(SIX, on_call=passes_the_deadline_after_the_last_candle_call)

    refreshed = run_refresh(api, oi_mode="real", workers=1, deadline_s=60.0, monotonic=clock)

    assert (refreshed.candle_not_asked, refreshed.oi_not_asked) == (0, 6)
    assert sum(1 for call in refreshed.calls if call.kind == "candle") == 5


def test_a_failed_call_is_counted_and_not_averaged_as_a_success() -> None:
    instruments = [(f"S{i}/USDT:USDT", NEWEST) for i in range(4)]
    api = FakeApi(
        instruments,
        overrides={
            ("S1/USDT:USDT", "/v1/coverage"): RawResponse(503, JSON_TYPE, b'{"error":{"code":"not_ready"}}'),
            ("S2/USDT:USDT", "/v1/coverage"): TransportError("connection"),
        },
    )

    refreshed = run_refresh(api, oi_mode="real", monotonic=Clock())

    assert refreshed.failed == 2
    assert evaluate([refreshed], series=4, l_tunnel_ms=None)["AC2"]["pass"] is False
    assert summarize(refreshed.calls)["candle"]["n"] == 2  # only the two successful candle calls


def test_summarize_reports_percentiles_and_omits_absent_kinds() -> None:
    calls = [record("candle", i / 1000) for i in range(1, 101)]

    summary = summarize(calls)

    assert set(summary) == {"candle"}
    assert summary["candle"] == {"n": 100, "p50": 50.0, "p95": 95.0, "p99": 99.0, "max": 100.0, "mean": 50.5}
    assert summarize([]) == {}
    assert summarize([record("oi", 0.1, status=503)]) == {}  # a kind with no successful call is absent


def test_ac1_is_inclusive_at_45_seconds_and_ac7_follows_the_formula() -> None:
    call = record("candle", 0.1)

    at_limit = evaluate([result(45.0, call)], series=600, l_tunnel_ms=None)
    over = evaluate([result(45.001, call)], series=600, l_tunnel_ms=None)
    with_tunnel = evaluate([result(1.0, call)], series=600, l_tunnel_ms=50.0)

    assert at_limit["AC1"]["pass"] is True
    assert over["AC1"]["pass"] is False
    assert at_limit["AC7"] == {"value": None, "pass": None}
    # (2 + ceil(600 / 4) * 2) * (100 ms + 50 ms) = 302 * 0.150 s
    assert with_tunnel["AC7"]["value"] == pytest.approx(45.3)
    assert with_tunnel["AC7"]["pass"] is False


def test_ac7_is_unknown_when_no_coverage_call_succeeded() -> None:
    only_health = result(1.0, record("health", 0.1))

    assert evaluate([only_health], series=300, l_tunnel_ms=50.0)["AC7"] == {"value": None, "pass": None}
    with pytest.raises(ValueError):
        evaluate([], series=300, l_tunnel_ms=None)


def test_ac6_reports_the_largest_body_and_decode_time() -> None:
    small = record("candle", 0.1, body_bytes=1_000_000, decode_s=0.01)
    large = record("oi", 0.1, body_bytes=1_000_001, decode_s=0.03)

    assert evaluate([result(1.0, small)], series=3, l_tunnel_ms=None)["AC6"]["pass"] is True
    both = evaluate([result(1.0, small, large)], series=3, l_tunnel_ms=None)["AC6"]
    assert both["pass"] is False
    assert both["value"] == {"max_body_bytes": 1_000_001, "max_decode_ms": pytest.approx(30.0)}


def test_the_window_comes_from_the_health_clock_only() -> None:
    first = run_refresh(FakeApi(SIX), oi_mode="real", monotonic=Clock())
    later_clock = Clock()
    later_clock.now = 5.0
    second = run_refresh(FakeApi(SIX), oi_mode="real", monotonic=later_clock)

    assert [call.path for call in first.calls] == [call.path for call in second.calls]


def test_the_writer_writes_at_most_100_rows_per_transaction(tmp_path: Path) -> None:
    conn = open_writer(tmp_path / "w.db")
    try:
        migrate(conn, applied_at_ms=T0)
        symbols = [f"S{i:03d}/USDT:USDT" for i in range(250)]
        statements: list[str] = []
        conn.set_trace_callback(statements.append)

        written = writer_tick(conn, symbols, NEWEST, NEWEST + TF)

        conn.set_trace_callback(None)
        assert written == 500
        blocks: list[int] = []
        inserted = 0
        for statement in statements:
            head = statement.lstrip().upper()
            if head.startswith("BEGIN"):
                inserted = 0
            elif head.startswith("INSERT"):
                inserted += 1
            elif head.startswith("COMMIT"):
                blocks.append(inserted)
        assert sum(blocks) == 500
        assert max(blocks) <= 100
        assert len(blocks) == 5
        for table in ("candles", "open_interest"):
            per_symbol = conn.execute(f"SELECT symbol, COUNT(*) FROM {table} GROUP BY symbol").fetchall()
            assert len(per_symbol) == 250
            assert {count for _, count in per_symbol} == {1}
    finally:
        conn.close()


def test_evaluate_sustained_thresholds() -> None:
    at_limit = evaluate_sustained([1000.0] * 100, 0, 432 * MIB)
    assert at_limit["AC4"]["pass"] is True
    assert at_limit["AC5"]["pass"] is True
    assert evaluate_sustained([1001.0] * 100, 0, 432 * MIB)["AC4"]["pass"] is False
    assert evaluate_sustained([1.0] * 100, 1, 432 * MIB)["AC4"]["pass"] is False
    assert evaluate_sustained([1.0] * 100, 0, 432 * MIB + 1)["AC5"]["pass"] is False
    assert evaluate_sustained([], 0, MIB)["AC4"]["pass"] is None  # nothing was probed: unknown, not passed
