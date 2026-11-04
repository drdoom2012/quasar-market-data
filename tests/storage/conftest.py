import sqlite3
from collections.abc import Iterator
from importlib import resources
from pathlib import Path

import pytest

from quasar_market_data.storage.connection import open_writer


def schema_v1_sql() -> str:
    return resources.files("quasar_market_data.storage.migrations").joinpath("001_schema_v1.sql").read_text("utf-8")


def schema_sql(version: int) -> str:
    """The shipped migration files ``1..version`` joined, in order."""
    directory = resources.files("quasar_market_data.storage.migrations")
    files = sorted(entry.name for entry in directory.iterdir() if entry.name.endswith(".sql"))
    return "\n".join(directory.joinpath(name).read_text("utf-8") for name in files[:version])


@pytest.fixture
def schema_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A writer connection on a fresh database with schema v1 applied directly (not through the runner)."""
    conn = open_writer(tmp_path / "market.db")
    conn.executescript(schema_v1_sql())
    yield conn
    conn.close()


@pytest.fixture
def schema_v2_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A writer connection on a fresh database with migrations 1 and 2 applied directly (not through the runner)."""
    conn = open_writer(tmp_path / "market.db")
    conn.executescript(schema_sql(2))
    yield conn
    conn.close()


@pytest.fixture
def schema_v3_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A writer connection on a fresh database with migrations 1 to 3 applied directly (not through the runner)."""
    conn = open_writer(tmp_path / "market.db")
    conn.executescript(schema_sql(3))
    yield conn
    conn.close()
