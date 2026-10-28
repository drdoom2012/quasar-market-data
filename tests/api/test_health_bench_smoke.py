import ast
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.api.health_reader import read_stored_series
from tests.api import bench_health
from tests.api.bench_health import BenchError, run


def test_smoke_profile_reports_the_three_criteria(tmp_path: Path) -> None:
    result = run("smoke", tmp_path / "bench", keep=False)

    assert result["series"] == 20
    assert result["H1_steps_within_2x"] is True
    assert result["H3_writer_does_not_break_or_change_reads"] is True
    assert result["H3_commits_during_reads"] == 5  # the writer really committed in the middle of every read
    assert result["H3_reads_that_differed_from_their_snapshot"] == 0
    assert isinstance(result["H2_snapshot_ms_max"], float)
    assert not (tmp_path / "bench").exists()  # removed unless kept


def test_h3_can_fail_when_a_read_is_not_one_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = read_stored_series

    def without_snapshot(conn: Any) -> Any:
        # the same reads without BEGIN: each statement sees the latest commit
        class Autocommit:
            def execute(self, sql: str, params: Any = ()) -> Any:
                if sql in ("BEGIN", "ROLLBACK"):
                    return conn.execute("SELECT 1")
                return conn.execute(sql, params)

        return real(Autocommit())  # type: ignore[arg-type]

    monkeypatch.setattr(bench_health, "read_stored_series", without_snapshot)

    result = run("smoke", tmp_path / "bench", keep=False)

    assert result["H3_writer_does_not_break_or_change_reads"] is False
    assert result["H3_reads_that_differed_from_their_snapshot"] > 0


def test_the_benchmark_refuses_an_existing_directory_and_an_unknown_profile(tmp_path: Path) -> None:
    existing = tmp_path / "there"
    existing.mkdir()

    with pytest.raises(BenchError):
        run("smoke", existing, keep=False)
    with pytest.raises(BenchError):
        run("nope", tmp_path / "x", keep=False)


def test_the_benchmark_never_imports_a_network_module() -> None:
    tree = ast.parse(Path(bench_health.__file__).read_text("utf-8"))
    modules = {
        alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    } | {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert modules.isdisjoint({"socket", "httpx", "requests", "aiohttp", "ccxt", "urllib"})
