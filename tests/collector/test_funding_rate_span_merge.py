import ast
import random
import re
import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.collector.funding_rate_writer import (
    MAX_ROWS_PER_TRANSACTION,
    FundingRateWriteError,
    FundingRateWriteOutcomeUnknown,
    FundingRateWriter,
    FundingSpanNotAdjacentError,
    SpanMerge,
)
from quasar_market_data.domain.funding_rate import FundingRate, FundingRateKey, RateType
from quasar_market_data.domain.funding_rate_span import Interval, NotAdjacentError, merge_span
from quasar_market_data.storage.connection import open_reader, open_writer
from quasar_market_data.storage.migrate import migrate
from tests.collector.faulty_connection import Fault, FaultyConnection, open_faulty_writer

AT = 1_793_840_000_000
VENUE = "binance-usdm"
SYMBOL = "BTC/USDT:USDT"
X = 1_793_836_800_000
HOUR = 3_600_000
INSERT_ROW = "INSERT INTO funding_rates ("
ROOT = Path(__file__).resolve().parents[2]


def fr(time_ms: int, symbol: str = SYMBOL, rate_type: RateType = RateType.REGULAR) -> FundingRate:
    return FundingRate(FundingRateKey(VENUE, symbol, time_ms, rate_type), "0.0001", "67123.45")


def merge_of(a: int, e: int, at: int = AT, symbol: str = SYMBOL) -> SpanMerge:
    return SpanMerge(VENUE, symbol, Interval(a, e), at)


