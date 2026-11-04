import itertools
import sqlite3

import pytest

from quasar_market_data.domain.decimal_text import InvalidDecimalError, canonical_decimal
from tests.storage.test_schema_v2_open_interest import V1_OBJECTS

VENUE = "binance-usdm"
SYMBOL = "BTC/USDT:USDT"
TIME_MS = 1_793_836_800_003
ROW = (VENUE, SYMBOL, TIME_MS, "Regular", "-0.0001", "67123.45", 1)
INSERT_ROW = "INSERT INTO funding_rates VALUES (?, ?, ?, ?, ?, ?, ?)"
CONFLICT_COLUMNS = "venue, symbol, funding_time_ms, rate_type, funding_rate, mark_price, first_seen_at_ms"
INSERT_CONFLICT = f"INSERT INTO funding_rate_conflicts ({CONFLICT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)"
CONFLICT = (VENUE, SYMBOL, TIME_MS, "Regular", "0.0002", "67123.45", 1)
INSERT_SPAN = "INSERT INTO funding_rate_verified VALUES (?, ?, ?, ?, ?)"
SPAN = (VENUE, SYMBOL, 1_000_000_000_000, 1_000_000_001_000, 5)
V2_OBJECTS = V1_OBJECTS | {
    ("table", "open_interest"),
    ("table", "open_interest_conflicts"),
    ("index", "open_interest_conflicts_observation"),
    ("index", "open_interest_conflicts_slot"),
    ("trigger", "open_interest_no_update"),
    ("trigger", "open_interest_no_delete"),
    ("trigger", "open_interest_conflicts_no_update"),
    ("trigger", "open_interest_conflicts_no_delete"),
}
# (statement, row, index of the funding_rate column, id): the signed check applies to both tables.
RATE_SLOTS = [
    pytest.param(INSERT_ROW, ROW, 4, id="rates.funding_rate"),
    pytest.param(INSERT_CONFLICT, CONFLICT, 4, id="conflict.funding_rate"),
]
PRICE_SLOTS = [
    pytest.param(INSERT_ROW, ROW, 5, id="rates.mark_price"),
    pytest.param(INSERT_CONFLICT, CONFLICT, 5, id="conflict.mark_price"),
]


def with_value(row: tuple[object, ...], index: int, value: object) -> tuple[object, ...]:
    changed = list(row)
    changed[index] = value
    return tuple(changed)


def sql_accepts(conn: sqlite3.Connection, value: object) -> bool:
    """Whether ``funding_rates`` takes ``value`` as ``funding_rate`` (the row is rolled back either way)."""
    conn.execute("SAVEPOINT probe")
    try:
        conn.execute(INSERT_ROW, with_value(ROW, 4, value))
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.execute("ROLLBACK TO probe")
        conn.execute("RELEASE probe")
    return True


