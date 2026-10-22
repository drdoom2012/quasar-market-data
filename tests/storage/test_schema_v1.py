import sqlite3

import pytest

CANDLE = (
    "binance-usdm",
    "BTC/USDT:USDT",
    "5m",
    1792676700000,
    "81700.8",
    "81700.8",
    "81454.7",
    "81555.9",
    "2123.997",
    "173139160.7713",
    1,
)
INSERT_CANDLE = "INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
COLUMN_INDEX = {"open": 4, "high": 5, "low": 6, "close": 7, "volume": 8, "turnover": 9}
VALUE_COLUMNS = tuple(COLUMN_INDEX)
BAD_DECIMALS = {
    "exponent": "1e5",
    "negative": "-1",
    "trailing_zero": "1.50",
    "leading_zero": "01",
    "trailing_point": "1.",
    "leading_point": ".5",
    "two_points": "1.2.3",
    "space": "1 ",
    "empty": "",
}
SLOT = ("binance-usdm", "BTC/USDT:USDT", "5m", 1792676700000)
CONFLICT_COLUMNS = "venue, symbol, timeframe, open_time_ms, open, high, low, close, volume, turnover, first_seen_at_ms"
INSERT_CONFLICT = (
    f"INSERT INTO candle_conflicts ({CONFLICT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING"
)


def candle_with(**overrides: object) -> tuple[object, ...]:
    row = list(CANDLE)
    for column, value in overrides.items():
        row[COLUMN_INDEX[column]] = value
    return tuple(row)


def conflict_row(close: str = "9", turnover: str | None = None, first_seen_at_ms: int = 1) -> tuple[object, ...]:
    return (*SLOT, "1", "2", "1", close, "3", turnover, first_seen_at_ms)


def test_schema_objects_are_exactly_the_approved_set(schema_conn: sqlite3.Connection) -> None:
    found = {
        (kind, name)
        for kind, name in schema_conn.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
    }
    assert found == {
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


def test_candle_round_trips_exact_text(schema_conn: sqlite3.Connection) -> None:
    schema_conn.execute(INSERT_CANDLE, CANDLE)
    schema_conn.execute(INSERT_CANDLE, (*SLOT[:3], SLOT[3] + 300_000, *CANDLE[4:9], None, 2))
    rows = schema_conn.execute("SELECT * FROM candles ORDER BY open_time_ms").fetchall()
    assert rows[0] == CANDLE
    assert rows[1][9] is None


@pytest.mark.parametrize("column", VALUE_COLUMNS)
@pytest.mark.parametrize("bad", BAD_DECIMALS.values(), ids=BAD_DECIMALS.keys())
def test_value_columns_reject_non_canonical_decimal_text(
    schema_conn: sqlite3.Connection, column: str, bad: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(INSERT_CANDLE, candle_with(**{column: bad}))
    assert schema_conn.execute("SELECT count(*) FROM candles").fetchone() == (0,)


@pytest.mark.parametrize("value", ["0", "100", "0.5"])
def test_value_columns_accept_canonical_decimal_text(schema_conn: sqlite3.Connection, value: str) -> None:
    schema_conn.execute(INSERT_CANDLE, candle_with(open=value, high=value, low=value, close=value, volume=value))


def test_only_turnover_may_be_null(schema_conn: sqlite3.Connection) -> None:
    schema_conn.execute(INSERT_CANDLE, candle_with(turnover=None))
    for column in ("open", "high", "low", "close", "volume"):
        with pytest.raises(sqlite3.IntegrityError):
            schema_conn.execute(INSERT_CANDLE, (*SLOT[:3], SLOT[3] + 300_000, *candle_with(**{column: None})[4:]))


@pytest.mark.parametrize("open_time", [946_684_799_999, 4_102_444_800_001, "abc"])
def test_open_time_must_be_an_integer_in_range(schema_conn: sqlite3.Connection, open_time: object) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(INSERT_CANDLE, (*CANDLE[:3], open_time, *CANDLE[4:]))


@pytest.mark.parametrize("open_time", [946_684_800_000, 4_102_444_800_000])
def test_open_time_bounds_are_accepted(schema_conn: sqlite3.Connection, open_time: int) -> None:
    schema_conn.execute(INSERT_CANDLE, (*CANDLE[:3], open_time, *CANDLE[4:]))


def test_duplicate_identity_is_rejected_and_do_nothing_keeps_the_row(schema_conn: sqlite3.Connection) -> None:
    schema_conn.execute(INSERT_CANDLE, CANDLE)
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(INSERT_CANDLE, candle_with(close="1"))

    cursor = schema_conn.execute(INSERT_CANDLE + " ON CONFLICT DO NOTHING", candle_with(close="1"))
    assert cursor.rowcount == 0
    assert schema_conn.execute("SELECT close FROM candles").fetchall() == [("81555.9",)]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE candles SET close = '1'",
        "DELETE FROM candles",
        INSERT_CANDLE.replace("INSERT", "INSERT OR REPLACE"),
        INSERT_CANDLE + " ON CONFLICT DO UPDATE SET close = excluded.close",
    ],
    ids=["update", "delete", "insert_or_replace", "upsert_do_update"],
)
def test_candles_are_immutable(schema_conn: sqlite3.Connection, statement: str) -> None:
    schema_conn.execute(INSERT_CANDLE, CANDLE)
    args = candle_with(close="1") if statement.startswith("INSERT") else ()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        schema_conn.execute(statement, args)
    assert schema_conn.execute("SELECT * FROM candles").fetchall() == [CANDLE]