def span_of(db: Path, symbol: str = SYMBOL) -> tuple[int, int, int] | None:
    conn = open_reader(db)
    try:
        row = conn.execute(
            "SELECT from_ms, through_ms, verified_at_ms FROM funding_rate_verified WHERE venue = ? AND symbol = ?",
            (VENUE, symbol),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else tuple(row)


def times_of(db: Path) -> list[int]:
    conn = open_reader(db)
    try:
        return [t for (t,) in conn.execute("SELECT funding_time_ms FROM funding_rates ORDER BY funding_time_ms")]
    finally:
        conn.close()


def dump(db: Path) -> dict[str, list[tuple[Any, ...]]]:
    conn = open_reader(db)
    try:
        return {
            "rates": conn.execute(
                "SELECT venue, symbol, funding_time_ms, rate_type, funding_rate, mark_price FROM funding_rates"
                " ORDER BY venue, symbol, funding_time_ms, rate_type"
            ).fetchall(),
            "conflicts": conn.execute("SELECT * FROM funding_rate_conflicts").fetchall(),
            "span": conn.execute("SELECT * FROM funding_rate_verified ORDER BY venue, symbol").fetchall(),
        }
    finally:
        conn.close()


def assert_span_is_backed(conn: sqlite3.Connection, expected_times: Sequence[int], context: str = "") -> None:
    """Every settlement the page contains and the span claims has its row stored."""
    span = conn.execute("SELECT from_ms, through_ms FROM funding_rate_verified").fetchone()
    if span is None:
        return
    stored = {t for (t,) in conn.execute("SELECT funding_time_ms FROM funding_rates")}
    missing = [t for t in expected_times if span[0] <= t <= span[1] and t not in stored]
    assert missing == [], f"span {tuple(span)} claims settlements with no stored row {context}: {missing[:3]}"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "market.db"
    conn = open_writer(path)
    migrate(conn, applied_at_ms=AT)
    conn.close()
    return path


@pytest.fixture
def writer(db: Path) -> Any:
    w = FundingRateWriter(open_writer(db))
    yield w
    if not w.unusable:
        w.close()


def faulty(db: Path, factory: type[FaultyConnection] = FaultyConnection) -> tuple[FaultyConnection, FundingRateWriter]:
    conn = open_faulty_writer(db, AT, factory=factory)
    w = FundingRateWriter(conn)
    conn.statements.clear()
    return conn, w


def test_span_merge_validates_its_arguments() -> None:
    with pytest.raises(TypeError):
        SpanMerge(VENUE, SYMBOL, (X, X + 1), AT)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SpanMerge("", SYMBOL, Interval(X, X + 1), AT)
    with pytest.raises(ValueError):
        SpanMerge(VENUE, "", Interval(X, X + 1), AT)
    with pytest.raises(TypeError):
        SpanMerge(VENUE, SYMBOL, Interval(X, X + 1), True)


def test_merge_creates_the_span_with_its_rows(writer: FundingRateWriter, db: Path) -> None:
    rows = [fr(X + i * HOUR) for i in range(3)]
    report = writer.write_transaction(rows, ingested_at_ms=AT, merge=merge_of(X, X + 3 * HOUR))
    assert report.inserted == 3
    assert (report.span_before, report.span_after) == (None, Interval(X, X + 3 * HOUR))
    assert span_of(db) == (X, X + 3 * HOUR, AT)
    assert times_of(db) == [X, X + HOUR, X + 2 * HOUR]


def test_merge_of_an_empty_page_is_a_merge_only_transaction(db: Path) -> None:
    conn, w = faulty(db)
    report = w.write_transaction([], ingested_at_ms=AT, merge=merge_of(X, X + HOUR))
    assert report.inserted == 0 and report.span_after == Interval(X, X + HOUR)
    assert span_of(db) == (X, X + HOUR, AT)
    assert not any(s.startswith(INSERT_ROW) for s in conn.statements)
    assert conn.statements[0] == "BEGIN IMMEDIATE" and conn.statements[-1] == "COMMIT"
    assert sum(s == "COMMIT" for s in conn.statements) == 1


def test_empty_rows_without_merge_execute_nothing(db: Path) -> None:
    conn, w = faulty(db)
    w.write_transaction([], ingested_at_ms=AT)
    assert conn.statements == []


@pytest.mark.parametrize(
    ("a", "e", "accepted", "expected"),
    [
        (1_001, 1_500, True, (0, 1_500)),  # a = through + 1
        (1_002, 1_500, False, None),  # a = through + 2
        (-500, -1, True, (-500, 1_000)),  # e = from - 1
        (-500, -2, False, None),  # e = from - 2
        (100, 200, True, (0, 1_000)),  # inside the span: a no-op
        (-5, 2_000, True, (-5, 2_000)),  # contains the span
        (500, 1_500, True, (0, 1_500)),  # overlaps above
        (-100, 500, True, (-100, 1_000)),  # overlaps below
        (0, 1_000, True, (0, 1_000)),  # the span itself
    ],
    ids=[
        "above_adjacent",
        "above_gap",
        "below_adjacent",
        "below_gap",
        "inside",
        "contains",
        "overlap_above",
        "overlap_below",
        "same",
    ],
)
def test_merge_boundaries(
    writer: FundingRateWriter, db: Path, a: int, e: int, accepted: bool, expected: tuple[int, int] | None
) -> None:
    writer.write_transaction([], ingested_at_ms=AT, merge=merge_of(X, X + 1_000))
    request = merge_of(X + a, X + e, at=AT + 7)
    if not accepted:
        with pytest.raises(FundingSpanNotAdjacentError):
            writer.write_transaction([], ingested_at_ms=AT + 7, merge=request)
        assert span_of(db) == (X, X + 1_000, AT)
        return
    report = writer.write_transaction([], ingested_at_ms=AT + 7, merge=request)
    assert expected is not None
    assert (report.span_before, report.span_after) == (
        Interval(X, X + 1_000),
        Interval(X + expected[0], X + expected[1]),
    )
    assert span_of(db) == (X + expected[0], X + expected[1], AT + 7)


def test_refused_merge_rolls_back_the_final_chunk_too(writer: FundingRateWriter, db: Path) -> None:
    writer.write_transaction([fr(X)], ingested_at_ms=AT, merge=merge_of(X, X + 10))
    with pytest.raises(FundingSpanNotAdjacentError) as error:
        writer.write_transaction([fr(X + 99), fr(X + 100)], ingested_at_ms=AT + 1, merge=merge_of(X + 99, X + 100))
    assert not isinstance(error.value, FundingRateWriteOutcomeUnknown)
    assert times_of(db) == [X]
    assert span_of(db) == (X, X + 10, AT)
    ok = writer.write_transaction([fr(X + 11)], ingested_at_ms=AT + 2, merge=merge_of(X + 11, X + 12, at=AT + 2))
    assert ok.inserted == 1 and span_of(db) == (X, X + 12, AT + 2)


def test_a_refused_merge_whose_rollback_fails_makes_the_writer_unusable(db: Path) -> None:
    conn, w = faulty(db)
    w.write_transaction([], ingested_at_ms=AT, merge=merge_of(X, X + 10))
    conn.faults.append(Fault("ROLLBACK", sqlite3.OperationalError("rollback failed")))
    with pytest.raises(FundingSpanNotAdjacentError, match="unusable"):
        w.write_transaction([fr(X + 99)], ingested_at_ms=AT, merge=merge_of(X + 99, X + 100))
    assert w.unusable
    with pytest.raises(FundingRateWriteError, match="unusable"):
        w.write_transaction([], ingested_at_ms=AT)


@pytest.mark.parametrize("seed", range(6))
def test_merge_agrees_with_the_domain_merge_span(db: Path, seed: int) -> None:
    rng = random.Random(seed)
    w = FundingRateWriter(open_writer(db))
    try:
        for case in range(200):
            symbol = f"S{seed}C{case}/USDT:USDT"
            existing: Interval | None = None
            if case % 5 != 0:  # one in five cases starts with no span
                start = X + rng.randint(0, 50)
                existing = Interval(start, start + rng.randint(0, 50))
                w.write_transaction(
                    [], ingested_at_ms=AT, merge=merge_of(existing.from_ms, existing.through_ms, symbol=symbol)
                )
            anchor = existing or Interval(X + 25, X + 25)
            a = anchor.from_ms + rng.randint(-60, 60)
            e = a + rng.randint(0, 60)
            incoming = Interval(a, e)
            request = merge_of(a, e, at=AT + 1, symbol=symbol)
            if existing is None:
                expected: Interval | None = incoming
            else:
                try:
                    expected = merge_span(existing, incoming)
                except NotAdjacentError:
                    expected = None
            if expected is None:
                with pytest.raises(FundingSpanNotAdjacentError):
                    w.write_transaction([], ingested_at_ms=AT + 1, merge=request)
            else:
                report = w.write_transaction([], ingested_at_ms=AT + 1, merge=request)
                assert report.span_after == expected
                assert span_of(db, symbol) is not None
    finally:
        w.close()


def test_merge_verified_at_never_decreases(writer: FundingRateWriter, db: Path) -> None:
    writer.write_transaction([], ingested_at_ms=AT, merge=merge_of(X, X + 10, at=AT + 100))
    writer.write_transaction([], ingested_at_ms=AT, merge=merge_of(X + 11, X + 12, at=AT + 5))
    assert span_of(db) == (X, X + 12, AT + 100)


def test_merge_rejects_rows_of_another_series(db: Path) -> None:
    conn, w = faulty(db)
    with pytest.raises(ValueError, match="series"):
        w.write_transaction([fr(X, symbol="ETH/USDT:USDT")], ingested_at_ms=AT, merge=merge_of(X, X + 1))
    assert conn.statements == []


def test_full_page_merge_claims_up_to_the_row_before_the_last(writer: FundingRateWriter, db: Path) -> None:
    times = [X + i for i in range(1_000)]
    last = times[-1]
    for start in range(0, 1_000, MAX_ROWS_PER_TRANSACTION):
        chunk = [fr(t) for t in times[start : start + MAX_ROWS_PER_TRANSACTION]]
        final = start + MAX_ROWS_PER_TRANSACTION >= 1_000
        merge = merge_of(X, last - 1) if final else None
        report = writer.write_transaction(chunk, ingested_at_ms=AT, merge=merge)
    assert report.span_after == Interval(X, last - 1)
    assert span_of(db) == (X, last - 1, AT)
    assert times_of(db)[-1] == last  # the row at `last` is stored although the span stops before it


def test_span_before_and_late_event_detection_data(writer: FundingRateWriter) -> None:
    writer.write_transaction([fr(X), fr(X + 10)], ingested_at_ms=AT, merge=merge_of(X, X + 20))
    report = writer.write_transaction(
        [fr(X + 10), fr(X + 15), fr(X + 25)], ingested_at_ms=AT + 1, merge=merge_of(X, X + 30)
    )
    assert report.span_before == Interval(X, X + 20)
    late = [k for k in report.inserted_keys if k.funding_time_ms <= report.span_before.through_ms]
    assert [k.funding_time_ms for k in late] == [X + 15]
    assert [k.funding_time_ms for k in report.inserted_keys] == [X + 15, X + 25]


def test_conflicts_do_not_block_span_growth(writer: FundingRateWriter, db: Path) -> None:
    writer.write_transaction([fr(X)], ingested_at_ms=AT, merge=merge_of(X, X + 10))
    differing = FundingRate(FundingRateKey(VENUE, SYMBOL, X, RateType.REGULAR), "0.0009", "67123.45")
    report = writer.write_transaction([differing], ingested_at_ms=AT + 1, merge=merge_of(X, X + 20, at=AT + 1))
    assert len(report.conflicts) == 1
    assert span_of(db) == (X, X + 20, AT + 1)


SPAN_STATEMENT = re.compile(
    r"(?:(?:INSERT|REPLACE)(?:\s+OR\s+\w+)?\s+INTO|UPDATE(?:\s+OR\s+\w+)?)\s+funding_rate_verified", re.IGNORECASE
)


def test_the_span_is_written_by_one_function_only() -> None:
    sources = sorted((ROOT / "src" / "quasar_market_data").rglob("*.py"))
    holders = [p for p in sources if SPAN_STATEMENT.search(p.read_text("utf-8"))]
    assert [p.name for p in holders] == ["funding_rate_writer.py"]
    tree = ast.parse(holders[0].read_text("utf-8"))
    constants = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign | ast.AnnAssign)
        and node.value is not None
        and SPAN_STATEMENT.search(ast.unparse(node.value))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    assert constants == {"_MERGE_INSERT", "_MERGE_UPDATE"}
    users = {
        f"{owner.name}"
        for owner in ast.walk(tree)
        if isinstance(owner, ast.FunctionDef)
        for node in ast.walk(owner)
        if isinstance(node, ast.Name) and node.id in constants
    }
    assert users == {"_merge_span_statements"}


