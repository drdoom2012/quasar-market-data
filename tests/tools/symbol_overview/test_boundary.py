"""The symbol overview tool must stay a stdlib-only, separate consumer of the HTTP API.

An AST scan of ``tools/symbol_overview/`` (the same technique as ``tests/test_domain_purity.py``): every direct
``import`` and ``from ... import`` is checked against an allowlist of standard-library modules. It does not follow
transitive imports or computed names. ``lint-imports`` does not cover ``tools/``; this test does.
"""

import ast
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parents[3] / "tools" / "symbol_overview"
TOOL_PACKAGE = "tools.symbol_overview"
ALLOWED: frozenset[str] = frozenset(
    {
        "argparse",
        "collections",
        "concurrent",
        "dataclasses",
        "enum",
        "http",
        "json",
        "pathlib",
        "sys",
        "threading",
        "time",
        "typing",
        "urllib",
        "__future__",
    }
)
FORBIDDEN_CALLS = frozenset({"__import__", "eval", "exec", "compile"})


def _own_module(name: str) -> bool:
    return name == TOOL_PACKAGE or name.startswith(TOOL_PACKAGE + ".")


def forbidden_imports(source: str, allowed: frozenset[str]) -> set[str]:
    """Top-level names imported anywhere in ``source`` (also inside functions) that are not allowed."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names if not _own_module(a.name)} - allowed
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level >= 2:
                found.add("." * node.level + module)  # leaves the tool package
            elif node.level == 1 or _own_module(module):
                continue
            elif module.split(".")[0] not in allowed:
                found.add(module.split(".")[0])
    return found


def test_detects_forbidden_imports() -> None:
    source = "import sqlite3\nfrom quasar_market_data.api import app\n"
    assert forbidden_imports(source, ALLOWED) == {"sqlite3", "quasar_market_data"}


def test_detects_a_forbidden_import_inside_a_function() -> None:
    assert forbidden_imports("def f():\n    import httpx\n", ALLOWED) == {"httpx"}


def test_detects_a_relative_import_that_leaves_the_package() -> None:
    assert forbidden_imports("from .. import quasar_market_data\n", ALLOWED) == {".."}


def test_allows_the_standard_library_and_the_tool_itself() -> None:
    source = (
        "import json\nfrom urllib.parse import quote\nfrom . import model\n"
        "import tools.symbol_overview.model\nfrom tools.symbol_overview import window\n"
    )
    assert forbidden_imports(source, ALLOWED) == set()


def test_tool_directory_exists() -> None:
    assert TOOL_DIR.is_dir()


@pytest.mark.parametrize("path", sorted(TOOL_DIR.rglob("*.py")), ids=lambda p: p.name)
def test_tool_sources_import_only_allowed_modules(path: Path) -> None:
    assert forbidden_imports(path.read_text(), ALLOWED) == set()


@pytest.mark.parametrize("path", sorted(TOOL_DIR.rglob("*.py")), ids=lambda p: p.name)
def test_tool_sources_do_not_call_dynamic_imports(path: Path) -> None:
    calls = {
        node.func.id
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS
    }
    assert calls == set()
