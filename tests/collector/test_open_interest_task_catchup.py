"""Restart reconcile and bounded catch-up of the open interest task (ADR-0003 P-12, P-15 rows marked OI-4)."""

import asyncio
import itertools
import shutil
import sqlite3
import threading
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, ClassVar

import pytest

import quasar_market_data.collector.open_interest_task as task_module
from quasar_market_data.collector.async_open_interest_writer import OpenInterestPageError, OpenInterestWriteReport
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.collector.open_interest_gate import OpenInterestPaused
from quasar_market_data.collector.open_interest_reader import OpenInterestReadError
from quasar_market_data.collector.open_interest_task import OpenInterestWriterUnusable
from quasar_market_data.collector.open_interest_writer import OpenInterestWriteError
from quasar_market_data.domain.open_interest_holes import newest_expected_ms, oldest_allowed_ms
from quasar_market_data.domain.open_interest_request import RETRIEVAL_HORIZON_MS
from tests.collector.faulty_connection import Fault, FaultyConnection
from tests.collector.manual_time import NS_PER_MS, ManualTime
from tests.collector.open_interest_task_fakes import (
    P5,
    STEP,
    RecordingWriter,
    Rig,
    make_rig,
    prefill,
    stored_times,
    table_rows,
)

DAY = 86_400_000
START_WALL = 1_792_677_036_000
BTC = "BTC/USDT:USDT"
ETH = "ETH/USDT:USDT"