def test_schema_objects_are_exactly_the_approved_set(schema_v3_conn: sqlite3.Connection) -> None:
    found = set(schema_v3_conn.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
    assert found == V2_OBJECTS | {
        ("table", "funding_rates"),
        ("table", "funding_rate_conflicts"),
        ("table", "funding_rate_verified"),
        ("index", "funding_rate_conflicts_observation"),
        ("index", "funding_rate_conflicts_event"),
        ("trigger", "funding_rates_no_update"),
        ("trigger", "funding_rates_no_delete"),
        ("trigger", "funding_rate_conflicts_no_update"),
        ("trigger", "funding_rate_conflicts_no_delete"),
        ("trigger", "funding_rate_verified_only_grows"),
        ("trigger", "funding_rate_verified_no_delete"),
    }


def test_round_trip_keeps_exact_text(schema_v3_conn: sqlite3.Connection) -> None:
    schema_v3_conn.execute(INSERT_ROW, ROW)
    no_price = with_value(with_value(ROW, 3, "Special"), 5, None)
    schema_v3_conn.execute(INSERT_ROW, no_price)
    rows = schema_v3_conn.execute("SELECT * FROM funding_rates ORDER BY rate_type").fetchall()
    assert rows == [ROW, no_price]


@pytest.mark.parametrize(("statement", "row", "index"), RATE_SLOTS)
@pytest.mark.parametrize("value", ["-0.0001", "0", "0.5", "12", "-12.5"])
def test_signed_check_accepts(
    schema_v3_conn: sqlite3.Connection, statement: str, row: tuple[object, ...], index: int, value: str
) -> None:
    schema_v3_conn.execute(statement, with_value(row, index, value))


@pytest.mark.parametrize(("statement", "row", "index"), RATE_SLOTS)
@pytest.mark.parametrize(
    "bad",
    [
        "-0", "-", "+1", "--1", "-+1", "1.50", "-1.50", "00.1", "-00.1", ".5", "5.", "-.5", "-5.", "-0.0",
        "1e-4", " 1", "- 1", "", 1e20,
    ],
)  # fmt: skip
def test_signed_check_refuses(
    schema_v3_conn: sqlite3.Connection, statement: str, row: tuple[object, ...], index: int, bad: object
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        schema_v3_conn.execute(statement, with_value(row, index, bad))


@pytest.mark.parametrize(("statement", "row", "index"), RATE_SLOTS + PRICE_SLOTS)
@pytest.mark.parametrize(("number", "text"), [(5, "5"), (0.0001, "0.0001")])
def test_bound_number_is_coerced_not_rejected(
    schema_v3_conn: sqlite3.Connection, statement: str, row: tuple[object, ...], index: int, number: float, text: str
) -> None:
    # A STRICT TEXT column turns a bound number into text before the CHECK runs (as in v1 and v2, INV-026): the
    # float guard is the FundingRate value object and the writer's type check, not the schema.
    table = statement.split()[2]
    column = "funding_rate" if index == 4 else "mark_price"
    schema_v3_conn.execute(statement, with_value(row, index, number))
    assert schema_v3_conn.execute(f"SELECT {column} FROM {table}").fetchall() == [(text,)]


def test_bound_negative_integer_is_stored_as_text_in_funding_rate(schema_v3_conn: sqlite3.Connection) -> None:
    schema_v3_conn.execute(INSERT_ROW, with_value(ROW, 4, -1))
    assert schema_v3_conn.execute("SELECT funding_rate FROM funding_rates").fetchall() == [("-1",)]


def test_signed_check_agrees_with_canonical_decimal(schema_v3_conn: sqlite3.Connection) -> None:
    corpus = {"".join(chars) for length in range(1, 5) for chars in itertools.product("-+.0159e ", repeat=length)}
    corpus |= {"-0.0001", "0", "-0", "-", "1.50", "00.1", "- 1", "1e-4", "-" + "1" * 96, "1" * 48 + "." + "1" * 48}
    assert len(corpus) > 7_000
    for text in sorted(corpus):
        try:
            expected = canonical_decimal(text) == text
        except InvalidDecimalError:
            expected = False
        assert sql_accepts(schema_v3_conn, text) is expected, text


def test_signed_check_is_a_superset_of_canonical_decimal_only_at_the_97_digit_boundary(
    schema_v3_conn: sqlite3.Connection,
) -> None:
    longest = "-" + "1" * 96
    assert canonical_decimal(longest) == longest
    assert sql_accepts(schema_v3_conn, longest)
    too_long = "1" * 97  # canonical_decimal allows 96 mantissa digits; the SQL check allows 97 characters (as v1)
    with pytest.raises(InvalidDecimalError):
        canonical_decimal(too_long)
    assert sql_accepts(schema_v3_conn, too_long)
    assert not sql_accepts(schema_v3_conn, "1" * 98)
    assert not sql_accepts(schema_v3_conn, "-" + "1" * 98)


@pytest.mark.parametrize(("statement", "row", "index"), PRICE_SLOTS)
@pytest.mark.parametrize("value", ["67123.45", "0"])
def test_mark_price_accepts_the_non_negative_shape(
    schema_v3_conn: sqlite3.Connection, statement: str, row: tuple[object, ...], index: int, value: str
) -> None:
    schema_v3_conn.execute(statement, with_value(row, index, value))


@pytest.mark.parametrize(("statement", "row", "index"), PRICE_SLOTS)
@pytest.mark.parametrize("bad", ["-1", "+1", "1.50", "", "-0", "00.1", "1e-4"])
def test_mark_price_refuses_everything_else(
    schema_v3_conn: sqlite3.Connection, statement: str, row: tuple[object, ...], index: int, bad: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        schema_v3_conn.execute(statement, with_value(row, index, bad))


@pytest.mark.parametrize(("statement", "row", "index"), PRICE_SLOTS)
def test_mark_price_accepts_null(
    schema_v3_conn: sqlite3.Connection, statement: str, row: tuple[object, ...], index: int
) -> None:
    schema_v3_conn.execute(statement, with_value(row, index, None))


@pytest.mark.parametrize(
    ("index", "value"),
    [
        (2, 946_684_799_999),
        (2, 4_102_444_800_001),
        (3, "regular"),
        (3, "Other"),
        (3, ""),
        (0, ""),
        (0, "v" * 33),
        (1, ""),
        (1, "s" * 65),
    ],
)
def test_key_checks(schema_v3_conn: sqlite3.Connection, index: int, value: object) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        schema_v3_conn.execute(INSERT_ROW, with_value(ROW, index, value))


@pytest.mark.parametrize(
    ("statement", "args"),
    [
        ("UPDATE funding_rates SET funding_rate = '1'", ()),
        ("DELETE FROM funding_rates", ()),
        (INSERT_ROW.replace("INSERT", "INSERT OR REPLACE"), with_value(ROW, 4, "1")),
        (INSERT_ROW + " ON CONFLICT DO UPDATE SET funding_rate = excluded.funding_rate", with_value(ROW, 4, "1")),
    ],
    ids=["update", "delete", "insert_or_replace", "upsert_do_update"],
)
def test_funding_rates_are_immutable(
    schema_v3_conn: sqlite3.Connection, statement: str, args: tuple[object, ...]
) -> None:
    schema_v3_conn.execute(INSERT_ROW, ROW)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        schema_v3_conn.execute(statement, args)
    assert schema_v3_conn.execute("SELECT * FROM funding_rates").fetchall() == [ROW]


@pytest.mark.parametrize(
    ("statement", "args"),
    [
        ("UPDATE funding_rate_conflicts SET funding_rate = '9'", ()),
        ("DELETE FROM funding_rate_conflicts", ()),
        (INSERT_CONFLICT.replace("INSERT", "INSERT OR REPLACE"), with_value(CONFLICT, 6, 2)),
        (
            INSERT_CONFLICT + " ON CONFLICT DO UPDATE SET first_seen_at_ms = excluded.first_seen_at_ms",
            with_value(CONFLICT, 6, 2),
        ),
    ],
    ids=["update", "delete", "insert_or_replace", "upsert_do_update"],
)
def test_funding_rate_conflicts_are_immutable(
    schema_v3_conn: sqlite3.Connection, statement: str, args: tuple[object, ...]
) -> None:
    schema_v3_conn.execute(INSERT_CONFLICT, CONFLICT)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        schema_v3_conn.execute(statement, args)
    assert schema_v3_conn.execute(f"SELECT {CONFLICT_COLUMNS} FROM funding_rate_conflicts").fetchall() == [CONFLICT]


def test_insert_or_replace_overwrites_without_recursive_triggers(schema_v3_conn: sqlite3.Connection) -> None:
    # Control: shows why the writer connection must run with recursive_triggers=ON (the pragma is not in the schema).
    schema_v3_conn.execute(INSERT_ROW, ROW)
    schema_v3_conn.execute("PRAGMA recursive_triggers=OFF")
    schema_v3_conn.execute(INSERT_ROW.replace("INSERT", "INSERT OR REPLACE"), with_value(ROW, 4, "1"))
    assert schema_v3_conn.execute("SELECT funding_rate FROM funding_rates").fetchall() == [("1",)]


def test_conflict_observation_is_unique_with_a_null_mark_price(schema_v3_conn: sqlite3.Connection) -> None:
    do_nothing = INSERT_CONFLICT + " ON CONFLICT DO NOTHING"
    null_price = with_value(CONFLICT, 5, None)
    assert schema_v3_conn.execute(do_nothing, null_price).rowcount == 1
    assert schema_v3_conn.execute(do_nothing, with_value(null_price, 6, 2)).rowcount == 0  # the COALESCE index
    assert schema_v3_conn.execute(do_nothing, CONFLICT).rowcount == 1  # same rate, a price: another observation
    assert schema_v3_conn.execute(do_nothing, with_value(CONFLICT, 6, 3)).rowcount == 0
    assert schema_v3_conn.execute(do_nothing, with_value(CONFLICT, 3, "Special")).rowcount == 1
    assert schema_v3_conn.execute("SELECT count(*) FROM funding_rate_conflicts").fetchone() == (3,)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE funding_rate_verified SET through_ms = 1000000000999",
        "UPDATE funding_rate_verified SET from_ms = 1000000000001",
        "UPDATE funding_rate_verified SET verified_at_ms = 4",
        "UPDATE funding_rate_verified SET venue = 'other'",
        "UPDATE funding_rate_verified SET symbol = 'ETH/USDT:USDT'",
        "DELETE FROM funding_rate_verified",
    ],
    ids=["through_lowered", "from_raised", "verified_at_lowered", "venue_changed", "symbol_changed", "delete"],
)
def test_span_trigger_refuses_every_shrink_and_rekey(schema_v3_conn: sqlite3.Connection, statement: str) -> None:
    schema_v3_conn.execute(INSERT_SPAN, SPAN)
    with pytest.raises(sqlite3.IntegrityError, match="only grows|not deletable"):
        schema_v3_conn.execute(statement)
    assert schema_v3_conn.execute("SELECT * FROM funding_rate_verified").fetchall() == [SPAN]


SHRINKING = (VENUE, SYMBOL, 1_000_000_000_500, 1_000_000_000_600, 6)


@pytest.mark.parametrize(
    "statement",
    [
        INSERT_SPAN.replace("INSERT", "INSERT OR REPLACE"),
        INSERT_SPAN + " ON CONFLICT DO UPDATE SET from_ms = excluded.from_ms, through_ms = excluded.through_ms",
    ],
    ids=["insert_or_replace", "upsert_do_update"],
)
def test_span_trigger_refuses_a_replace_or_upsert_that_shrinks(
    schema_v3_conn: sqlite3.Connection, statement: str
) -> None:
    schema_v3_conn.execute(INSERT_SPAN, SPAN)
    with pytest.raises(sqlite3.IntegrityError, match="only grows|not deletable"):
        schema_v3_conn.execute(statement, SHRINKING)
    assert schema_v3_conn.execute("SELECT * FROM funding_rate_verified").fetchall() == [SPAN]


def test_span_replace_shrinks_without_recursive_triggers(schema_v3_conn: sqlite3.Connection) -> None:
    # Control: without recursive_triggers a REPLACE deletes and re-inserts without firing the delete trigger.
    schema_v3_conn.execute(INSERT_SPAN, SPAN)
    schema_v3_conn.execute("PRAGMA recursive_triggers=OFF")
    schema_v3_conn.execute(INSERT_SPAN.replace("INSERT", "INSERT OR REPLACE"), SHRINKING)
    assert schema_v3_conn.execute("SELECT * FROM funding_rate_verified").fetchall() == [SHRINKING]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE funding_rate_verified SET from_ms = 999999999999",
        "UPDATE funding_rate_verified SET through_ms = 1000000009999",
        "UPDATE funding_rate_verified SET verified_at_ms = 6",
        "UPDATE funding_rate_verified SET from_ms = 999999999999, through_ms = 1000000009999, verified_at_ms = 9",
        "UPDATE funding_rate_verified SET from_ms = from_ms",
    ],
    ids=["from_lowered", "through_raised", "verified_at_raised", "all_three", "nothing_changes"],
)
def test_span_trigger_accepts_growth(schema_v3_conn: sqlite3.Connection, statement: str) -> None:
    schema_v3_conn.execute(INSERT_SPAN, SPAN)
    schema_v3_conn.execute(statement)


