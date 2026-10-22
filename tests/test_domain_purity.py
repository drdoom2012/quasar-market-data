import ast
from pathlib import Path

import pytest

DOMAIN_DIR = Path(__file__).resolve().parent.parent / "src" / "quasar_market_data" / "domain"

ALLOWED_EXTERNAL_IMPORTS: frozenset[str] = frozenset(
    {"__future__", "collections", "dataclasses", "decimal", "enum", "re", "typing"}
)
DOMAIN_PACKAGE = "quasar_market_data.domain"
FORBIDDEN_CALLS = frozenset({"open", "__import__", "eval", "exec", "compile"})


def _is_domain_module(name: str) -> bool:
    return name == DOMAIN_PACKAGE or name.startswith(DOMAIN_PACKAGE + ".")


def _import_allowed(name: str) -> bool:
    return name.split(".")[0] in ALLOWED_EXTERNAL_IMPORTS or _is_domain_module(name)


def find_violations(source: str) -> list[str]:
    """Direct-import and forbidden-call check; does not follow transitive imports or computed names."""
    violations: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            violations += [f"import {a.name}" for a in node.names if not _import_allowed(a.name)]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level >= 2:
                violations.append(f"from {'.' * node.level}{module} import ...")
            elif node.level == 1:
                continue  # relative to the domain package itself
            elif module == "quasar_market_data":
                violations += [f"from {module} import {a.name}" for a in node.names if a.name != "domain"]
            elif not _import_allowed(module):
                violations.append(f"from {module} import ...")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
            violations.append(f"call {node.func.id}()")
    return violations


@pytest.mark.parametrize(
    ("source", "expected_count"),
    [
        ("import re", 0),
        ("from dataclasses import dataclass", 0),
        ("from quasar_market_data.domain.candle import Candle", 0),
        ("import sqlite3", 1),
        ("import datetime", 1),
        ("from quasar_market_data import api", 1),
        ("from quasar_market_data.collector import cli", 1),
        ("from ..collector import cli", 1),
        ("def f():\n    import os", 1),
        ('open("x")', 1),
        ('__import__("os")', 1),
    ],
)
def test_find_violations_counts(source: str, expected_count: int) -> None:
    assert len(find_violations(source)) == expected_count


def test_allowlist_is_the_documented_one() -> None:
    assert ALLOWED_EXTERNAL_IMPORTS == frozenset(
        {"__future__", "collections", "dataclasses", "decimal", "enum", "re", "typing"}
    )


def test_domain_package_has_no_violations() -> None:
    files = sorted(DOMAIN_DIR.rglob("*.py"))
    assert files, "domain package has no sources to check"
    found = {f.name: find_violations(f.read_text()) for f in files}
    assert {name: v for name, v in found.items() if v} == {}