def run(scenario: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(scenario())


def field(line: str, name: str) -> str:
    return next(part for part in line.split() if part.startswith(f"{name}=")).split("=", 1)[1]


def round_boundary(line: str) -> int:
    return int(field(line, "boundary"))


def aligned_range(first_ms: int, last_ms: int) -> list[int]:
    return list(range(-(-first_ms // STEP) * STEP, last_ms // STEP * STEP + 1, STEP))


def page_calls(rig: Rig) -> list[Any]:
    """Catch-up pages are the history requests that are not a round's ``[B - 2 periods, B]``."""
    return [c for c in rig.source.hist_calls if c.end_ms - c.start_ms != 2 * STEP]


async def next_cycle(rig: Rig, rounds: int) -> None:
    await rig.advance_to_next_wake()
    await rig.until_lines("oi-round", rounds)
    await rig.t.settle()


async def first_window(rig: Rig) -> None:
    rig.start()
    await rig.until_lines("oi-round", 1)
    await rig.until_lines("oi-catchup", 1)


def full_history(wall_ms: int) -> list[int]:
    """Every slot a window at ``wall_ms`` can derive as a hole, plus a day of margin on both sides."""
    return aligned_range(wall_ms - RETRIEVAL_HORIZON_MS - DAY, wall_ms + DAY)


def test_an_empty_database_is_reconciled_and_the_first_window_fills_every_hole(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=(BTC, ETH))
        await first_window(rig)
        reconcile = rig.lines_with("oi-reconcile symbol=")
        assert len(reconcile) == 2
        for line in reconcile:
            assert 8340 <= int(field(line, "holes")) <= 8352 and field(line, "no_longer_recoverable") == "0"
        total = rig.lines_with("oi-reconcile total")[0]
        assert field(total, "series") == "2"
        as_of = int(field(total, "as_of"))
        oldest = oldest_allowed_ms(as_of, P5)
        newest = newest_expected_ms(as_of, 1, 200_000, P5)
        for symbol in (BTC, ETH):
            assert set(aligned_range(oldest, newest)) <= set(stored_times(rig.db, symbol))
        catchup = rig.lines_with("oi-catchup")[0]
        assert field(catchup, "pages") == "34" and field(catchup, "end") == "no-pages"  # 17 pages per symbol
        await rig.stop_and_wait()

    run(scenario)


def test_reconcile_summary_counts_the_holes_of_a_nearly_complete_series(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=(BTC,))
        slots = [
            s
            for s in full_history(START_WALL)
            if s
            not in (
                START_WALL // STEP * STEP - 1_000 * STEP,
                START_WALL // STEP * STEP - 1_001 * STEP,
                START_WALL // STEP * STEP - 3_000 * STEP,
            )
        ]
        prefill(rig.db, (BTC,), slots, START_WALL)
        await first_window(rig)
        line = rig.lines_with("oi-reconcile symbol=")[0]
        assert field(line, "holes") == "3"
        two_pages = [c for c in page_calls(rig)]
        assert [(c.start_ms, c.end_ms) for c in two_pages] == [
            (START_WALL // STEP * STEP - 3_000 * STEP,) * 2,
            (START_WALL // STEP * STEP - 1_001 * STEP, START_WALL // STEP * STEP - 1_000 * STEP),
        ]
        await rig.stop_and_wait()

    run(scenario)


def test_a_long_outage_counts_what_is_no_longer_recoverable_and_requests_nothing_older_than_the_horizon(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=(BTC,))
        last_stored = (START_WALL - 40 * DAY) // STEP * STEP
        old_slots = [last_stored - i * STEP for i in range(50)]
        prefill(rig.db, (BTC,), sorted(old_slots), START_WALL)
        await first_window(rig)
        total = rig.lines_with("oi-reconcile total")[0]
        as_of = int(field(total, "as_of"))
        oldest = oldest_allowed_ms(as_of, P5)
        first = min(old_slots)
        expected = len([s for s in range(first, oldest, STEP) if s not in set(old_slots)])
        assert int(field(rig.lines_with("oi-reconcile symbol=")[0], "no_longer_recoverable")) == expected > 3000
        pages = page_calls(rig)
        assert pages and all(c.start_ms >= c.as_of_ms - RETRIEVAL_HORIZON_MS for c in pages)
        assert [c.start_ms for c in pages] == sorted(c.start_ms for c in pages)  # oldest first
        await rig.stop_and_wait()

    run(scenario)


def test_the_page_plan_is_deterministic_for_the_same_stored_rows_and_time(tmp_path: Path) -> None:
    async def scenario() -> list[list[tuple[str, int, int]]]:
        runs = []
        template = tmp_path / "template.db"
        shutil.copy(await _seeded(tmp_path), template)
        for index in range(2):
            folder = tmp_path / f"run{index}"
            folder.mkdir()
            shutil.copy(template, folder / "market.db")
            rig = await make_rig(folder, symbols=(BTC, ETH), db=folder / "market.db")
            await first_window(rig)
            runs.append([(c.symbol, c.start_ms, c.end_ms) for c in page_calls(rig)])
            await rig.stop_and_wait()
        return runs

    async def _seeded(folder: Path) -> Path:
        rig = await make_rig(folder, symbols=(BTC, ETH))
        slots = [s for s in full_history(START_WALL) if s % (7 * STEP) != 0]  # holes all over the history
        prefill(rig.db, (BTC, ETH), slots, START_WALL)
        await rig.writer.close()
        return rig.db

    first, second = asyncio.run(scenario())
    assert first and first == second
    assert [(s, symbol) for symbol, s, _ in first[:2]] == sorted((s, symbol) for symbol, s, _ in first[:2])


def max_starts_in_any_window(starts: list[int]) -> int:
    ordered = sorted(starts)
    worst, low = 0, 0
    for high, instant in enumerate(ordered):
        while ordered[low] <= instant - 300_000 * NS_PER_MS:
            low += 1
        worst = max(worst, high - low + 1)
    return worst


def test_rounds_pre_empt_the_catch_up_when_the_budget_is_nearly_used(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=(BTC, ETH), budget_per_5min=100, count_budget_in_source=True)
        assert rig.config.round_cost == 6
        preload_ns = rig.t.monotonic_ns()
        for _ in range(90):
            await rig.budget.acquire()  # something else used 90 of the 100 starts
        rig.start()
        await rig.until_lines("oi-round", 1)
        await rig.until_lines("oi-catchup", 1)
        first = rig.lines_with("oi-catchup")[0]
        assert field(first, "pages") == "0" and field(first, "end") == "budget"  # a page would take the round's room
        assert page_calls(rig) == []
        wake_ns = min(rig.t.waiting)
        await next_cycle(rig, 2)
        round_two = [c for c in rig.source.hist_calls if c.at_ns >= wake_ns]
        assert [c.at_ns for c in round_two[:2]] == [wake_ns, wake_ns]  # the round's requests were granted at once
        await rig.until_lines("oi-catchup", 2)
        second = rig.lines_with("oi-catchup")[1]
        assert field(second, "pages") == "2" and field(second, "end") == "budget"  # only what is grantable at once
        starts = [preload_ns] * 90 + [c.at_ns for c in rig.source.hist_calls] + rig.source.time_calls
        assert max_starts_in_any_window(starts) <= 100
        await rig.stop_and_wait()

    run(scenario)


def test_a_full_budget_now_that_frees_before_the_next_wake_still_starts_no_page_and_no_time_call(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=(BTC,), budget_per_5min=100)
        for _ in range(100):
            await rig.budget.acquire()  # all 100 starts are in the window now and expire 300 s after the start
        rig.start()
        await rig.until_lines("oi-round", 1)
        await rig.until_lines("oi-catchup", 1)
        assert field(rig.lines_with("oi-catchup")[0], "end") == "budget"
        assert len(rig.source.time_calls) == 1  # the round's own; the window made none (it could not start at once)
        assert page_calls(rig) == []
        await rig.stop_and_wait()

    run(scenario)


def test_a_429_ends_the_window_and_the_next_round_goes_on(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=(BTC,))
        rig.source.errors = [None, OpenInterestPaused(300_000, 0)]  # the round's call, then the first page
        await first_window(rig)
        line = rig.lines_with("oi-catchup")[0]
        assert field(line, "end") == "paused" and field(line, "pages") == "0"
        assert len(page_calls(rig)) == 1  # nothing else was asked in the window
        await next_cycle(rig, 2)
        assert field(rig.lines_with("oi-round")[1], "ok") == "1"
        await rig.stop_and_wait()

    run(scenario)


def covering(rig: Rig, slot: int) -> list[Any]:
    return [c for c in rig.source.hist_calls if c.start_ms <= slot <= c.end_ms and c.end_ms - c.start_ms != 2 * STEP]


async def run_cycles(rig: Rig, count: int, rounds_so_far: int) -> int:
    for index in range(count):
        await next_cycle(rig, rounds_so_far + index + 1)
    return rounds_so_far + count


def test_a_slot_answered_without_its_row_is_asked_three_times_per_run_and_nothing_is_written_about_it(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=(BTC,))
        hole = (START_WALL - 10 * DAY) // STEP * STEP
        rig.source.never.add((BTC, hole))
        await first_window(rig)
        assert len(covering(rig, hole)) == 1  # try 1, inside the first big page
        rounds = await run_cycles(rig, 110, 1)  # about 9 hours: tries at 0, 1 h and 7 h, then none
        assert len(covering(rig, hole)) == 3
        assert hole not in stored_times(rig.db)
        assert len(rig.lines_with("oi-catchup-exhausted")) == 1
        assert table_rows(rig.db, "open_interest_conflicts") == []
        assert rounds == 111
        await rig.stop_and_wait()

    run(scenario)


def test_a_failed_page_is_not_a_try_and_a_restart_gives_one_new_try(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=(BTC,))
        hole = (START_WALL - 10 * DAY) // STEP * STEP
        rig.source.never.add((BTC, hole))
        # the catch-up page that covers the hole fails once, before any try
        original = rig.source.fetch_open_interest_hist
        failed = {"done": False}

        async def flaky(symbol: str, start: int, end: int, *, as_of_ms: int) -> Any:
            if start <= hole <= end and end - start != 2 * STEP and not failed["done"]:
                failed["done"] = True
                rig.source.hist_calls.append(
                    type(rig.source.hist_calls[0])(symbol, start, end, as_of_ms, rig.t.monotonic_ns())
                )
                raise SourceError("temporary")
            return await original(symbol, start, end, as_of_ms=as_of_ms)

        rig.source.fetch_open_interest_hist = flaky  # type: ignore[method-assign,assignment]
        await first_window(rig)
        assert field(rig.lines_with("oi-catchup")[0], "failures") == "1"
        await run_cycles(rig, 110, 1)
        assert len(covering(rig, hole)) == 4  # one failed call that was no try, then three tries
        await rig.stop_and_wait()
        # restart: a new task forgets the tries; the hole gets exactly one new try
        second = await make_rig(tmp_path, symbols=(BTC,), db=rig.db, t=rig.t)
        second.source.never.add((BTC, hole))
        second.start()
        await second.until_lines("oi-round", 1)
        await second.until_lines("oi-catchup", 1)
        for index in range(2, 6):
            await next_cycle(second, index)
        assert len(covering(second, hole)) == 1
        await second.stop_and_wait()

    run(scenario)


def test_page_errors_back_off_one_two_four_windows_and_then_suspend_the_symbol(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=(BTC, ETH))
        rig.source.page_error = (BTC, SourceError("down"))
        await first_window(rig)
        rounds = 1
        asked_in: list[int] = [1]
        seen = len([c for c in page_calls(rig) if c.symbol == BTC])
        for _ in range(12):
            rounds += 1
            await next_cycle(rig, rounds)
            await rig.t.settle()
            now = len([c for c in page_calls(rig) if c.symbol == BTC])
            if now > seen:
                asked_in.append(rounds)
                seen = now
        assert [b - a for a, b in itertools.pairwise(asked_in)] == [1, 2, 4]  # then no more
        assert len(rig.lines_with("oi-warning catchup-suspended")) == 1
        assert set(aligned_range(START_WALL - 5 * DAY, START_WALL - 4 * DAY)) <= set(stored_times(rig.db, ETH))
        await rig.stop_and_wait()

    run(scenario)


def test_a_page_that_hangs_is_cancelled_at_the_window_deadline_and_the_next_round_starts_on_time(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=(BTC,))
        rig.source.hang_after_calls = 1  # the round's call passes, the first page hangs for ever
        rig.start()
        await rig.until_lines("oi-round", 1)
        await rig.t.settle()
        (deadline_ns,) = rig.t.waiting  # only the window's deadline timer: the page hangs
        wake_ns = deadline_ns + 15_000 * NS_PER_MS  # the next round's wake is 15 s after the window deadline
        await rig.t.advance_ns(deadline_ns - rig.t.monotonic_ns())
        await rig.until_lines("oi-catchup", 1)
        assert field(rig.lines_with("oi-catchup")[0], "end") == "deadline"
        rig.source.hang_after_calls = None
        await rig.t.advance_ns(wake_ns - rig.t.monotonic_ns())
        await rig.until_lines("oi-round", 2)
        second_round = [c for c in rig.source.hist_calls if c.at_ns == wake_ns and c.end_ms - c.start_ms == 2 * STEP]
        assert len(second_round) == 1  # on time, right at its wake
        await rig.stop_and_wait()

    run(scenario)


def test_a_row_published_after_three_rounds_is_stored_by_the_catch_up_once_its_cooldown_is_over(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=(BTC,))
        prefill(
            rig.db, (BTC,), aligned_range(START_WALL - RETRIEVAL_HORIZON_MS - DAY, START_WALL - 20 * STEP), START_WALL
        )
        rig.start()
        await rig.until_lines("oi-round", 1)
        await rig.until_lines("oi-catchup", 1)
        target = round_boundary(rig.lines_with("oi-round")[0]) + STEP  # round 2 is the round of `target`
        rig.source.late[(BTC, target)] = target + 3 * STEP + 100_000  # after the third round that could have had it
        stored_at: int | None = None
        for rounds in range(2, 25):
            await next_cycle(rig, rounds)
            if target in stored_times(rig.db, BTC):
                stored_at = rounds
                break
        # rounds 2, 3 and 4 miss it; the window of round 3 asked for it (absent: try 1); try 2 is one hour (12 windows) later
        assert stored_at is not None and 14 <= stored_at <= 16
        assert rig.lines_with("oi-error") == []
        await rig.stop_and_wait()

    run(scenario)


def test_a_hole_that_failed_rounds_leave_is_found_by_the_very_next_window(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=(BTC,))
        prefill(
            rig.db, (BTC,), aligned_range(START_WALL - RETRIEVAL_HORIZON_MS - DAY, START_WALL - 20 * STEP), START_WALL
        )
        await first_window(rig)  # the window closes the gap up to what is expected
        first = round_boundary(rig.lines_with("oi-round")[0])
        missed = first + STEP
        assert missed not in stored_times(rig.db, BTC)
        rig.source.errors = [SourceError("down")] * 4  # rounds `missed` and `missed + 5 min`, attempt and retry each
        for rounds in (2, 3):  # each failing round retries once after one second
            await rig.advance_to_next_wake()
            await rig.t.settle()
            await rig.t.advance(1_000)
            await rig.until_lines("oi-round", rounds)
            await rig.t.settle()
        await rig.until_lines("oi-catchup", 2)
        assert missed in stored_times(rig.db, BTC)  # found by the window of the very round after, not an hour later
        assert any(c.start_ms == c.end_ms == missed for c in page_calls(rig))
        await rig.stop_and_wait()

    run(scenario)


def test_a_served_but_pending_row_is_not_a_try_and_is_stored_when_it_becomes_eligible(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=(BTC,), publication_delay_ms=1_500_000, publish_lag_ms=0)
        prefill(
            rig.db, (BTC,), aligned_range(START_WALL - RETRIEVAL_HORIZON_MS - DAY, START_WALL - 3_600_000), START_WALL
        )
        never = START_WALL // STEP * STEP - 11 * STEP  # a hole Binance really does not serve, in the same page
        rig.source.never.add((BTC, never))
        await first_window(rig)
        pending_slot = max(c.end_ms for c in page_calls(rig))  # the newest slot of the first page: Binance serves it
        assert pending_slot not in stored_times(rig.db, BTC)  # but it is not yet eligible, so it is not stored
        stored_at = None
        for rounds in range(2, 12):
            await next_cycle(rig, rounds)
            if pending_slot in stored_times(rig.db, BTC):
                stored_at = rounds
                break
        # asked in four or more windows: had a pending answer counted as a try, three tries would have ended it
        asked = [c for c in page_calls(rig) if c.start_ms <= pending_slot <= c.end_ms]
        assert stored_at is not None and len(asked) >= 4
        # while a page holds a row we could not judge yet, nothing in it is tried: the absent slot is asked every window
        assert len([c for c in asked if c.start_ms <= never <= c.end_ms]) >= 4
        await rig.stop_and_wait()

    run(scenario)


def test_an_unusable_writer_during_a_page_ends_the_task(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=(BTC,), recording=True)
        assert isinstance(rig.writer, RecordingWriter)
        rig.writer.errors = [
            None,
            OpenInterestPageError(
                OpenInterestWriteError("writer is closed"), 0, OpenInterestWriteReport(0, 0, (), (), (), 0, 0)
            ),
        ]
        rig.start()
        assert rig.task is not None
        with pytest.raises(OpenInterestWriterUnusable):
            await asyncio.wait_for(rig.task, 5)

    run(scenario)


def test_a_window_longer_than_the_time_trust_keeps_storing_with_its_single_bound(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=(BTC,))
        rig.source.hist_delay_ms = 70_000  # each page takes longer than the 60 s the trust gate allows
        rig.start()
        await rig.until_lines("oi-round", 1)
        await rig.t.settle()
        await rig.t.advance(70_000)  # page 1 ends; page 2 starts 70 s after the window's time observation
        await rig.t.settle()
        before = len(stored_times(rig.db, BTC))
        assert before >= 500
        await rig.t.advance(70_000)
        await rig.t.settle()
        assert len(stored_times(rig.db, BTC)) >= before + 500  # page 2 stored its rows although the trust had expired
        assert rig.lines_with("oi-warning") == []
        await rig.stop_and_wait()

    run(scenario)


def test_a_failing_read_stops_the_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=(BTC,), recording=True)

        async def broken(*args: Any, **kwargs: Any) -> Any:
            raise OpenInterestReadError("disk gone")

        monkeypatch.setattr(task_module, "read_series", broken)
        rig.start()
        assert rig.task is not None
        with pytest.raises(OpenInterestReadError):
            await asyncio.wait_for(rig.task, 5)

    run(scenario)


def test_one_time_request_per_round_and_one_per_window_even_when_nothing_is_to_be_fetched(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=(BTC,))
        await first_window(rig)
        assert len(rig.source.time_calls) == 2  # the round's and the window's
        for rounds in range(2, 7):
            await next_cycle(rig, rounds)
        assert len(rig.source.time_calls) == 2 * 6
        await rig.stop_and_wait()

    run(scenario)


def test_with_catch_up_running_from_the_start_no_window_exceeds_the_round_cost_or_the_budget(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=(BTC, ETH), count_budget_in_source=True)
        rig.align_to_next_wake()
        rig.start()
        await rig.until_lines("oi-round", 1)
        await rig.until_lines("oi-catchup", 1)
        for rounds in range(2, 9):
            await next_cycle(rig, rounds)
        non_page = rig.source.time_calls + [c.at_ns for c in rig.source.hist_calls if c.end_ms - c.start_ms == 2 * STEP]
        everything = rig.source.time_calls + [c.at_ns for c in rig.source.hist_calls]
        assert max_starts_in_any_window(non_page) <= rig.config.round_cost
        assert max_starts_in_any_window(everything) <= rig.config.budget_per_5min
        await rig.stop_and_wait()

    run(scenario)


def test_holes_that_cannot_be_reached_before_they_leave_the_horizon_are_named_in_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(task_module, "backfill_share", lambda symbol_count, budget: 1)
        rig = await make_rig(tmp_path, symbols=(BTC, ETH))
        await first_window(rig)
        warnings = rig.lines_with("oi-warning holes-will-leave-the-horizon")
        assert len(warnings) == 1 and f"symbol={ETH}" in warnings[0]  # BTC's first page is reachable, ETH's is the 2nd
        assert int(field(warnings[0], "slots")) >= 1
        await rig.stop_and_wait()

    run(scenario)


def test_with_enough_capacity_no_warning_is_logged(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=(BTC, ETH))
        await first_window(rig)
        assert rig.lines_with("oi-warning") == []
        await rig.stop_and_wait()

    run(scenario)


def test_stopping_mid_catch_up_and_starting_again_converges_and_the_next_start_adds_nothing(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = ManualTime()
        db: Path | None = None
        rig = await make_rig(tmp_path, symbols=(BTC,), t=t)
        db = rig.db
        rig.source.hist_delay_ms = 20_000  # a window fits only a few pages
        rig.start()
        await rig.until_lines("oi-round", 1)
        await t.advance(20_000)
        await t.settle()
        await rig.stop_and_wait()
        partial = len(stored_times(db, BTC))
        runs = 1
        while True:
            runs += 1
            assert runs < 12
            rig = await make_rig(tmp_path, symbols=(BTC,), t=t, db=db)
            rig.start()
            await rig.until_lines("oi-round", 1)
            await rig.until_lines("oi-catchup", 1)
            await rig.stop_and_wait()
            if "end=no-pages" in rig.lines_with("oi-catchup")[-1]:
                break
        assert len(stored_times(db, BTC)) > partial
        final_rows = table_rows(db)
        total = rig.lines_with("oi-reconcile total")
        as_of = int(field(total[0], "as_of")) if total else t.wall_ms()
        assert set(aligned_range(oldest_allowed_ms(as_of, P5), as_of - 600_000)) <= set(stored_times(db, BTC))
        third = await make_rig(tmp_path, symbols=(BTC,), t=t, db=db)
        third.start()
        await third.until_lines("oi-round", 1)
        await t.settle()
        await third.stop_and_wait()
        assert page_calls(third) == [] or all(c.start_ms == c.end_ms for c in page_calls(third))
        assert table_rows(db, "open_interest_conflicts") == []
        assert len(table_rows(db)) >= len(final_rows)

    run(scenario)


class _Boom(FaultyConnection):
    pass


def faulty_connect(made: list[FaultyConnection], *faults: Fault) -> Callable[[Path, int], sqlite3.Connection]:
    def connect(path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
        conn = sqlite3.connect(path, factory=FaultyConnection, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA recursive_triggers=ON")
        conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        conn.faults.extend(faults)
        made.append(conn)
        return conn

    return connect


def test_an_unknown_write_outcome_then_the_next_try_leaves_no_duplicate_and_no_conflict(tmp_path: Path) -> None:
    async def scenario() -> None:
        made: list[FaultyConnection] = []
        fault = Fault("COMMIT", sqlite3.OperationalError("disk I/O error"), nth=4, after_execution=True)
        rig = await make_rig(tmp_path, symbols=(BTC,), connect=faulty_connect(made, fault))
        await first_window(rig)
        assert (
            rig.lines_with("oi-warning catchup-page-failed")
            and "outcome-unknown" in rig.lines_with("oi-warning catchup-page-failed")[0]
        )
        await next_cycle(rig, 2)  # the symbol backs off one window, then the page is sent again
        await next_cycle(rig, 3)
        rows = stored_times(rig.db, BTC)
        assert len(rows) == len(set(rows))
        assert table_rows(rig.db, "open_interest_conflicts") == []
        total = rig.lines_with("oi-reconcile total")[0]
        as_of = int(field(total, "as_of"))
        assert set(aligned_range(oldest_allowed_ms(as_of, P5), as_of - 600_000)) <= set(rows)
        await rig.stop_and_wait()

    run(scenario)


class _BlockingBegin(FaultyConnection):
    reached: ClassVar[threading.Event] = threading.Event()
    release: ClassVar[threading.Event] = threading.Event()
    begins: ClassVar[int] = 0

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if sql.startswith("BEGIN IMMEDIATE"):
            type(self).begins += 1
            if type(self).begins == 3:
                type(self).reached.set()
                type(self).release.wait(10)
        return super().execute(sql, *args)


def test_cancelled_in_the_middle_of_a_page_leaves_whole_transactions_and_a_new_run_completes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        _BlockingBegin.reached, _BlockingBegin.release, _BlockingBegin.begins = threading.Event(), threading.Event(), 0

        def connect(path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
            conn = sqlite3.connect(path, factory=_BlockingBegin, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA recursive_triggers=ON")
            conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            return conn

        t = ManualTime(wait_for_threads=False)
        rig = await make_rig(tmp_path, symbols=(BTC,), t=t, connect=connect)
        rig.start()
        await rig.until_lines("oi-round", 1)
        for _ in range(500):  # real time: the writer thread reaches its third transaction
            if _BlockingBegin.reached.is_set():
                break
            await asyncio.sleep(0.01)
        assert _BlockingBegin.reached.is_set()
        rig.stop.set()
        assert rig.task is not None
        await rig.task  # cancelled between transactions: nothing is left running
        _BlockingBegin.release.set()
        await rig.writer.close()
        page = page_calls(rig)[0]
        inside = [s for s in stored_times(rig.db, BTC) if page.start_ms <= s <= page.end_ms]
        assert 0 < len(inside) < 500 and len(inside) % 100 == 0  # whole transactions of 100 rows only
        again = await make_rig(tmp_path, symbols=(BTC,), t=ManualTime(), db=rig.db)
        await first_window(again)
        assert [s for s in stored_times(again.db, BTC) if page.start_ms <= s <= page.end_ms] == aligned_range(
            page.start_ms, page.end_ms
        )
        assert table_rows(again.db, "open_interest_conflicts") == []
        await again.stop_and_wait()

    run(scenario)


def test_a_successful_page_resets_the_failure_count(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=(BTC,))
        base = START_WALL // STEP * STEP
        gaps = [base - 2_000 * STEP, base - 1_000 * STEP]  # more than 500 slots apart: two pages
        slots = [s for s in aligned_range(START_WALL - RETRIEVAL_HORIZON_MS - DAY, base - 20 * STEP) if s not in gaps]
        prefill(rig.db, (BTC,), slots, START_WALL)
        rig.source.page_script = [True, False, True]  # page 1 fails, page 1 ok, page 2 fails, then all succeed
        await first_window(rig)
        assert len(rig.lines_with("oi-warning catchup-page-failed")) == 1
        await next_cycle(rig, 2)
        await rig.until_lines("oi-catchup", 2)
        assert gaps[0] in stored_times(rig.db, BTC) and gaps[1] not in stored_times(rig.db, BTC)
        # the success reset the count: the second failure waits one window, not two
        await next_cycle(rig, 3)
        await rig.until_lines("oi-catchup", 3)
        assert gaps[1] in stored_times(rig.db, BTC)
        await rig.stop_and_wait()

    run(scenario)
