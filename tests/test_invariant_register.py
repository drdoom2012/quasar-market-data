"""Integrity tests for docs/invariants.md.

These tests check the register's integrity only: the table parses, IDs are unique, statuses are one of the three
allowed values, every ENFORCED row points at a test function that exists in the repository, and every PLANNED or
NOT_APPLICABLE row carries a target or a reason. They do NOT prove that any invariant holds, and they do not run or
verify the referenced tests; the referenced tests run in `make check` on their own.
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = REPO_ROOT / "docs" / "invariants.md"
COLUMNS = ("ID", "Invariant", "Status", "Mechanism", "Evidence / Target / Reason")
STATUSES = ("ENFORCED", "PLANNED", "NOT_APPLICABLE")


class RegisterError(ValueError):
    """The register text is not a well-formed table."""


@dataclass(frozen=True)
class Row:
    id: str
    invariant: str
    status: str
    mechanism: str
    detail: str


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_register(text: str) -> list[Row]:
    """Parse the one markdown table whose header is exactly COLUMNS; cells cannot contain a literal pipe."""
    lines = text.splitlines()
    for start, line in enumerate(lines):
        if line.lstrip().startswith("|") and tuple(_cells(line)) == COLUMNS:
            break
    else:
        raise RegisterError(f"no table with header {' | '.join(COLUMNS)}")
    separator = lines[start + 1] if start + 1 < len(lines) else ""
    if not re.fullmatch(r"\|?[\s:|-]+\|?", separator) or "-" not in separator:
        raise RegisterError("header is not followed by a separator line")
    rows: list[Row] = []
    for line in lines[start + 2 :]:
        if not line.lstrip().startswith("|"):
            break
        cells = _cells(line)
        if len(cells) != len(COLUMNS):
            raise RegisterError(f"row has {len(cells)} cells, expected {len(COLUMNS)}: {line!r}")
        rows.append(Row(*cells))
    return rows


EVIDENCE_REF = re.compile(r"(?P<path>[^:\s]+\.py)::(?P<name>[A-Za-z_]\w*)(?:\[(?P<case>[^\]]+)\])?")


def _evidence_errors(row: Row, repo_root: Path) -> list[str]:
    """Check each comma-separated `path::test_name[optional case id]` reference of an ENFORCED row."""
    errors: list[str] = []
    refs = [ref.strip().strip("`") for ref in row.detail.split(",") if ref.strip()]
    if not refs:
        return [f"{row.id}: ENFORCED evidence must be path::name, got an empty cell"]
    for ref in refs:
        match = EVIDENCE_REF.fullmatch(ref)
        if match is None:
            errors.append(f"{row.id}: evidence {ref!r} is not path::name")
            continue
        path = Path(match["path"])
        if path.is_absolute() or ".." in path.parts or path.parts[0] != "tests":
            errors.append(f"{row.id}: evidence {ref!r} must point to a file under tests/")
            continue
        name = match["name"]
        if not name.startswith("test_"):
            errors.append(f"{row.id}: evidence function {name!r} must start with test_")
            continue
        file = repo_root / path
        if not file.is_file():
            errors.append(f"{row.id}: evidence file {path} does not exist")
            continue
        source = file.read_text()
        defined = {
            node.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        if name not in defined:
            errors.append(f"{row.id}: {path} does not define {name}")
        elif match["case"] is not None and match["case"] not in source:
            errors.append(f"{row.id}: case id {match['case']!r} does not appear in {path}")
    return errors


def validate_register(rows: list[Row], repo_root: Path) -> list[str]:
    """Return every integrity problem found; an empty list means the register is well formed."""
    errors: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not row.id:
            errors.append(f"empty id on row {row.invariant!r}")
        elif row.id in seen:
            errors.append(f"duplicate id {row.id}")
        seen.add(row.id)
        if row.status not in STATUSES:
            errors.append(f"{row.id}: unknown status {row.status!r}; allowed: {', '.join(STATUSES)}")
        elif row.status == "ENFORCED":
            errors += _evidence_errors(row, repo_root)
        elif not row.detail:
            errors.append(f"{row.id}: {row.status} row has an empty target/reason")
    for status in ("ENFORCED", "PLANNED"):
        if not any(row.status == status for row in rows):
            errors.append(f"no {status} row in the register")
    return errors


# --- unit tests of the parser and validator, using inline tables -------------------------------------------------

HEADER = "| ID | Invariant | Status | Mechanism | Evidence / Target / Reason |\n|---|---|---|---|---|\n"


def table(*rows: str) -> str:
    return "Intro text.\n\n" + HEADER + "".join(row + "\n" for row in rows)


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_sample.py").write_text(
        "import pytest\n\n"
        "CASES = {'case_a': 1}\n\n"
        "def test_present() -> None:\n    pass\n\n"
        "async def test_async_present() -> None:\n    pass\n\n"
        "def helper() -> None:\n    pass\n"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.py").write_text("def test_elsewhere() -> None:\n    pass\n")
    return tmp_path


def errors_for(text: str, repo: Path) -> list[str]:
    return validate_register(parse_register(text), repo)


def test_parser_reads_rows() -> None:
    rows = parse_register(table("| A-1 | Something | PLANNED | none yet | S9 |"))
    assert rows == [Row("A-1", "Something", "PLANNED", "none yet", "S9")]


def test_parser_ignores_text_outside_the_table() -> None:
    text = table("| A-1 | x | PLANNED | m | S9 |") + "\nTrailing | pipes | outside | the table\n"
    assert len(parse_register(text)) == 1


@pytest.mark.parametrize(
    "text",
    [
        "no table here\n",
        "| ID | Invariant | Status |\n|---|---|---|\n| A | b | PLANNED |\n",
        HEADER + "| A-1 | only | three |\n",
    ],
    ids=["no_table", "wrong_columns", "short_row"],
)
def test_parser_rejects_malformed_tables(text: str) -> None:
    with pytest.raises(RegisterError):
        parse_register(text)


def test_valid_inline_register_has_no_errors(fake_repo: Path) -> None:
    text = table(
        "| A-1 | one | ENFORCED | test | tests/test_sample.py::test_present |",
        "| A-2 | two | ENFORCED | test | tests/test_sample.py::test_async_present[case_a] |",
        "| A-3 | three | PLANNED | none | S9 |",
        "| A-4 | four | NOT_APPLICABLE | process | human review |",
    )
    assert errors_for(text, fake_repo) == []


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ("| A-1 | x | MAYBE | m | S9 |", "unknown status"),
        ("| A-1 | x | planned | m | S9 |", "unknown status"),
        ("| A-1 | x | ENFORCED | m | tests/test_sample.py::test_missing |", "does not define"),
        ("| A-1 | x | ENFORCED | m | tests/test_sample.py::helper |", "must start with test_"),
        ("| A-1 | x | ENFORCED | m | tests/no_such_file.py::test_present |", "does not exist"),
        ("| A-1 | x | ENFORCED | m | tests/test_sample.py |", "path::name"),
        ("| A-1 | x | ENFORCED | m | tests/test_sample.py::test_present[nope] |", "case id"),
        ("| A-1 | x | ENFORCED | m | ../outside.py::test_present |", "under tests/"),
        ("| A-1 | x | ENFORCED | m | docs/note.py::test_elsewhere |", "under tests/"),
        ("| A-1 | x | ENFORCED | m |  |", "path::name"),
        ("| A-1 | x | PLANNED | m |  |", "empty"),
        ("| A-1 | x | NOT_APPLICABLE | m |  |", "empty"),
        ("|  | x | PLANNED | m | S9 |", "empty id"),
    ],
    ids=[
        "unknown_status",
        "status_is_case_sensitive",
        "enforced_missing_test",
        "enforced_not_a_test",
        "enforced_missing_file",
        "enforced_no_function",
        "enforced_bad_case_id",
        "enforced_path_escape",
        "enforced_outside_tests",
        "enforced_empty_evidence",
        "planned_empty_target",
        "not_applicable_empty_reason",
        "empty_id",
    ],
)
def test_validator_rejects_bad_row(fake_repo: Path, row: str, expected: str) -> None:
    errors = errors_for(table(row), fake_repo)
    assert any(expected in error for error in errors), errors


def test_validator_accepts_backticked_references(fake_repo: Path) -> None:
    text = table(
        "| A-1 | x | ENFORCED | m | `tests/test_sample.py::test_present` |",
        "| A-2 | y | PLANNED | m | S9 |",
    )
    assert errors_for(text, fake_repo) == []


def test_validator_rejects_prose_in_evidence_cell(fake_repo: Path) -> None:
    text = table(
        "| A-1 | x | ENFORCED | m | tests/test_sample.py::test_present is the negative test |",
        "| A-2 | y | PLANNED | m | S9 |",
    )
    assert any("path::name" in error for error in errors_for(text, fake_repo))


def test_validator_rejects_duplicate_ids(fake_repo: Path) -> None:
    text = table("| A-1 | one | PLANNED | m | S9 |", "| A-1 | two | PLANNED | m | S9 |")
    assert any("duplicate id" in error for error in errors_for(text, fake_repo))


@pytest.mark.parametrize("missing", ["ENFORCED", "PLANNED"])
def test_validator_requires_enforced_and_planned_rows(fake_repo: Path, missing: str) -> None:
    rows = {
        "ENFORCED": "| A-1 | x | ENFORCED | m | tests/test_sample.py::test_present |",
        "PLANNED": "| A-2 | y | PLANNED | m | S9 |",
    }
    remaining = [row for status, row in rows.items() if status != missing]
    errors = errors_for(table(*remaining), fake_repo)
    assert any(f"no {missing} row" in error for error in errors), errors


def test_validator_accepts_several_evidence_references(fake_repo: Path) -> None:
    text = table(
        "| A-1 | x | ENFORCED | m | tests/test_sample.py::test_present, tests/test_sample.py::test_async_present |",
        "| A-2 | y | PLANNED | m | S9 |",
    )
    assert errors_for(text, fake_repo) == []


def test_validator_rejects_one_bad_reference_among_several(fake_repo: Path) -> None:
    text = table(
        "| A-1 | x | ENFORCED | m | tests/test_sample.py::test_present, tests/test_sample.py::test_missing |",
        "| A-2 | y | PLANNED | m | S9 |",
    )
    assert any("does not define" in error for error in errors_for(text, fake_repo))


# --- the real register -------------------------------------------------------------------------------------------


def test_real_register_is_well_formed() -> None:
    assert REGISTER_PATH.is_file(), f"missing {REGISTER_PATH}"
    rows = parse_register(REGISTER_PATH.read_text())
    assert validate_register(rows, REPO_ROOT) == []
