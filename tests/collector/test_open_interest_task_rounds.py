"""Rounds of the open interest task (ADR-0003 P-9, P-10, P-15 rows marked OI-4)."""

import asyncio
import itertools
import math
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.collector.async_open_interest_writer import OpenInterestPageError, OpenInterestWriteReport
from quasar_market_data.collector.binance_usdm_open_interest import OpenInterestRequestRefused
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.collector.open_interest_gate import OpenInterestBanned, OpenInterestPaused
from quasar_market_data.collector.open_interest_task import OpenInterestWriterUnusable, _OpenInterestTask
from quasar_market_data.collector.open_interest_writer import (
    OpenInterestWriteBusy,
    OpenInterestWriteError,
    OpenInterestWriteOutcomeUnknown,
)
from tests.collector.manual_time import NS_PER_MS, assert_no_leftover_tasks
from tests.collector.open_interest_task_fakes import (
    SKEW_MS,
    STEP,
    RecordingWriter,
    Rig,
    make_rig,
    stored_times,
    symbols_of,
    table_rows,
)

OFFSET = 200_000


@pytest.fixture(autouse=True)
def no_catch_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests are about rounds: the idle window (catch-up) has its own file."""

    async def nothing(self: Any, wake_ns: int, wake_ms: int) -> None:
        return None

    monkeypatch.setattr(_OpenInterestTask, "_idle_window", nothing)


def run(scenario: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(scenario())


def boundaries(rig: Rig) -> list[int]:
    return sorted({int(line.split()[1].split("=")[1]) for line in rig.lines_with("oi-round")})


def round_boundary(line: str) -> int:
    return int(line.split()[1].split("=")[1])


def field(line: str, name: str) -> int:
    return int(next(part for part in line.split() if part.startswith(f"{name}=")).split("=")[1])


async def first_wake(rig: Rig) -> int:
    """Settle the first (late) round and return the boundary of the next one."""
    rig.start()
    await rig.until_lines("oi-round", 1)
    return round_boundary(rig.lines_with("oi-round")[0]) + STEP


def test_the_first_round_runs_late_inside_its_window_then_waits_for_the_next_wake(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, recording=True)
        start_wall = rig.t.wall_ms()
        rig.start()
        await rig.until_lines("oi-round", 1)
        line = rig.lines_with("oi-round")[0]
        first = round_boundary(line)
        # the newest boundary whose wake is behind us and whose deadline is not: it runs late
        assert first + OFFSET <= start_wall < first + OFFSET + STEP - 15_000
        assert field(line, "late_ms") == start_wall - (first + OFFSET)
        assert field(line, "skipped_rounds") == 0
        wake_ns = rig.t.monotonic_ns() + (first + STEP + OFFSET - rig.t.wall_ms()) * NS_PER_MS
        await rig.t.settle()
        assert rig.t.waiting == [wake_ns]
        await rig.stop_and_wait()
        await assert_no_leftover_tasks()

    run(scenario)


def test_rounds_follow_every_wake_and_never_overlap(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, recording=True)
        rig.start()
        await rig.until_lines("oi-round", 1)
        for index in range(2, 5):
            await rig.advance_to_next_wake()
            await rig.until_lines("oi-round", index)
        found = boundaries(rig)
        assert [b - a for a, b in itertools.pairwise(found)] == [STEP] * 3
        # every round's hist calls are finished before the next round's wake: calls per boundary are contiguous
        order = [call.end_ms for call in rig.source.hist_calls]
        assert order == sorted(order)
        await rig.stop_and_wait()

    run(scenario)


def test_c1_every_round_asks_all_symbols_with_three_period_ends(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=symbols_of(10), recording=True)
        rig.start()
        await rig.until_lines("oi-round", 1)
        boundary = round_boundary(rig.lines_with("oi-round")[0])
        assert sorted(call.symbol for call in rig.source.hist_calls) == sorted(symbols_of(10))
        assert {(c.start_ms, c.end_ms) for c in rig.source.hist_calls} == {(boundary - 2 * STEP, boundary)}
        assert rig.source.max_active <= 2  # MAX_IN_FLIGHT
        assert len(rig.source.time_calls) == 1
        await rig.stop_and_wait()

    run(scenario)


def test_c5_every_symbol_is_asked_exactly_once_per_five_rounds(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=symbols_of(600), budget_per_5min=400, recording=True)
        assert rig.config.cadence == 5
        rig.start()
        await rig.until_lines("oi-round", 1)
        for index in range(2, 26):
            await rig.advance_to_next_wake()
            await rig.until_lines("oi-round", index)
        by_boundary: dict[int, list[str]] = {}
        for call in rig.source.hist_calls:
            by_boundary.setdefault(call.end_ms, []).append(call.symbol)
        assert len(by_boundary) == 25 and all(len(s) == 120 for s in by_boundary.values())
        ordered = [by_boundary[b] for b in sorted(by_boundary)]
        for start in range(0, 25, 5):  # any five consecutive rounds ask each symbol exactly once
            window = [symbol for symbols in ordered[start : start + 5] for symbol in symbols]
            assert sorted(window) == sorted(symbols_of(600))
        assert {c.end_ms - c.start_ms for c in rig.source.hist_calls} == {10 * STEP}  # 2c + 1 = 11 period ends
        await rig.stop_and_wait()

    run(scenario)


def test_a_row_absent_at_round_b_is_stored_once_at_the_next_round_without_retry_or_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=("BTC/USDT:USDT",))
        rig.start()
        await rig.until_lines("oi-round", 1)
        first = round_boundary(rig.lines_with("oi-round")[0])
        target = first + STEP  # published only after its own round's request
        rig.source.late[("BTC/USDT:USDT", target)] = target + 250_000
        await rig.advance_to_next_wake()  # round `target`
        await rig.until_lines("oi-round", 2)
        assert target not in stored_times(rig.db)
        assert len([c for c in rig.source.hist_calls if c.end_ms == target]) == 1
        await rig.advance_to_next_wake()  # round target + 5 min: its range [target - 10 min, target + 5 min] covers it
        await rig.until_lines("oi-round", 3)
        times = stored_times(rig.db)
        assert times.count(target) == 1
        assert len([c for c in rig.source.hist_calls if c.end_ms == target + STEP]) == 1  # no retry used
        assert rig.lines_with("oi-warning") == [] and rig.lines_with("oi-error") == []
        await rig.stop_and_wait()

    run(scenario)


def test_ideal_run_c5_stores_a_published_row_within_c_periods_plus_the_offset(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(
            tmp_path, symbols=symbols_of(150), budget_per_5min=100, recording=True, publish_lag_ms=150_000
        )
        assert rig.config.cadence == 5
        writer = rig.writer
        assert isinstance(writer, RecordingWriter)
        rig.start()
        await rig.until_lines("oi-round", 1)
        for index in range(2, 32):
            await rig.advance_to_next_wake()
            await rig.until_lines("oi-round", index)
        first = round_boundary(rig.lines_with("oi-round")[0])
        fresh = {key: at for key, at in writer.stored.items() if key[1] >= first}  # slots of the run, not the history
        assert len(fresh) > 100
        for (_, time_ms), ingested_at in fresh.items():
            assert ingested_at - time_ms <= 5 * STEP + OFFSET
        await rig.stop_and_wait()

    run(scenario)


def test_a_failing_next_request_leaves_the_slot_absent_and_a_later_one_stores_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=("BTC/USDT:USDT",))
        rig.start()
        await rig.until_lines("oi-round", 1)
        first = round_boundary(rig.lines_with("oi-round")[0])
        target = first + STEP
        rig.source.late[("BTC/USDT:USDT", target)] = target + 250_000
        await rig.advance_to_next_wake()
        await rig.until_lines("oi-round", 2)
        rig.source.errors = [SourceError("boom"), SourceError("boom again")]  # the next request and its retry fail
        await rig.advance_to_next_wake()
        await rig.t.advance(1_000)  # the retry delay
        await rig.until_lines("oi-round", 3)
        assert target not in stored_times(rig.db)  # never fabricated
        await rig.advance_to_next_wake()
        await rig.until_lines("oi-round", 4)
        assert stored_times(rig.db).count(target) == 1
        await rig.stop_and_wait()

    run(scenario)


def test_one_retry_after_one_second_then_success(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=("BTC/USDT:USDT",), recording=True)
        rig.source.errors = [SourceError("temporary")]
        rig.start()
        await rig.t.settle()
        assert len(rig.source.hist_calls) == 1
        await rig.t.advance(999)
        assert len(rig.source.hist_calls) == 1
        await rig.t.advance(1)
        await rig.until_lines("oi-round", 1)
        first, second = rig.source.hist_calls
        assert second.at_ns - first.at_ns == 1_000 * NS_PER_MS
        assert field(rig.lines_with("oi-round")[0], "ok") == 1
        await rig.stop_and_wait()

    run(scenario)


def test_two_errors_fail_the_symbol_after_exactly_two_calls(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=("BTC/USDT:USDT",), recording=True)
        rig.source.errors = [SourceError("one"), SourceError("two"), SourceError("never reached")]
        rig.start()
        await rig.t.settle()
        await rig.t.advance(1_000)
        await rig.until_lines("oi-round", 1)
        assert len(rig.source.hist_calls) == 2
        assert field(rig.lines_with("oi-round")[0], "failed") == 1
        assert len(rig.lines_with("oi-warning symbol-failed")) == 1
        await rig.stop_and_wait()

    run(scenario)


def test_a_parse_error_stores_nothing_and_uses_the_one_retry(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=("BTC/USDT:USDT",), recording=True)
        rig.source.mutate_rows = lambda rows: [*rows, {**rows[0], "timestamp": rows[0]["timestamp"] - 10 * STEP}]
        rig.start()
        await rig.t.settle()
        await rig.t.advance(1_000)
        await rig.until_lines("oi-round", 1)
        assert len(rig.source.hist_calls) == 2
        assert isinstance(rig.writer, RecordingWriter) and rig.writer.writes == []
        assert field(rig.lines_with("oi-round")[0], "failed") == 1
        await rig.stop_and_wait()

    run(scenario)


def test_a_retry_that_would_end_past_the_deadline_is_not_started(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=("BTC/USDT:USDT",), recording=True)
        rig.source.errors = [SourceError("temporary")]
        rig.t._wall_ms += 148_500
        rig.t._mono_ns += 148_500 * NS_PER_MS
        rig.start()
        await rig.until_lines("oi-round", 1)
        assert len(rig.source.hist_calls) == 1
        assert field(rig.lines_with("oi-round")[0], "failed") == 1
        await rig.stop_and_wait()

    run(scenario)


def test_a_refused_request_is_not_retried(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=("BTC/USDT:USDT",), recording=True)
        rig.source.errors = [OpenInterestRequestRefused("range too old")]
        rig.start()
        await rig.until_lines("oi-round", 1)
        assert len(rig.source.hist_calls) == 1
        assert field(rig.lines_with("oi-round")[0], "refused") == 1
        assert len(rig.lines_with("oi-error request-refused")) == 1
        await rig.stop_and_wait()

    run(scenario)


def test_a_row_inside_the_publication_delay_is_pending_and_not_written(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(
            tmp_path, symbols=("BTC/USDT:USDT",), recording=True, publication_delay_ms=340_000, publish_lag_ms=0
        )
        rig.start()
        await rig.until_lines("oi-round", 1)
        line = rig.lines_with("oi-round")[0]
        boundary = round_boundary(line)
        assert isinstance(rig.writer, RecordingWriter)
        written = {row.key.time_ms for rows, _ in rig.writer.writes for row in rows}
        assert (
            boundary not in written and boundary - STEP in written
        )  # B + 340 s is past L (the first round is 136 s late); B - 5 min is not
        assert field(line, "pending") >= 1
        await rig.stop_and_wait()

    run(scenario)


def test_untrusted_time_makes_no_history_request_and_a_later_round_recovers(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=("BTC/USDT:USDT",), recording=True)
        rig.source.time_error = SourceError("no time")
        rig.start()
        await rig.until_lines("oi-round", 1)
        assert rig.source.hist_calls == []
        assert field(rig.lines_with("oi-round")[0], "untrusted") == 1
        assert len(rig.lines_with("oi-warning round-untrusted-time")) == 1
        rig.source.time_error = None
        await rig.advance_to_next_wake()
        await rig.until_lines("oi-round", 2)
        assert len(rig.source.hist_calls) == 1 and field(rig.lines_with("oi-round")[1], "ok") == 1
        await rig.stop_and_wait()

    run(scenario)


def test_a_429_stops_the_round_and_the_task_continues_with_the_next(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=symbols_of(10), recording=True)
        rig.source.errors = [OpenInterestPaused(300_000, rig.t.monotonic_ns() + 300_000 * NS_PER_MS)]
        rig.start()
        await rig.until_lines("oi-round", 1)
        line = rig.lines_with("oi-round")[0]
        assert field(line, "paused") == 10
        assert len(rig.source.hist_calls) <= 2  # at most the calls in flight when the first answered 429
        calls_before = len(rig.source.hist_calls)
        await rig.advance_to_next_wake()
        await rig.until_lines("oi-round", 2)
        assert field(rig.lines_with("oi-round")[1], "ok") == 10
        assert len(rig.source.hist_calls) == calls_before + 10
        await rig.stop_and_wait()

    run(scenario)


def test_a_429_on_the_time_request_skips_the_round(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=("BTC/USDT:USDT",), recording=True)
        rig.source.time_error = OpenInterestPaused(300_000, 0)
        rig.start()
        await rig.until_lines("oi-round", 1)
        assert rig.source.hist_calls == [] and field(rig.lines_with("oi-round")[0], "paused") == 1
        await rig.stop_and_wait()

    run(scenario)


def test_a_418_ends_the_task_by_raising_without_logging_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=("BTC/USDT:USDT",), recording=True)
        rig.source.errors = [OpenInterestBanned()]
        rig.start()
        assert rig.task is not None
        with pytest.raises(OpenInterestBanned):
            await asyncio.wait_for(rig.task, 5)
        assert rig.lines_with("oi-error") == []
        assert len(rig.source.hist_calls) == 1

    run(scenario)


def max_starts_in_any_window(starts: list[int]) -> int:
    ordered = sorted(starts)
    worst, low = 0, 0
    for high, instant in enumerate(ordered):
        while ordered[low] <= instant - 300_000 * NS_PER_MS:
            low += 1
        worst = max(worst, high - low + 1)
    return worst


@pytest.mark.parametrize(("budget", "cadence"), [(400, 5), (200, 9)])
def test_no_five_minute_window_exceeds_the_round_cost_even_when_every_first_attempt_fails(
    tmp_path: Path, budget: int, cadence: int
) -> None:
    async def scenario() -> None:
        rig = await make_rig(
            tmp_path, symbols=symbols_of(600), budget_per_5min=budget, recording=True, count_budget_in_source=True
        )
        assert rig.config.cadence == cadence
        rig.source.fail_first = True
        rig.align_to_next_wake()  # rounds exactly 300 s apart, as scheduled
        rig.start()
        for index in range(1, 14):  # about one hour of rounds
            if index > 1:
                await rig.advance_to_next_wake()
            await rig.t.settle()
            await rig.t.advance(1_000)  # the retry delay
            await rig.until_lines("oi-round", index)
        starts = [call.at_ns for call in rig.source.hist_calls] + rig.source.time_calls
        bound = 2 * math.ceil(600 / cadence) + 2
        assert rig.config.round_cost == bound
        assert max_starts_in_any_window(starts) <= bound
        assert max_starts_in_any_window(starts) <= budget
        assert len(rig.source.hist_calls) > 13 * math.ceil(600 / cadence)  # the retries really happened
        await rig.stop_and_wait()

    run(scenario)


def test_rows_waiting_for_the_candle_writer_are_dropped_at_the_deadline_and_stored_by_the_next_round(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=("BTC/USDT:USDT",))
        hold = rig.activity.writing()
        hold.__enter__()  # a candle write that never ends during this round
        rig.start()
        await rig.t.settle()
        first_deadline = min(rig.t.waiting)  # the round's deadline timer
        wall_deadline = 1_792_676_700_000 + OFFSET + STEP - 15_000  # B + 300 s + offset - 15 s of the first round
        assert first_deadline == rig.t.monotonic_ns() + (wall_deadline - rig.t.wall_ms()) * NS_PER_MS
        await rig.t.advance_ns(first_deadline - rig.t.monotonic_ns())
        await rig.until_lines("oi-round", 1)
        line = rig.lines_with("oi-round")[0]
        assert field(line, "deadline") == 1
        warning = rig.lines_with("oi-warning round-deadline")[0]
        assert "BTC/USDT:USDT:waiting-for-candles" in warning and "rows-dropped-unwritten" in warning
        assert stored_times(rig.db) == []
        hold.__exit__(None, None, None)
        await rig.advance_to_next_wake()
        await rig.until_lines("oi-round", 2)
        assert len(stored_times(rig.db)) >= 3  # the overlapping range stored the dropped rows
        await rig.stop_and_wait()

    run(scenario)


def test_a_differing_redelivery_is_one_conflict_line_and_the_next_round_adds_none(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=("BTC/USDT:USDT",))
        rig.start()
        await rig.until_lines("oi-round", 1)
        before = table_rows(rig.db)
        first = round_boundary(rig.lines_with("oi-round")[0])
        rig.source.values = lambda time_ms: ("7.5", "8.25")  # every re-delivered slot now differs
        await rig.advance_to_next_wake()
        await rig.until_lines("oi-round", 2)
        unchanged = [row for row in table_rows(rig.db) if row[3] <= first]
        assert unchanged == before  # the stored rows are never written over
        conflicts = rig.lines_with("oi-conflict")
        assert len(conflicts) == 2 and all(
            "incoming=7.5/8.25" in line for line in conflicts
        )  # slots first - 5 min, first
        assert field(rig.lines_with("oi-round")[1], "conflicts") == 2
        await rig.advance_to_next_wake()
        await rig.until_lines("oi-round", 3)
        assert len(rig.lines_with("oi-conflict")) == 2  # the same differing value again: no new line
        assert field(rig.lines_with("oi-round")[2], "conflicts") == 1  # but the repeated one is counted
        await rig.stop_and_wait()

    run(scenario)


def test_stop_during_the_wait_returns_without_another_round_and_leaves_no_task(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, recording=True)
        rig.start()
        await rig.until_lines("oi-round", 1)
        await rig.stop_and_wait()
        assert len(rig.lines_with("oi-round")) == 1
        await assert_no_leftover_tasks()

    run(scenario)


def test_stop_during_a_round_cancels_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, recording=True)
        rig.source.hang_hist = True
        rig.start()
        await rig.t.settle()
        await rig.stop_and_wait()
        assert field(rig.lines_with("oi-round")[0], "stopped") == 2
        await assert_no_leftover_tasks()

    run(scenario)


def test_a_round_longer_than_the_time_trust_still_stores_every_symbol_with_its_single_bound(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=symbols_of(120))
        assert rig.config.cadence == 1
        hold = rig.activity.writing()
        hold.__enter__()  # candle writes keep every open interest write waiting for 90 s (> max_age_ms of 60 s)
        rig.start()
        await rig.t.settle()
        await rig.t.advance(90_000)
        hold.__exit__(None, None, None)
        await rig.until_lines("oi-round", 1)
        line = rig.lines_with("oi-round")[0]
        assert field(line, "ok") == 120 and field(line, "untrusted") == 0
        assert rig.lines_with("oi-warning") == []
        assert len({t for t in stored_times(rig.db)}) >= 3
        await rig.stop_and_wait()

    run(scenario)


def page_error(cause: OpenInterestWriteError) -> OpenInterestPageError:
    return OpenInterestPageError(cause, 0, OpenInterestWriteReport(0, 0, (), (), (), 0, 0))


@pytest.mark.parametrize("cause", [OpenInterestWriteBusy("busy"), OpenInterestWriteOutcomeUnknown("unknown")])
def test_busy_and_unknown_outcome_use_the_one_retry_and_the_task_continues(
    tmp_path: Path, cause: OpenInterestWriteError
) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=("BTC/USDT:USDT",), recording=True)
        assert isinstance(rig.writer, RecordingWriter)
        rig.writer.errors = [page_error(cause)]
        rig.start()
        await rig.t.settle()
        await rig.t.advance(1_000)
        await rig.until_lines("oi-round", 1)
        assert field(rig.lines_with("oi-round")[0], "ok") == 1
        assert (rig.measurements.line(1000) or "").count("busy=1") == (
            1 if isinstance(cause, OpenInterestWriteBusy) else 0
        )
        await rig.advance_to_next_wake()
        await rig.until_lines("oi-round", 2)
        await rig.stop_and_wait()

    run(scenario)


def test_an_unusable_writer_ends_the_task(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=("BTC/USDT:USDT",), recording=True)
        assert isinstance(rig.writer, RecordingWriter)
        rig.writer.errors = [page_error(OpenInterestWriteError("writer is closed"))]
        rig.start()
        assert rig.task is not None
        with pytest.raises(OpenInterestWriterUnusable):
            await asyncio.wait_for(rig.task, 5)
        assert rig.lines_with("oi-error") == []

    run(scenario)


def test_an_hourly_measurement_line_appears_and_the_counters_reset(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=("BTC/USDT:USDT",))
        rig.measurements.record_candle_write(7_000_000)
        rig.start()
        await rig.until_lines("oi-round", 1)
        assert rig.lines_with("oi-measurement") == []
        for index in range(2, 15):  # a bit more than an hour of rounds
            await rig.advance_to_next_wake()
            await rig.until_lines("oi-round", index)
        lines = rig.lines_with("oi-measurement")
        assert len(lines) == 1 and "window_s=" in lines[0] and "candle_writes=1" in lines[0] and "oi_tx=" in lines[0]
        await rig.stop_and_wait()
        final = rig.lines_with("oi-measurement")
        assert all("candle_writes=0" in line for line in final[1:])  # the counters were reset after the first line

    run(scenario)


def test_the_skew_constant_is_what_the_trust_gate_sees() -> None:
    assert SKEW_MS == 1_000
