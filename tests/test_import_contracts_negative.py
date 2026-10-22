"""Negative tests for the import-linter contracts in pyproject.toml.

Each case copies the real package and the real pyproject.toml into tmp_path, injects a violating module into the
copy and runs lint-imports against the copy. The repo working tree is never modified.
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "quasar_market_data"
DOMAIN_CONTRACT = "domain is pure"
INDEPENDENCE_CONTRACT = "collector and api are independent"
STORAGE_CONTRACT = "storage is a leaf below collector and api"
CONTRACT_NAMES = (DOMAIN_CONTRACT, INDEPENDENCE_CONTRACT, STORAGE_CONTRACT)


@dataclass(frozen=True)
class Case:
    probe_path: str | None  # relative to the package root; None means the unmodified copy
    probe_source: str
    broken_contract: str | None  # None means every contract must be kept


CASES = {
    "positive_control_unmodified_copy_passes": Case(None, "", None),
    "domain_importing_sqlite3_fails_domain_is_pure": Case("domain/_probe.py", "import sqlite3\n", DOMAIN_CONTRACT),
    "collector_importing_api_fails_independence": Case(
        "collector/_probe.py", f"import {PACKAGE}.api\n", INDEPENDENCE_CONTRACT
    ),
    "domain_importing_collector_fails_domain_is_pure": Case(
        "domain/_probe.py", f"import {PACKAGE}.collector\n", DOMAIN_CONTRACT
    ),
    "domain_importing_storage_fails_domain_is_pure": Case(
        "domain/_probe.py", f"import {PACKAGE}.storage\n", DOMAIN_CONTRACT
    ),
    "storage_importing_collector_fails_storage_leaf": Case(
        "storage/_probe.py", f"import {PACKAGE}.collector\n", STORAGE_CONTRACT
    ),
    "storage_importing_api_fails_storage_leaf": Case("storage/_probe.py", f"import {PACKAGE}.api\n", STORAGE_CONTRACT),
    "storage_importing_sqlite3_is_allowed": Case("storage/_probe.py", "import sqlite3\n", None),
}


def find_lint_imports() -> str:
    """Locate lint-imports next to the running interpreter, falling back to PATH."""
    candidate = Path(sys.executable).parent / "lint-imports"
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("lint-imports")
    if found is None:
        pytest.fail("lint-imports executable not found next to sys.executable or on PATH")
    return found


def run_lint_imports(workdir: Path, *, config: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(workdir / "src")}
    return subprocess.run(
        [find_lint_imports(), "--config", str(config or workdir / "pyproject.toml")],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def make_copy(tmp_path: Path) -> Path:
    """Copy the real package and pyproject.toml; bytecode caches are skipped."""
    shutil.copytree(
        REPO_ROOT / "src" / PACKAGE,
        tmp_path / "src" / PACKAGE,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy(REPO_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    return tmp_path


@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
def test_import_contracts_on_copied_package(tmp_path: Path, case: Case) -> None:
    workdir = make_copy(tmp_path)
    if case.probe_path is not None:
        (workdir / "src" / PACKAGE / case.probe_path).write_text(case.probe_source)

    result = run_lint_imports(workdir)
    output = result.stdout + result.stderr

    if case.broken_contract is None:
        assert result.returncode == 0, output
        assert f"{len(CONTRACT_NAMES)} kept, 0 broken" in output
        return

    assert result.returncode != 0, output
    assert "1 broken" in output, output
    assert f"{case.broken_contract} BROKEN" in output, output
    for kept in CONTRACT_NAMES:
        if kept != case.broken_contract:
            assert f"{kept} KEPT" in output, output
    assert case.probe_path is not None
    assert Path(case.probe_path).stem in output  # the injected module appears in the reported chain


def test_removing_forbidden_module_from_copied_config_makes_domain_probe_pass(tmp_path: Path) -> None:
    """Proves the sqlite3 case is real: the same probe passes once sqlite3 is dropped from the copied contract."""
    workdir = make_copy(tmp_path)
    (workdir / "src" / PACKAGE / "domain" / "_probe.py").write_text("import sqlite3\n")
    config = workdir / "pyproject.toml"
    original = config.read_text()
    weakened = original.replace('    "sqlite3",\n', "")
    assert weakened != original

    assert run_lint_imports(workdir).returncode != 0
    config.write_text(weakened)
    result = run_lint_imports(workdir)
    assert result.returncode == 0, result.stdout + result.stderr