# --- crash table (ADR-0004 P-7) -----------------------------------------------------------------------------

PAGE_TIMES = [X + i * 1_000 for i in range(250)]
PAGE_END = PAGE_TIMES[-1] + 1_000
BOOM = sqlite3.OperationalError("injected")


def chunks_of_page() -> list[list[FundingRate]]:
    rows = [fr(t) for t in PAGE_TIMES]
    return [rows[0:100], rows[100:200], rows[200:250]]


def send_page(w: FundingRateWriter, at: int = AT) -> None:
    chunks = chunks_of_page()
    for number, chunk in enumerate(chunks):
        merge = merge_of(X, PAGE_END, at=at) if number == len(chunks) - 1 else None
        w.write_transaction(chunk, ingested_at_ms=at, merge=merge)


def clean_dump(tmp_path: Path) -> dict[str, list[tuple[Any, ...]]]:
    path = tmp_path / "clean.db"
    conn = open_writer(path)
    migrate(conn, applied_at_ms=AT)
    w = FundingRateWriter(conn)
    send_page(w)
    w.close()
    return dump(path)


FAULT_POINTS = {
    "chunk2_row": (Fault(INSERT_ROW, BOOM, nth=150), 100),
    "final_first_row": (Fault(INSERT_ROW, BOOM, nth=201), 200),
    "final_last_row": (Fault(INSERT_ROW, BOOM, nth=250), 200),
    "span_insert": (Fault("INSERT INTO funding_rate_verified", BOOM), 200),
    "span_update": (Fault("UPDATE funding_rate_verified", BOOM), 200),
    "span_select_before": (Fault("SELECT from_ms", BOOM, nth=1), 200),
    "span_select_after": (Fault("SELECT from_ms", BOOM, nth=2), 200),
    "commit_before_execution": (Fault("COMMIT", BOOM, nth=3), 200),
}


