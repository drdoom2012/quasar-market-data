"""Read-only and no-network boundaries of the ``api`` package (INV-014, INV-044).

What each layer enforces, and no more:

* import-linter (``api reaches no network client``, ``ccxt is imported only by the Binance adapter``): the ``api``
  package has no import of ``httpx``, ``requests``, ``aiohttp``, ``websockets``, ``socket``, ``ssl`` or ``ccxt``.
* ``find_violations`` (an AST scan of the ``api`` source): no import or use (``urllib.request``, ``http.client`` also through ``import urllib``) of ``urllib.request``, ``http.client``,
  ``importlib`` (except ``importlib.metadata``), ``ctypes`` or ``subprocess``; no ``__import__``, also as ``builtins.__import__``; no ``sqlite3.connect`` (only ``storage.connection.open_reader`` opens a database); no call of an attribute named
  ``open_connection`` or ``create_connection``; and, for the read-only property, no ``.cursor(``, ``executescript``,
  ``executemany`` and no use of ``open_writer``, ``migrate``, ``CandleWriter``, ``AsyncCandleWriter``, ``Migration``.
  A name-based scan is bypassed by aliasing or a transitive dependency: it is a tripwire, not a proof.
* the socket guard in ``conftest.py``: applies only to the requests the API tests execute.

Not covered: network access from code outside ``api``, from a third-party package, through UDP ``sendto``, name
resolution or another process; any dynamic mechanism not named above; the deployment (S21).
"""

import ast
import hashlib
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from quasar_market_data.api.app import create_app
from quasar_market_data.domain.open_interest_coverage import OpenInterestRule
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import open_reader
from tests.api.seed import SAMPLE_RULE, SAMPLE_UNIVERSE, make_database

API_DIR = Path(__file__).resolve().parents[2] / "src" / "quasar_market_data" / "api"
FORBIDDEN_MODULES = frozenset({"urllib.request", "http.client", "importlib", "ctypes", "subprocess"})
FORBIDDEN_CALL_NAMES = frozenset({"__import__"})
FORBIDDEN_ATTRIBUTE_CALLS = frozenset(
    {"open_connection", "create_connection", "cursor", "executescript", "executemany"}
)
FORBIDDEN_IMPORTED_CALLS = frozenset({"open_connection", "create_connection"})  # from asyncio import open_connection
FORBIDDEN_NAMES = frozenset({"open_writer", "migrate", "CandleWriter", "AsyncCandleWriter", "Migration"})

TF = 300_000
S = 1792643400000
Q = f"venue=binance-usdm&symbol=BTC/USDT:USDT&timeframe=5m&start={S}"


def _forbidden_module(name: str) -> bool:
    if name == "importlib.metadata" or name.startswith("importlib.metadata."):
        return False  # reading package metadata (the CLI's --version) is not a dynamic import
    return any(name == m or name.startswith(m + ".") for m in FORBIDDEN_MODULES)


