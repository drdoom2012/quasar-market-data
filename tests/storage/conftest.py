import sqlite3
from collections.abc import Iterator
from importlib import resources
from pathlib import Path

import pytest

from quasar_market_data.storage.connection import open_writer


def schema_v1_sql() -> str:
    return resources.files("quasar_market_data.storage.migrations").joinpath("001_schema_v1.sql").read_text("utf-8")


@pytest.fixture
def schema_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A writer connection on a fresh database with schema v1 applied directly (not through the runner)."""
    conn = open_writer(tmp_path / "market.db")
    conn.executescript(schema_v1_sql())
    yield conn
    conn.close()