@pytest.mark.parametrize("point", sorted(FAULT_POINTS))
def test_failure_leaves_the_stored_rows_and_never_a_span(db: Path, tmp_path: Path, point: str) -> None:
    fault, stored_rows = FAULT_POINTS[point]
    conn, w = faulty(db)
    conn.faults.append(Fault(fault.prefix, fault.exception, fault.nth))
    with pytest.raises(FundingRateWriteError):
        send_page(w)
    reader = open_reader(db)
    try:
        assert reader.execute("SELECT count(*) FROM funding_rates").fetchone() == (stored_rows,)
        assert reader.execute("SELECT count(*) FROM funding_rate_verified").fetchone() == (0,)
        assert_span_is_backed(reader, PAGE_TIMES, point)
    finally:
        reader.close()
    # restart: a new writer re-sends the page and converges to the clean run, with no duplicate and no conflict
    restarted = FundingRateWriter(open_writer(db))
    send_page(restarted)
    restarted.close()
    assert dump(db) == clean_dump(tmp_path)
    assert dump(db)["conflicts"] == []


def test_commit_that_took_effect_is_all_or_nothing(db: Path, tmp_path: Path) -> None:
    conn, w = faulty(db)
    conn.faults.append(Fault("COMMIT", sqlite3.OperationalError("disk I/O error"), nth=3, after_execution=True))
    with pytest.raises(FundingRateWriteOutcomeUnknown):
        send_page(w)
    other = open_reader(db)  # a second connection
    try:
        assert other.execute("SELECT count(*) FROM funding_rates").fetchone() == (250,)
        assert other.execute("SELECT from_ms, through_ms FROM funding_rate_verified").fetchall() == [(X, PAGE_END)]
        assert_span_is_backed(other, PAGE_TIMES)
    finally:
        other.close()
    retry = FundingRateWriter(open_writer(db))
    send_page(retry)
    retry.close()
    assert dump(db) == clean_dump(tmp_path)  # nothing changed: all duplicates, the span is a no-op, no conflict


