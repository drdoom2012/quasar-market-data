"""A functional check of ``make bench-indicators`` at its smallest size (no timing is asserted)."""

import json
import tempfile
from pathlib import Path

import pytest

from tests.api.bench_indicators import PHASES, BenchError, main, run


def test_the_benchmark_runs_and_prints_the_four_phases(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--repeat", "1", "--series", "1"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert set(result["phases_ms"]) == {"read", "compute", "serialize", "request"} == set(PHASES)
    for phase in PHASES:
        assert set(result["phases_ms"][phase]) == {"min", "median", "max"}
    assert result["window_slots"] == 2016
    assert result["read_range_slots"] == 3016
    assert result["body_bytes"] > 0
    assert result["environment"]["note"] == "page cache not dropped"
    for key in ("platform", "machine", "python", "sqlite", "talib", "ta_lib_c", "numpy"):
        assert result["environment"][key]


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

    assert main(["--repeat", "1", "--series", "1"]) == 0
    assert not (tmp_path / "made").exists()
