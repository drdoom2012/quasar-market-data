import sqlite3

import pytest

from tests.storage.test_schema_v1 import BAD_DECIMALS

ROW = ("binance-usdm", "BTC/USDT:USDT", "5m", 1793620800000, "95677.779", "7446123150.675", 1)
INSERT_ROW = "INSERT INTO open_interest VALUES (?, ?, ?, ?, ?, ?, ?)"
CONFLICT_COLUMNS = "venue, symbol, period, time_ms, open_interest, open_interest_value, first_seen_at_ms"
INSERT_CONFLICT = f"INSERT INTO open_interest_conflicts ({CONFLICT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)"
CONFLICT = (*ROW[:4], "1", "2", 1)
VALUE_SLOTS = [
    pytest.param(INSERT_ROW, ROW, 4, id="oi.open_interest"),
    pytest.param(INSERT_ROW, ROW, 5, id="oi.open_interest_value"),
    pytest.param(INSERT_CONFLICT, CONFLICT, 4, id="conflict.open_interest"),
    pytest.param(INSERT_CONFLICT, CONFLICT, 5, id="conflict.open_interest_value"),
]
V1_OBJECTS = {
    ("table", "candles"),
    ("table", "candle_conflicts"),
    ("table", "unavailable_candles"),
    ("table", "schema_migrations"),
    ("index", "candle_conflicts_observation"),
    ("index", "candle_conflicts_slot"),
    ("trigger", "candles_no_update"),
    ("trigger", "candles_no_delete"),
    ("trigger", "candle_conflicts_no_update"),
    ("trigger", "candle_conflicts_no_delete"),
    ("trigger", "unavailable_candles_no_update"),
    ("trigger", "unavailable_candles_no_delete"),
}


def with_value(row: tuple[object, ...], index: int, value: object) -> tuple[object, ...]:
    changed = list(row)
    changed[index] = value
    return tuple(changed)