def test_committed_page_whose_report_is_lost_is_a_no_op_on_resend(db: Path, tmp_path: Path) -> None:
    w = FundingRateWriter(open_writer(db))
    send_page(w)
    before = dump(db)
    again = FundingRateWriter(open_writer(db))
    for number, chunk in enumerate(chunks_of_page()):
        merge = merge_of(X, PAGE_END) if number == 2 else None
        report = again.write_transaction(chunk, ingested_at_ms=AT + 1, merge=merge)
        assert (report.inserted, report.conflicts) == (0, ())
    assert dump(db) == before == clean_dump(tmp_path)
    w.close()
    again.close()


class ObservingConnection(FaultyConnection):
    """After every statement of the writer connection a reader looks at the committed state."""

    observer: Callable[[], None] | None = None

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        cursor = super().execute(sql, *args)
        if self.observer is not None:
            self.observer()
        return cursor


def test_a_reader_never_sees_a_span_without_its_rows(db: Path) -> None:
    conn = open_faulty_writer(db, AT, factory=ObservingConnection)
    assert isinstance(conn, ObservingConnection)
    w = FundingRateWriter(conn)
    reader = open_reader(db)
    observations: list[tuple[int, int]] = []

    def observe() -> None:
        assert_span_is_backed(reader, PAGE_TIMES, "(observed after a writer statement)")
        observations.append(
            (
                reader.execute("SELECT count(*) FROM funding_rates").fetchone()[0],
                reader.execute("SELECT count(*) FROM funding_rate_verified").fetchone()[0],
            )
        )

    conn.observer = observe
    send_page(w)
    conn.observer = None
    assert observations[-1] == (250, 1)
    assert (200, 0) in observations  # the first two chunks were visible without the span
    assert all(spans == 0 for rows, spans in observations if rows < 250)
    reader.close()
    w.close()


