"""Contract 4.1 and 6.2.4 (ADR-0005 P-4, P-5): the two row fields on every row endpoint, and the health object."""

from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.domain.readability import READABLE_BOUND_MS
from quasar_market_data.storage.connection import open_writer
from quasar_market_data.storage.migrate import load_migrations, migrate
from tests.api import test_app, test_funding_rate_endpoint, test_open_interest_endpoint
from tests.api.seed import (
    SAMPLE_UNIVERSE,
    SYMBOL,
    VENUE,
    insert_candle,
    insert_funding_rate,
    insert_open_interest,
)
from tests.api.test_health_universe import client as health_client

OLD = 1_792_670_000_000  # stamps of rows stored before the cut-over
AT = 1_792_677_000_000  # the migration to schema 4
CUTOVER = AT + 1
CONFIRMED, UNCONFIRMED = AT + 10, AT + 20
TF = 300_000
C0 = test_app.S  # three candle slots
O0 = test_open_interest_endpoint.S  # three open interest period ends
F0 = 1_792_656_000_000  # three settlements, one hour apart


def database(path: Path) -> Path:
    """One old row per dataset (schema 3, then migrated), then a confirmed and an unconfirmed new row each."""
    conn = open_writer(path)
    try:
        migrate(conn, applied_at_ms=OLD, migrations=load_migrations()[:3])
        conn.execute(
            "INSERT INTO candles VALUES (?, ?, '5m', ?, '1', '2', '1', '1', '1', NULL, ?)", (VENUE, SYMBOL, C0, OLD)
        )
        conn.execute("INSERT INTO open_interest VALUES (?, ?, '5m', ?, '1', '2', ?)", (VENUE, SYMBOL, O0, OLD))
        conn.execute(
            "INSERT INTO funding_rates VALUES (?, ?, ?, 'Regular', '0.0001', NULL, ?)", (VENUE, SYMBOL, F0, OLD)
        )
        migrate(conn, applied_at_ms=AT)
        for n, stamp, confirmed in ((1, CONFIRMED, True), (2, UNCONFIRMED, False)):
            insert_candle(conn, C0 + n * TF, stamp=stamp, confirmed=confirmed)
            insert_open_interest(conn, O0 + n * TF, stamp=stamp, confirmed=confirmed)
            insert_funding_rate(conn, F0 + n * 3_600_000, stamp=stamp, confirmed=confirmed)
    finally:
        conn.close()
    return path


EXPECTED = [
    (CUTOVER, False),  # stored before the cut-over
    (CONFIRMED + READABLE_BOUND_MS, False),
    (UNCONFIRMED + READABLE_BOUND_MS, True),  # its bound does not hold
]


def fields(rows: list[dict[str, Any]]) -> list[tuple[int, bool]]:
    return [(row["readable_by_ms"], row["readable_by_unconfirmed"]) for row in rows]


def test_candle_rows(tmp_path: Path) -> None:
    body = test_app.client_for(database(tmp_path / "m.db")).get(f"/v1/candles?{test_app.Q}&end={C0 + 3 * TF}").json()
    assert fields(body["candles"]) == EXPECTED
    assert [list(c)[-3:] for c in body["candles"]] == [["conflicted", "readable_by_ms", "readable_by_unconfirmed"]] * 3


def test_open_interest_rows(tmp_path: Path) -> None:
    client = test_open_interest_endpoint.client_for(database(tmp_path / "m.db"))
    body = client.get(f"/v1/open-interest?{test_open_interest_endpoint.Q}&end={O0 + 3 * TF}").json()
    assert fields(body["rows"]) == EXPECTED


def test_funding_rows(tmp_path: Path) -> None:
    client = test_funding_rate_endpoint.client_for(database(tmp_path / "m.db"))
    query = f"{test_funding_rate_endpoint.BASE}&start={F0}&end={F0 + 2 * 3_600_000}"
    assert fields(client.get(f"/v1/funding-rates?{query}").json()["rows"]) == EXPECTED


def readability(path: Path, as_of: int) -> dict[str, Any]:
    response = health_client(path, SAMPLE_UNIVERSE, as_of).get("/health/ingest")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert list(body)[-1] == "readability"
    found: dict[str, Any] = body["readability"]
    return found


@pytest.mark.parametrize(
    ("as_of", "counted"),
    [
        (UNCONFIRMED + READABLE_BOUND_MS - 1, False),  # still within the bound: not counted yet
        (UNCONFIRMED + READABLE_BOUND_MS, True),  # inclusive
        (UNCONFIRMED + 86_400_000 - 1, True),
        (UNCONFIRMED + 86_400_000, False),  # the window start is exclusive
    ],
)
def test_the_health_object_counts_unconfirmed_transactions_in_its_window(
    tmp_path: Path, as_of: int, counted: bool
) -> None:
    found = readability(database(tmp_path / "m.db"), as_of)
    one = (
        {"transactions": 1, "newest_stamp_ms": UNCONFIRMED} if counted else {"transactions": 0, "newest_stamp_ms": None}
    )
    assert found == {
        "bound_ms": READABLE_BOUND_MS,
        "cutover_ms": CUTOVER,
        "window_ms": 86_400_000,
        "unconfirmed": {"candles": one, "open_interest": one, "funding_rates": one},
    }


def test_the_health_object_changes_no_status(tmp_path: Path) -> None:
    path = database(tmp_path / "m.db")
    as_of = UNCONFIRMED + READABLE_BOUND_MS
    with_unconfirmed = health_client(path, SAMPLE_UNIVERSE, as_of).get("/health/ingest").json()
    earlier = health_client(path, SAMPLE_UNIVERSE, as_of - 1).get("/health/ingest").json()
    assert with_unconfirmed["readability"]["unconfirmed"]["candles"]["transactions"] == 1
    assert with_unconfirmed["status"] == earlier["status"]
