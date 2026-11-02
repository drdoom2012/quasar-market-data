import ast
from importlib import resources
from pathlib import Path

import pytest

from quasar_market_data.collector.async_open_interest_writer import DEFAULT_OPEN_INTEREST_BUSY_TIMEOUT_MS
from quasar_market_data.collector.open_interest_writer import INSERT_OPEN_INTEREST
from tools import bench_open_interest_write as bench

SCHEMA = Path(str(resources.files("quasar_market_data.storage.migrations").joinpath("002_open_interest.sql")))
FIELDS = ("rows_per_tx=100", "transactions=5", "p50_ms=", "p99_ms=", "max_ms=", "dir=", "sqlite=", "date=")


def test_prints_every_field_and_exits_zero_without_a_threshold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "run"
    assert bench.main(["--schema", str(SCHEMA), "--dir", str(target), "--transactions", "5"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("oi_write ")
    for field in FIELDS:
        assert field in line


def test_a_missing_dir_is_created_and_removed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "run"
    bench.main(["--schema", str(SCHEMA), "--dir", str(target), "--transactions", "2"])
    assert not target.exists()


def test_keep_leaves_the_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "run"
    bench.main(["--schema", str(SCHEMA), "--dir", str(target), "--transactions", "2", "--keep"])
    assert (target / bench.DB_NAME).exists()


def test_a_non_empty_dir_is_refused_before_writing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "run"
    target.mkdir()
    (target / "other.db").write_text("x")
    assert bench.main(["--schema", str(SCHEMA), "--dir", str(target), "--transactions", "2"]) == 2
    assert sorted(p.name for p in target.iterdir()) == ["other.db"]
    assert "nothing was written" in capsys.readouterr().err


def test_threshold_sets_the_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["--schema", str(SCHEMA), "--transactions", "2", "--max-p99-ms"]
    assert bench.main([*args, "0"]) == 1
    assert bench.main([*args, "100000"]) == 0


def test_nearest_rank() -> None:
    values = list(range(1, 101))
    assert bench.nearest_rank(values, 50) == 50
    assert bench.nearest_rank(values, 99) == 99
    assert bench.nearest_rank([7], 99) == 7
    assert bench.nearest_rank([1, 2, 3, 4], 50) == 2
    with pytest.raises(ValueError):
        bench.nearest_rank([], 50)


def test_insert_statement_matches_the_writer() -> None:
    assert bench.INSERT_OPEN_INTEREST == INSERT_OPEN_INTEREST


def test_imports_only_the_standard_library() -> None:
    tree = ast.parse(Path(bench.__file__).read_text("utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {
        "argparse",
        "shutil",
        "sqlite3",
        "sys",
        "tempfile",
        "time",
        "collections",
        "datetime",
        "pathlib",
        "typing",
    }


def test_busy_timeout_matches_the_writer() -> None:
    assert bench.BUSY_TIMEOUT_MS == DEFAULT_OPEN_INTEREST_BUSY_TIMEOUT_MS


def test_readme_runs_the_benchmark_in_the_dockerfile_runtime_image() -> None:
    root = Path(__file__).resolve().parents[2]
    runtime = next(
        line.split()[1]
        for line in (root / "Dockerfile").read_text("utf-8").splitlines()
        if line.startswith("FROM ") and line.rstrip().endswith("AS runtime")
    )
    assert "@sha256:" in runtime
    assert f"/src:ro {runtime} \\" in (root / "README.md").read_text("utf-8")