def test_a_snapshot_pinned_before_the_final_transaction_sees_neither(db: Path) -> None:
    w = FundingRateWriter(open_writer(db))
    chunks = chunks_of_page()
    w.write_transaction(chunks[0], ingested_at_ms=AT)
    w.write_transaction(chunks[1], ingested_at_ms=AT)
    reader = open_reader(db)
    reader.execute("BEGIN")
    assert reader.execute("SELECT count(*) FROM funding_rate_verified").fetchone() == (0,)  # pins the snapshot
    w.write_transaction(chunks[2], ingested_at_ms=AT, merge=merge_of(X, PAGE_END))
    assert reader.execute("SELECT count(*) FROM funding_rates").fetchone() == (200,)
    assert reader.execute("SELECT count(*) FROM funding_rate_verified").fetchone() == (0,)
    reader.execute("COMMIT")
    assert reader.execute("SELECT count(*) FROM funding_rates").fetchone() == (250,)
    assert reader.execute("SELECT count(*) FROM funding_rate_verified").fetchone() == (1,)
    reader.close()
    w.close()


def test_killed_process_inside_the_final_transaction_leaves_neither_rows_nor_span(db: Path, tmp_path: Path) -> None:
    script = textwrap.dedent(
        """
        import os, sqlite3, sys
        from pathlib import Path
        from quasar_market_data.collector.funding_rate_writer import FundingRateWriter, SpanMerge
        from quasar_market_data.domain.funding_rate import FundingRate, FundingRateKey, RateType
        from quasar_market_data.domain.funding_rate_span import Interval

        class Killer(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql.startswith("COMMIT"):
                    os._exit(7)  # the process dies with the final transaction open
                return super().execute(sql, *args)

        conn = sqlite3.connect(sys.argv[1], factory=Killer, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA recursive_triggers=ON")
        writer = FundingRateWriter(conn)
        rows = [
            FundingRate(FundingRateKey("binance-usdm", "BTC/USDT:USDT", int(t), RateType.REGULAR), "0.0001", None)
            for t in sys.argv[2:]
        ]
        writer.write_transaction(rows, ingested_at_ms=1_793_840_000_000,
                                 merge=SpanMerge("binance-usdm", "BTC/USDT:USDT", Interval(int(sys.argv[2]), int(sys.argv[-1]) + 1),
                                                 1_793_840_000_000))
        """
    )
    times = [str(t) for t in PAGE_TIMES[:5]]
    result = subprocess.run(
        [sys.executable, "-c", script, str(db), *times], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 7, result.stderr
    assert times_of(db) == []
    assert span_of(db) is None
    restarted = FundingRateWriter(open_writer(db))
    restarted.write_transaction(
        [fr(int(t)) for t in times], ingested_at_ms=AT, merge=merge_of(int(times[0]), int(times[-1]) + 1)
    )
    restarted.close()
    assert len(times_of(db)) == 5 and span_of(db) is not None