def test_schema_objects_are_exactly_the_approved_set(schema_v2_conn: sqlite3.Connection) -> None:
    found = set(schema_v2_conn.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
    assert found == V1_OBJECTS | {
        ("table", "open_interest"),
        ("table", "open_interest_conflicts"),
        ("index", "open_interest_conflicts_observation"),
        ("index", "open_interest_conflicts_slot"),
        ("trigger", "open_interest_no_update"),
        ("trigger", "open_interest_no_delete"),
        ("trigger", "open_interest_conflicts_no_update"),
        ("trigger", "open_interest_conflicts_no_delete"),
    }


def test_open_interest_round_trips_exact_text(schema_v2_conn: sqlite3.Connection) -> None:
    schema_v2_conn.execute(INSERT_ROW, ROW)
    assert schema_v2_conn.execute("SELECT * FROM open_interest").fetchall() == [ROW]


@pytest.mark.parametrize(("statement", "row", "index"), VALUE_SLOTS)
@pytest.mark.parametrize("value", ["0", "100", "0.5"])
def test_value_columns_accept_canonical_text(
    schema_v2_conn: sqlite3.Connection, statement: str, row: tuple[object, ...], index: int, value: str
) -> None:
    schema_v2_conn.execute(statement, with_value(row, index, value))


@pytest.mark.parametrize(("statement", "row", "index"), VALUE_SLOTS)
@pytest.mark.parametrize(
    "bad", [*BAD_DECIMALS.values(), 1e20, -1], ids=[*BAD_DECIMALS.keys(), "bound_1e20", "bound_-1"]
)
def test_value_columns_reject_non_canonical_text(
    schema_v2_conn: sqlite3.Connection, statement: str, row: tuple[object, ...], index: int, bad: object
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        schema_v2_conn.execute(statement, with_value(row, index, bad))


def test_bound_number_is_coerced_not_rejected(schema_v2_conn: sqlite3.Connection) -> None:
    # Like candles (ADR-0002 E-3): a bound number becomes text before the CHECK; the writer's types guard against it.
    schema_v2_conn.execute(INSERT_ROW, with_value(ROW, 4, 5))
    assert schema_v2_conn.execute("SELECT open_interest FROM open_interest").fetchall() == [("5",)]


@pytest.mark.parametrize(
    ("index", "value"),
    [(3, 946_684_799_999), (3, 4_102_444_800_001), (2, "5"), (2, "5min"), (0, "")],
    ids=["time_too_early", "time_too_late", "period_short", "period_long", "venue_empty"],
)
def test_key_checks(schema_v2_conn: sqlite3.Connection, index: int, value: object) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        schema_v2_conn.execute(INSERT_ROW, with_value(ROW, index, value))


@pytest.mark.parametrize(
    ("statement", "args"),
    [
        ("UPDATE open_interest SET open_interest = '1'", ()),
        ("DELETE FROM open_interest", ()),
        (INSERT_ROW.replace("INSERT", "INSERT OR REPLACE"), with_value(ROW, 4, "1")),
        (INSERT_ROW + " ON CONFLICT DO UPDATE SET open_interest = excluded.open_interest", with_value(ROW, 4, "1")),
    ],
    ids=["update", "delete", "insert_or_replace", "upsert_do_update"],
)
def test_open_interest_is_immutable(
    schema_v2_conn: sqlite3.Connection, statement: str, args: tuple[object, ...]
) -> None:
    schema_v2_conn.execute(INSERT_ROW, ROW)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        schema_v2_conn.execute(statement, args)
    assert schema_v2_conn.execute("SELECT * FROM open_interest").fetchall() == [ROW]


@pytest.mark.parametrize(
    ("statement", "args"),
    [
        ("UPDATE open_interest_conflicts SET open_interest = '9'", ()),
        ("DELETE FROM open_interest_conflicts", ()),
        (INSERT_CONFLICT.replace("INSERT", "INSERT OR REPLACE"), with_value(CONFLICT, 6, 2)),
        (
            INSERT_CONFLICT + " ON CONFLICT DO UPDATE SET first_seen_at_ms = excluded.first_seen_at_ms",
            with_value(CONFLICT, 6, 2),
        ),
    ],
    ids=["update", "delete", "insert_or_replace", "upsert_do_update"],
)
def test_open_interest_conflicts_are_immutable(
    schema_v2_conn: sqlite3.Connection, statement: str, args: tuple[object, ...]
) -> None:
    schema_v2_conn.execute(INSERT_CONFLICT, CONFLICT)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        schema_v2_conn.execute(statement, args)
    assert schema_v2_conn.execute(f"SELECT {CONFLICT_COLUMNS} FROM open_interest_conflicts").fetchall() == [CONFLICT]


def test_insert_or_replace_overwrites_without_recursive_triggers(schema_v2_conn: sqlite3.Connection) -> None:
    # Control for ADR-0002 E-2: this is why the writer factory must set recursive_triggers=ON.
    schema_v2_conn.execute(INSERT_ROW, ROW)
    schema_v2_conn.execute("PRAGMA recursive_triggers=OFF")
    schema_v2_conn.execute(INSERT_ROW.replace("INSERT", "INSERT OR REPLACE"), with_value(ROW, 4, "1"))
    assert schema_v2_conn.execute("SELECT open_interest FROM open_interest").fetchall() == [("1",)]


def test_conflict_observation_is_unique(schema_v2_conn: sqlite3.Connection) -> None:
    do_nothing = INSERT_CONFLICT + " ON CONFLICT DO NOTHING"
    schema_v2_conn.execute(do_nothing, CONFLICT)
    assert schema_v2_conn.execute(do_nothing, with_value(CONFLICT, 6, 2)).rowcount == 0
    schema_v2_conn.execute(do_nothing, with_value(CONFLICT, 5, "3"))
    assert schema_v2_conn.execute("SELECT count(*) FROM open_interest_conflicts").fetchone() == (2,)
