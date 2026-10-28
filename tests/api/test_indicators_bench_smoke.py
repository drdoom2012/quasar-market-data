"""A functional check of ``make bench-indicators`` at its smallest size (no timing is asserted)."""

import json
import socket
import tempfile
from pathlib import Path

import pytest

from tests.api.bench_indicators import PHASES, BenchError, main, run

# Captured at import, before the autouse guard of conftest.py patches them for each test.
REAL_CONNECT = socket.socket.connect
REAL_CONNECT_EX = socket.socket.connect_ex
REAL_CREATE_CONNECTION = socket.create_connection


def test_the_s18_profile_runs_and_prints_the_four_phases(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--profile", "s18", "--repeat", "1", "--series", "1", "--concurrency", "0"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert set(result["phases_ms"]) == {"read", "compute", "serialize", "request"} == set(PHASES)
    for phase in PHASES:
        assert set(result["phases_ms"][phase]) == {"min", "median", "max"}
    assert result["window_slots"] == 2016
    assert result["read_range_slots"] == 3016
    assert result["profile"] == "s18"
    assert result["served"] == {}
    assert result["body_bytes"] > 0
    assert result["environment"]["note"] == "page cache not dropped"
    for key in ("platform", "machine", "python", "sqlite", "talib", "ta_lib_c", "numpy"):
        assert result["environment"][key]


SERVED_KEYS = {
    "latency_ms",
    "body_bytes",
    "health_ms",
    "health_over_100ms",
    "server_cpu_user_s",
    "server_cpu_system_s",
    "server_peak_rss_bytes",
    "stopped",
}


def test_the_s19_profile_serves_concurrent_requests_and_probes_health(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The benchmark's own client talks to its own server process on 127.0.0.1: lift the guard for this test only.
    monkeypatch.setattr(socket.socket, "connect", REAL_CONNECT)
    monkeypatch.setattr(socket.socket, "connect_ex", REAL_CONNECT_EX)
    monkeypatch.setattr(socket, "create_connection", REAL_CREATE_CONNECTION)
    assert main(["--profile", "s19", "--repeat", "1", "--series", "1", "--concurrency", "1"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["profile"] == "s19"
    assert result["read_range_slots"] == 3516
    assert set(result["phases_ms"]) == set(PHASES)
    served = result["served"]["1"]
    assert SERVED_KEYS <= set(served)
    for key in ("latency_ms", "health_ms"):
        assert set(served[key]) == {"min", "median", "max"}
    assert served["body_bytes"] > 0
    assert isinstance(served["health_over_100ms"], int)
    assert served["server_peak_rss_bytes"] > 10_000_000  # a KiB/bytes mix-up would fail here
    assert served["server_cpu_user_s"] > 0
    assert served["stopped"] in ("sigint", "sigkill")
    assert result["served_note"]


def test_the_benchmark_refuses_an_existing_directory(tmp_path: Path) -> None:
    existing = tmp_path / "there"
    existing.mkdir()

    with pytest.raises(BenchError):
        run(existing, repeat=1, series=1)
    assert existing.exists()


def test_the_benchmark_builds_in_a_temporary_directory_it_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix: str(tmp_path / "made"))

    assert main(["--repeat", "1", "--series", "1", "--concurrency", "0"]) == 0
    assert not (tmp_path / "made").exists()