@pytest.mark.parametrize(
    "span",
    [
        (VENUE, SYMBOL, 1_000_000_000_001, 1_000_000_000_000, 1),
        (VENUE, SYMBOL, 946_684_799_999, 1_000_000_000_000, 1),
        (VENUE, SYMBOL, 1_000_000_000_000, 4_102_444_800_001, 1),
        ("", SYMBOL, 1_000_000_000_000, 1_000_000_000_001, 1),
    ],
    ids=["through_below_from", "from_out_of_range", "through_out_of_range", "empty_venue"],
)
def test_span_checks(schema_v3_conn: sqlite3.Connection, span: tuple[object, ...]) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        schema_v3_conn.execute(INSERT_SPAN, span)


def test_the_schema_accepts_a_non_adjacent_hand_written_growth(schema_v3_conn: sqlite3.Connection) -> None:
    # Negative control (ADR-0004 P-6): triggers see only that the span grows, not whether it grows adjacent to
    # verified data. Adjacency is enforced by the single merge statement of the writer, not by the schema.
    schema_v3_conn.execute(INSERT_SPAN, SPAN)
    schema_v3_conn.execute("UPDATE funding_rate_verified SET through_ms = through_ms + 315360000000")
    assert schema_v3_conn.execute("SELECT through_ms FROM funding_rate_verified").fetchone() == (1_315_360_001_000,)