def find_violations(source: str) -> list[str]:
    """Direct-import, forbidden-call and forbidden-name check; does not follow aliases or transitive imports."""
    violations: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            violations += [f"import {a.name}" for a in node.names if _forbidden_module(a.name)]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module != "importlib" and _forbidden_module(module):
                violations.append(f"from {module} import ...")
            violations += [
                f"from {module} import {a.name}" for a in node.names if _forbidden_module(f"{module}.{a.name}")
            ]
            violations += [f"from {module} import {a.name}" for a in node.names if a.name in FORBIDDEN_NAMES]
            violations += [f"from {module} import {a.name}" for a in node.names if a.name in FORBIDDEN_IMPORTED_CALLS]
            if module == "sqlite3":
                violations += [f"from sqlite3 import {a.name}" for a in node.names if a.name == "connect"]
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALL_NAMES:
                violations.append(f"call {func.id}()")
            elif isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_ATTRIBUTE_CALLS:
                violations.append(f"call .{func.attr}()")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            violations.append(f"name {node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_NAMES:
                violations.append(f"name {node.attr}")
            if node.attr == "__import__":
                violations.append("attribute __import__")  # builtins.__import__
            if isinstance(node.value, ast.Name) and _forbidden_module(f"{node.value.id}.{node.attr}"):
                violations.append(f"use of {node.value.id}.{node.attr}")  # import urllib; urllib.request...
            if isinstance(node.value, ast.Name) and node.value.id == "sqlite3" and node.attr == "connect":
                violations.append("call sqlite3.connect")  # only storage.connection.open_reader may open a database
    return violations


@pytest.mark.parametrize(
    ("source", "reported"),
    [
        ("import urllib.request", True),
        ("from urllib import request", True),
        ("from urllib.request import urlopen", True),
        ("from http import client", True),
        ("import http.client", True),
        ("import importlib", True),
        ("from importlib import import_module", True),
        ("import ctypes", True),
        ("import subprocess", True),
        ('__import__("socket")', True),
        ("asyncio.open_connection('h', 1)", True),
        ("loop.create_connection(f, 'h', 1)", True),
        ("conn.cursor()", True),
        ("conn.executescript('x')", True),
        ("conn.executemany('x', [])", True),
        ("open_writer(path)", True),
        ("from quasar_market_data.storage.connection import open_writer", True),
        ("migrate(conn)", True),
        ("CandleWriter(conn)", True),
        ("AsyncCandleWriter.open(path)", True),
        ("x.Migration", True),
        ("from asyncio import open_connection", True),
        ("from asyncio import create_connection", True),
        ('import importlib.metadata\nimportlib.import_module("ccxt")', True),
        ("sqlite3.connect(path)", True),
        ("from sqlite3 import connect", True),
        ('builtins.__import__("socket")', True),
        ("import urllib\nurllib.request.urlopen(u)", True),
        ("import http\nhttp.client.HTTPSConnection(h)", True),
        ("import subprocess\nsubprocess.run(c)", True),
        ('importlib.metadata.version("x")', False),
        ("urllib.parse.quote(x)", False),
        ("sqlite3.Connection", False),
        ("import urllib.parse", False),
        ("from importlib.metadata import version", False),
        ("from importlib import metadata", False),
        ("import importlib.metadata", False),
        ("import sqlite3", False),
        ("from starlette.concurrency import run_in_threadpool", False),
        ("conn.execute('SELECT 1')", False),
        ("open_connection(path)", False),  # a plain name: the injected connection factory
        ("x = 'socket subprocess importlib'  # socket", False),
    ],
)
def test_find_violations_counts(source: str, reported: bool) -> None:
    assert bool(find_violations(source)) is reported, find_violations(source)


def test_the_api_package_has_no_violations() -> None:
    files = sorted(API_DIR.rglob("*.py"))
    assert len(files) >= 7  # __init__, app, candle_reader, cli, database, errors, params
    for path in files:
        assert find_violations(path.read_text()) == [], path.name


OI_RULE = OpenInterestRule(cadence=1, expected_after_ms=500_000, lag_threshold_ms=60_000, period=Timeframe("5m"))
OI_Q = f"venue=binance-usdm&symbol=BTC/USDT:USDT&period=5m&start={S}"


def _exercise(client: TestClient) -> None:
    assert client.get(f"/v1/open-interest?{OI_Q}").status_code == 200  # open interest rows, a conflict, a gap
    assert client.get(f"/v1/open-interest?{OI_Q}&end={S + 8 * TF}&limit=2").status_code == 200  # a cut page
    assert client.get(f"/v1/open-interest?{OI_Q.replace('BTC/USDT:USDT', 'X/Y')}").status_code == 404
    assert client.get(f"/v1/candles?{Q}").status_code == 200  # success
    assert client.get(f"/v1/candles?{Q.replace('BTC/USDT:USDT', 'X/Y')}").status_code == 404  # unknown series
    assert client.get(f"/v1/candles?{Q.replace(str(S), str(S + 100 * TF))}").status_code == 200  # beyond stored
    assert client.get(f"/v1/candles?{Q}&end={S + 8 * TF}").status_code == 200  # a gap window
    assert client.get(f"/v1/coverage?{Q}&end={S + 8 * TF}").status_code == 200  # a coverage window
    assert client.get("/v1/instruments").status_code == 200  # the configured universe
    assert client.get("/health/ingest").status_code == 200  # the S17 fields and the universe block


def test_every_statement_the_api_issues_is_read_only(tmp_path: Path) -> None:
    path = make_database(
        tmp_path / "market.db",
        candles=[S, S + TF, S + 5 * TF],
        conflicts=[S + TF],
        unavailable=[S + 3 * TF],
        open_interest=[S, S + TF, S + 5 * TF],
        open_interest_conflicts=[S + TF],
    )
    log: list[str] = []

    def traced(target: Path) -> sqlite3.Connection:
        conn = open_reader(target)
        conn.set_trace_callback(log.append)  # only appends to a list; it never touches a connection
        return conn

    _exercise(
        TestClient(
            create_app(path, rule=SAMPLE_RULE, universe=SAMPLE_UNIVERSE, open_interest=OI_RULE, open_connection=traced),
            raise_server_exceptions=False,
        )
    )

    assert log
    allowed = ("BEGIN", "SELECT", "PRAGMA", "ROLLBACK")
    for statement in log:
        text = statement.lstrip().upper()
        assert text.startswith(allowed), statement
        if text.startswith("PRAGMA"):
            assert "=" not in text, statement  # no assignment PRAGMA


def test_the_main_database_file_is_unchanged_by_requests(tmp_path: Path) -> None:
    path = make_database(
        tmp_path / "market.db",
        candles=[S, S + TF, S + 5 * TF],
        conflicts=[S + TF],
        open_interest=[S, S + TF, S + 5 * TF],
        open_interest_conflicts=[S + TF],
    )
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    _exercise(
        TestClient(
            create_app(path, rule=SAMPLE_RULE, universe=SAMPLE_UNIVERSE, open_interest=OI_RULE),
            raise_server_exceptions=False,
        )
    )

    assert (
        hashlib.sha256(path.read_bytes()).hexdigest() == before
    )  # -wal/-shm sidecars may appear; the file may not change


def test_a_write_through_the_apps_default_connection_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = make_database(tmp_path / "market.db", candles=[S])
    attempts: list[str] = []

    def writes(conn: sqlite3.Connection, request: Any) -> Any:
        try:
            conn.execute(
                "INSERT INTO candles (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume,"
                " turnover, ingested_at_ms) VALUES ('binance-usdm', 'X/Y', '5m', ?, '1', '1', '1', '1', '1', NULL, 0)",
                (S,),
            )
        except sqlite3.Error as error:
            attempts.append(str(error))
            raise
        raise AssertionError("the write was accepted")

    monkeypatch.setattr("quasar_market_data.api.database.read_candles_page", writes)

    response = TestClient(
        create_app(path, rule=SAMPLE_RULE, universe=SAMPLE_UNIVERSE), raise_server_exceptions=False
    ).get(f"/v1/candles?{Q}")

    assert attempts
    assert "readonly" in attempts[0].lower()
    assert response.status_code == 500  # a plain SQLITE_READONLY is a write attempt: a bug, not an environment failure
    assert "readonly" not in response.text.lower()
    check = sqlite3.connect(path)
    try:
        assert check.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 1  # the table is unchanged
    finally:
        check.close()
