import ast
import json
import platform
import sqlite3
import tempfile
from pathlib import Path

import pytest

from tests.collector import bench_reconcile
from tests.collector.bench_reconcile import build_database, main, run

STATEMENT_CLASSES = {
    "bounds",
    "candles",
    "conflicts",
    "unavailable",
    "holes",
    "largest_runs",
    "unavailable_in_holes",
    "conflicted",
}


def test_smoke_profile_reports_every_field(tmp_path: Path) -> None:
    result = run("smoke", tmp_path / "bench", keep=False)
    assert result["profile"] == "smoke"
    assert result["symbols"] == 3
    assert result["rows_total"] == 6000
    assert result["rows_visited"] == 6000
    assert result["db_bytes"] > 0
    assert result["total_ms"] >= 0
    assert result["first_run_ms"] >= 0 and result["second_run_ms"] >= 0
    assert result["sqlite_version"] == sqlite3.sqlite_version
    assert result["python_version"] == platform.python_version()
    assert "cache" in result["note"]
    statements = result["statements"]
    assert STATEMENT_CLASSES <= set(statements)
    for name in STATEMENT_CLASSES:
        entry = statements[name]
        assert entry["count"] >= 1
        assert entry["total_ms"] >= 0
        assert entry["plan"] and all(isinstance(detail, str) for detail in entry["plan"])
    assert statements["bounds"]["count"] == 3
    json.dumps(result)  # the result is printed as JSON


def test_rows_visited_equals_rows_stored(tmp_path: Path) -> None:
    path = tmp_path / "two-holes.db"
    stats = build_database(path, symbols=2, rows_per_symbol=500, holes_per_symbol=2)
    assert stats.rows_total == 1000
    conn = sqlite3.connect(path)
    counts = [
        conn.execute("SELECT COUNT(*) FROM candles WHERE symbol = ?", (symbol,)).fetchone()[0]
        for (symbol,) in conn.execute("SELECT DISTINCT symbol FROM candles").fetchall()
    ]
    conn.close()
    assert sum(counts) == stats.rows_total
    result = run("smoke", tmp_path / "bench", keep=False)
    assert result["pairs_visited"] == result["rows_total"] - result["symbols"]


def test_build_database_is_deterministic_and_has_the_requested_facts(tmp_path: Path) -> None:
    first = build_database(tmp_path / "a.db", symbols=2, rows_per_symbol=300)
    second = build_database(tmp_path / "b.db", symbols=2, rows_per_symbol=300)
    assert first == second
    assert (first.holes, first.unavailable, first.conflicts) == (6, 20, 4)
    conn = sqlite3.connect(tmp_path / "a.db")
    assert conn.execute("SELECT COUNT(*) FROM unavailable_candles").fetchone()[0] == 20
    assert conn.execute("SELECT COUNT(*) FROM candle_conflicts").fetchone()[0] == 4
    conn.close()


def test_benchmark_refuses_a_non_empty_directory_before_creating_anything(tmp_path: Path) -> None:
    taken = tmp_path / "taken"
    taken.mkdir()
    (taken / "precious.db").write_text("keep me")
    assert main(["--profile", "smoke", "--dir", str(taken)]) != 0
    assert [p.name for p in taken.iterdir()] == ["precious.db"]
    assert (taken / "precious.db").read_text() == "keep me"


def test_the_default_location_is_a_fresh_mkdtemp(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[str] = []
    real = tempfile.mkdtemp

    def spy(*args: str, **kwargs: str) -> str:
        path = real(*args, **kwargs)
        made.append(path)
        return path

    monkeypatch.setattr("tempfile.mkdtemp", spy)
    result = run("smoke", None, keep=False)
    assert len(made) == 1
    assert result["directory"] == made[0]
    assert not Path(made[0]).exists()


def test_the_benchmark_database_is_removed_unless_kept(tmp_path: Path) -> None:
    gone = tmp_path / "gone"
    run("smoke", gone, keep=False)
    assert not gone.exists()
    kept = tmp_path / "kept"
    result = run("smoke", kept, keep=True)
    assert (kept / "bench.db").exists()
    assert result["kept"] is True


def test_the_benchmark_never_imports_a_network_module() -> None:
    tree = ast.parse(Path(bench_reconcile.__file__).read_text("utf-8"))
    forbidden = {"ccxt", "socket", "ssl", "urllib", "http", "httpx", "requests", "aiohttp"}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])
    assert imported & forbidden == set()


def test_unknown_profile_is_refused() -> None:
    with pytest.raises(SystemExit) as info:
        main(["--profile", "nonsense"])
    assert info.value.code != 0