def test_insert_or_replace_overwrites_without_recursive_triggers(schema_conn: sqlite3.Connection) -> None:
    # Control for ADR-0002 E-2: this is why the writer factory must set recursive_triggers=ON.
    schema_conn.execute(INSERT_CANDLE, CANDLE)
    schema_conn.execute("PRAGMA recursive_triggers=OFF")
    schema_conn.execute(INSERT_CANDLE.replace("INSERT", "INSERT OR REPLACE"), candle_with(close="1"))
    assert schema_conn.execute("SELECT close FROM candles").fetchall() == [("1",)]


def test_bound_float_is_coerced_not_rejected(schema_conn: sqlite3.Connection) -> None:
    # Documents the limit in ADR-0002 E-3: SQLite turns the float into text before the CHECK runs,
    # so the guard against floats lives in the writer's types, not in the schema.
    schema_conn.execute(INSERT_CANDLE, candle_with(close=0.5))
    assert schema_conn.execute("SELECT close FROM candles").fetchall() == [("0.5",)]


def test_conflict_recording_is_idempotent(schema_conn: sqlite3.Connection) -> None:
    for turnover in (None, "5"):
        schema_conn.execute(INSERT_CONFLICT, conflict_row(turnover=turnover, first_seen_at_ms=1))
        schema_conn.execute(INSERT_CONFLICT, conflict_row(turnover=turnover, first_seen_at_ms=2))
    assert schema_conn.execute(
        "SELECT turnover, first_seen_at_ms FROM candle_conflicts ORDER BY conflict_id"
    ).fetchall() == [
        (None, 1),
        ("5", 1),
    ]

    schema_conn.execute(INSERT_CONFLICT, conflict_row(close="8", first_seen_at_ms=3))
    assert schema_conn.execute("SELECT count(*) FROM candle_conflicts").fetchone() == (3,)


def test_insert_or_ignore_would_swallow_an_invalid_conflict_record(schema_conn: sqlite3.Connection) -> None:
    # Control: OR IGNORE also ignores CHECK violations, so a malformed record would vanish silently.
    # Recording must use ON CONFLICT DO NOTHING, which only ignores uniqueness conflicts.
    statement = INSERT_CONFLICT.replace("INSERT INTO", "INSERT OR IGNORE INTO").replace(" ON CONFLICT DO NOTHING", "")
    schema_conn.execute(statement, conflict_row(close="1e5"))
    assert schema_conn.execute("SELECT count(*) FROM candle_conflicts").fetchone() == (0,)


def test_conflict_values_use_the_decimal_shape(schema_conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(INSERT_CONFLICT, conflict_row(close="1e5"))


@pytest.mark.parametrize("statement", ["UPDATE candle_conflicts SET close = '1'", "DELETE FROM candle_conflicts"])
def test_conflict_rows_are_immutable(schema_conn: sqlite3.Connection, statement: str) -> None:
    schema_conn.execute(INSERT_CONFLICT, conflict_row())
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        schema_conn.execute(statement)


INSERT_UNAVAILABLE = "INSERT INTO unavailable_candles VALUES (?, ?, ?, ?, ?, ?)"


def test_unavailable_reason_is_a_closed_enum(schema_conn: sqlite3.Connection) -> None:
    schema_conn.execute(INSERT_UNAVAILABLE, (*SLOT, "EXCHANGE_NO_DATA", 1))
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(INSERT_UNAVAILABLE, (*SLOT[:3], SLOT[3] + 300_000, "OTHER", 1))


def test_unavailable_slot_is_unique(schema_conn: sqlite3.Connection) -> None:
    schema_conn.execute(INSERT_UNAVAILABLE, (*SLOT, "EXCHANGE_NO_DATA", 1))
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(INSERT_UNAVAILABLE, (*SLOT, "EXCHANGE_NO_DATA", 2))


@pytest.mark.parametrize(
    "statement", ["UPDATE unavailable_candles SET recorded_at_ms = 2", "DELETE FROM unavailable_candles"]
)
def test_unavailable_rows_are_immutable(schema_conn: sqlite3.Connection, statement: str) -> None:
    schema_conn.execute(INSERT_UNAVAILABLE, (*SLOT, "EXCHANGE_NO_DATA", 1))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        schema_conn.execute(statement)


def test_slot_may_hold_a_candle_and_an_unavailable_row(schema_conn: sqlite3.Connection) -> None:
    # Documents ADR-0002 P-6: no cross-table prevention; coverage reports such a slot as conflicted (S11).
    schema_conn.execute(INSERT_CANDLE, CANDLE)
    schema_conn.execute(INSERT_UNAVAILABLE, (*SLOT, "EXCHANGE_NO_DATA", 1))
    assert schema_conn.execute("SELECT count(*) FROM unavailable_candles").fetchone() == (1,)
