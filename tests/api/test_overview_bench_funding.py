"""The funding phase of the overview refresh benchmark (FR-6): call order, deadline, phase times, cost, data."""

import json
import socket
from typing import Any

import pytest

from tests.api.bench_overview_refresh import (
    PHASES,
    CallRecord,
    RefreshResult,
    breakdown,
    build_database,
    evaluate,
    format_breakdown,
    funding_cost,
    funding_rows,
    main,
    run_refresh,
)
from tests.api.bench_server import BenchError
from tests.api.test_overview_bench_model import NEWEST, SIX, T0, Clock, FakeApi, ok, record
from tools.symbol_overview.fetch import RawResponse
from tools.symbol_overview.window import funding_window, window_for

FUNDING = "/v1/funding-rates"
HEALTH_RULE = {
    "publication_delay_ms": 30_000,
    "behind_after_ms": 7_230_000,
    "quiet_after_ms": 172_800_000,
    "backfill_days": 30,
}


class FundingFakeApi(FakeApi):
    """The fake of the model tests with a health body that reports funding (or a given value for the key)."""

    def __init__(self, *args: Any, funding_key: object = "enabled", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.funding_key = funding_key

    def __call__(self, path: str) -> RawResponse:
        if path.partition("?")[0] == "/health/ingest":
            with self._lock:
                self.paths.append(path)
            if self.on_call is not None:
                self.on_call(path)
            health: dict[str, Any] = {
                "as_of_ms": T0,
                "rule": {"publication_delay_ms": 15_000, "lag_threshold_ms": 60_000},
            }
            if self.funding_key == "enabled":
                health["funding_rate"] = {
                    "as_of_ms": T0,
                    "rule": HEALTH_RULE,
                    "scope": "stored_series",
                    "status": "current",
                    "series_count": 6,
                    "current": 6,
                    "behind": 0,
                    "catching_up": 0,
                    "conflicted_series": 0,
                    "quiet_series": 0,
                }
            elif self.funding_key != "absent":
                health["funding_rate"] = self.funding_key
            return ok(health)
        return super().__call__(path)


def result(duration_s: float, phases: dict[str, float], *calls: CallRecord, **kwargs: Any) -> RefreshResult:
    failed = sum(1 for call in calls if call.status != 200)
    return RefreshResult(duration_s, tuple(calls), 0, 0, failed, phases=phases, **kwargs)


# --- the call order ----------------------------------------------------------------------------------------------------


def test_the_funding_phase_follows_the_open_interest_phase() -> None:
    api = FundingFakeApi(SIX)

    refreshed = run_refresh(api, oi_mode="real", funding_mode="real", monotonic=Clock())

    kinds = [call.kind for call in refreshed.calls]
    assert kinds == ["health", "instruments"] + ["candle"] * 5 + ["oi"] * 6 + ["funding"] * 6
    last_oi = max(i for i, path in enumerate(api.paths) if path.startswith("/v1/open-interest/coverage"))
    first_funding = min(i for i, path in enumerate(api.paths) if path.startswith(FUNDING))
    assert last_oi < first_funding
    assert (refreshed.candle_not_asked, refreshed.oi_not_asked, refreshed.funding_not_asked, refreshed.failed) == (
        0,
        0,
        0,
        0,
    )


def test_the_funding_calls_are_the_tools_own_requests() -> None:
    window = window_for(T0)
    fwindow = funding_window(window, _rule(), T0)

    refreshed = run_refresh(FundingFakeApi(SIX), oi_mode="real", funding_mode="real", monotonic=Clock())

    paths = [call.path for call in refreshed.calls if call.kind == "funding"]
    assert len(paths) == 6
    for path in paths:
        assert path.startswith(f"{FUNDING}?venue=binance-usdm&symbol=")
        assert path.endswith(f"&start={fwindow.start}&end={fwindow.end}&limit=2")


def _rule() -> Any:
    from tools.symbol_overview.model import FundingRule

    return FundingRule(30_000, 7_230_000, 172_800_000, 365)


def test_the_off_mode_makes_no_funding_call_and_keeps_the_old_result() -> None:
    api = FundingFakeApi(SIX)

    refreshed = run_refresh(api, oi_mode="real", monotonic=Clock())

    assert not any(path.startswith(FUNDING) for path in api.paths)
    assert [call.kind for call in refreshed.calls] == ["health", "instruments"] + ["candle"] * 5 + ["oi"] * 6
    assert refreshed.funding_not_asked == 0 and refreshed.phases["funding"] == 0.0


@pytest.mark.parametrize("key", [None, "absent", 3], ids=["disabled", "absent", "malformed"])
def test_the_real_mode_needs_an_api_with_funding(key: object) -> None:
    with pytest.raises(BenchError):
        run_refresh(FundingFakeApi(SIX, funding_key=key), oi_mode="real", funding_mode="real", monotonic=Clock())


def test_no_funding_call_starts_after_the_deadline() -> None:
    clock = Clock()
    seen: list[str] = []

    def passes_the_deadline_after_the_second_funding_call(path: str) -> None:
        if path.startswith(FUNDING):
            seen.append(path)
            if len(seen) == 2:
                clock.now = 100.0

    api = FundingFakeApi(SIX, on_call=passes_the_deadline_after_the_second_funding_call)

    refreshed = run_refresh(api, oi_mode="real", funding_mode="real", workers=1, deadline_s=60.0, monotonic=clock)

    assert len(seen) == 2
    assert (refreshed.candle_not_asked, refreshed.oi_not_asked, refreshed.funding_not_asked) == (0, 0, 4)
    assert sum(1 for call in refreshed.calls if call.kind == "funding") == 2


def test_every_phase_has_a_time() -> None:
    clock = Clock()

    def one_second_per_call(path: str) -> None:
        clock.now += 1.0

    refreshed = run_refresh(
        FundingFakeApi(SIX, on_call=one_second_per_call),
        oi_mode="real",
        funding_mode="real",
        workers=1,
        monotonic=clock,
    )

    assert set(refreshed.phases) == set(PHASES)
    # head: 2 calls; candle: 5; oi: 6; funding: 6 (one clock tick per call, one worker)
    assert refreshed.phases == {"head": 2.0, "candle": 5.0, "oi": 6.0, "funding": 6.0}
    assert refreshed.duration_s == pytest.approx(19.0)


# --- the figures -----------------------------------------------------------------------------------------------------


def test_the_breakdown_gives_total_phases_shares_and_calls() -> None:
    health, instruments = record("health", 0.1), record("instruments", 0.1)
    one = result(
        10.0,
        {"head": 1.0, "candle": 5.0, "oi": 3.0, "funding": 1.0},
        health,
        instruments,
        record("candle", 0.1),
        record("candle", 0.1),
        record("oi", 0.1),
        record("funding", 0.1),
    )
    two = result(
        20.0,
        {"head": 1.0, "candle": 9.0, "oi": 5.0, "funding": 5.0},
        health,
        instruments,
        record("candle", 0.1),
        record("oi", 0.1),
        record("funding", 0.1),
        record("funding", 0.1),
    )

    data = breakdown([one, two])

    assert data["refreshes"] == 2
    assert data["total"] == {"min": 10.0, "p50": 10.0, "p95": 20.0, "max": 20.0, "mean": 15.0}
    funding = data["phases"]["funding"]
    assert (funding["min"], funding["max"], funding["mean"]) == (1.0, 5.0, 3.0)
    assert funding["share"] == pytest.approx(3.0 / 15.0) and funding["calls"] == 1.5
    assert data["phases"]["head"]["calls"] == 2.0
    assert data["phases"]["candle"]["calls"] == 1.5
    assert sum(p["share"] for p in data["phases"].values()) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        breakdown([])


def test_the_table_names_the_total_and_every_phase() -> None:
    one = result(10.0, {"head": 1.0, "candle": 5.0, "oi": 3.0, "funding": 1.0}, record("health", 0.1))

    table = format_breakdown("a refresh", breakdown([one]))

    lines = table.splitlines()
    assert lines[0] == "a refresh (1 refreshes)"
    assert [line.split()[0] for line in lines[2:7]] == ["TOTAL", "head", "candle", "oi", "funding"]
    assert "10.00" in lines[2] and "100%" in lines[2] and "50.0%" in lines[4]


def test_the_cost_of_funding_is_the_difference_of_two_measured_means() -> None:
    off = [result(10.0, {}), result(12.0, {})]
    on = [result(14.0, {}, record("funding", 0.2), record("funding", 0.4)), result(16.0, {}, record("funding", 0.3))]

    cost = funding_cost(off, on)

    assert cost["mean_total_without_s"] == 11.0 and cost["mean_total_with_s"] == 15.0
    assert cost["added_s"] == 4.0 and cost["added_share_of_with"] == pytest.approx(4.0 / 15.0)
    assert cost["mean_funding_call_ms"] == pytest.approx(300.0)


def test_ac7_uses_three_phases_with_funding_and_ac2_counts_the_symbols_not_asked() -> None:
    call, funding_call = record("candle", 0.1), record("funding", 0.3)
    with_funding = evaluate([result(1.0, {}, call, funding_call)], series=600, l_tunnel_ms=50.0, funding=True)
    without = evaluate([result(1.0, {}, call, funding_call)], series=600, l_tunnel_ms=50.0)

    # the latency average includes the funding calls: (100 + 300) / 2 = 200 ms; (2 + 150 * 3) * (200 + 50) ms
    assert with_funding["AC7"]["value"] == pytest.approx(452 * 0.250)
    # without funding the funding calls are not part of the average and the factor stays 2: 302 * 0.150
    assert without["AC7"]["value"] == pytest.approx(302 * 0.150)
    assert set(with_funding["AC2"]["value"]) == {"failed", "candle_not_asked", "oi_not_asked", "funding_not_asked"}
    assert set(without["AC2"]["value"]) == {"failed", "candle_not_asked", "oi_not_asked"}
    missed = RefreshResult(1.0, (call,), 0, 0, 0, funding_not_asked=2)
    assert evaluate([missed], series=3, l_tunnel_ms=None, funding=True)["AC2"]["pass"] is False
    assert evaluate([missed], series=3, l_tunnel_ms=None)["AC2"]["pass"] is True  # not part of a run without funding
    assert set(without) == {"AC1", "AC2", "AC3", "AC6", "AC7"}


# --- the generated data --------------------------------------------------------------------------------------------------


def test_the_generated_funding_history_has_the_documented_cases() -> None:
    now = 1_792_677_000_000
    healthy_rows, healthy_span, healthy_conflicts = funding_rows(4, "S", now)
    assert len(healthy_rows) == 90 and healthy_conflicts == []  # 30 days, every 8 hours
    assert healthy_span[2] < now - 30 * 86_400_000 and healthy_span[3] == now - 3_600_000
    assert healthy_rows[0][2] == healthy_span[3] - 5 and all(r[2] <= healthy_span[3] for r in healthy_rows)
    assert len(funding_rows(5, "S", now)[0]) == 720  # every 5th series settles hourly
    _, shallow_span, _ = funding_rows(1, "S", now)
    assert shallow_span[3] - shallow_span[2] == 3 * 86_400_000
    behind_rows, behind_span, _ = funding_rows(2, "S", now)
    assert behind_span[3] == now - 5 * 3_600_000 and behind_rows
    quiet_rows, quiet_span, quiet_conflicts = funding_rows(3, "S", now)
    assert quiet_rows == [] and quiet_span and quiet_conflicts == []
    assert len(funding_rows(7, "S", now)[2]) == 1 and len(funding_rows(0, "S", now)[2]) == 1


def test_the_database_builder_adds_funding_only_when_asked(tmp_path: Any) -> None:
    import sqlite3

    for flag, expected in ((False, 0), (True, 3)):
        db = tmp_path / f"{flag}.db"
        build_database(db, series=3, candle_rows=20, oi_rows=20, now_ms=T0, funding=flag)
        conn = sqlite3.connect(db)
        try:
            assert conn.execute("SELECT COUNT(*) FROM funding_rate_verified").fetchone()[0] == expected
            assert (conn.execute("SELECT COUNT(*) FROM funding_rates").fetchone()[0] > 0) is flag
        finally:
            conn.close()


# --- the command ---------------------------------------------------------------------------------------------------------

REAL_CONNECT = socket.socket.connect
REAL_CONNECT_EX = socket.socket.connect_ex
REAL_CREATE_CONNECTION = socket.create_connection


def test_the_real_funding_mode_reports_total_phases_and_cost(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket.socket, "connect", REAL_CONNECT)
    monkeypatch.setattr(socket.socket, "connect_ex", REAL_CONNECT_EX)
    monkeypatch.setattr(socket, "create_connection", REAL_CREATE_CONNECTION)

    assert (
        main(
            [
                "--series", "8", "--profile", "smoke", "--oi-mode", "real", "--funding-mode", "real",
                "--refreshes", "2", "--sustained-minutes", "0",
            ]
        )
        == 0
    )  # fmt: skip

    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["parameters"]["funding_mode"] == "real"
    assert out["evaluate"]["AC2"]["pass"] is True  # every call of every phase answered 200, none was skipped
    assert out["evaluate"]["AC3"]["value"]["funding"]["n"] == 16  # 8 symbols x 2 refreshes
    assert set(out["breakdown"]["phases"]) == set(PHASES)
    assert out["breakdown"]["phases"]["funding"]["calls"] == 8.0 and out["breakdown"]["phases"]["funding"]["mean"] > 0
    assert out["without_funding"]["breakdown"]["phases"]["funding"]["calls"] == 0.0
    assert out["without_funding"]["evaluate"]["AC3"]["value"].get("funding") is None
    assert out["funding_cost"]["added_s"] > 0 and out["funding_cost"]["mean_funding_call_ms"] > 0
    assert out["breakdown"]["total"]["mean"] >= out["without_funding"]["breakdown"]["total"]["mean"] * 0.5
    assert "TOTAL" in captured.err and "funding added" in captured.err and "without the funding phase" in captured.err
    assert NEWEST > 0  # the shared fixtures of the model tests are imported for the fake only
