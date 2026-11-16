"""The shared retry reserve of an open interest round (ADR-0003 P-10, A-6; INV-146, INV-148).

A round of ``n`` symbols hands out at most ``min(50, n)`` retry tickets. A ticket is taken by one synchronous
check-and-increment on attempt 2, inside the request slot, after the paused/stop check; it is never returned.

The concurrency test makes 200 retries reach the check in one scheduler turn (no slot limit, one shared release of the
retry delay), so it fails for a check, then an ``await``, then the increment. The synchronous test pins that
``try_take_retry`` is not a coroutine function, so no ``await`` can be put inside it.
"""

import asyncio
import inspect
from collections import Counter
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.collector import open_interest_task
from quasar_market_data.collector.async_open_interest_writer import OpenInterestPageError, OpenInterestWriteReport
from quasar_market_data.collector.binance_usdm_open_interest import OpenInterestRequestRefused
from quasar_market_data.collector.extension_pause import ExtensionPaused
from quasar_market_data.collector.open_interest_task import (
    OpenInterestSymbolOutcome,
    _OpenInterestTask,
    _RoundState,
)
from quasar_market_data.collector.open_interest_writer import OpenInterestWriteBusy
from tests.collector.manual_time import NS_PER_MS
from tests.collector.open_interest_task_fakes import STEP, RecordingWriter, Rig, make_rig, symbols_of

OFFSET = 200_000


@pytest.fixture(autouse=True)
def no_catch_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only rounds are under test here: the idle window (catch-up) makes no request."""

    async def nothing(self: Any, wake_ns: int, wake_ms: int) -> None:
        return None

    monkeypatch.setattr(_OpenInterestTask, "_idle_window", nothing)


@pytest.fixture
def states(monkeypatch: pytest.MonkeyPatch) -> list[_RoundState]:
    """Every ``_RoundState`` the task builds, in round order."""
    built: list[_RoundState] = []
    original = open_interest_task._RoundState

    def record(*args: Any, **kwargs: Any) -> _RoundState:
        state = original(*args, **kwargs)
        built.append(state)
        return state

    monkeypatch.setattr(open_interest_task, "_RoundState", record)
    return built


def run(scenario: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(scenario())


def field(line: str, name: str) -> int:
    return int(next(part for part in line.split() if part.startswith(f"{name}=")).split("=")[1])


def retried(rig: Rig) -> set[str]:
    """The symbols asked a second time for the same range."""
    counts = Counter((call.symbol, call.end_ms) for call in rig.source.hist_calls)
    return {symbol for (symbol, _), count in counts.items() if count >= 2}


def max_starts_in_any_window(starts: list[int]) -> int:
    ordered = sorted(starts)
    worst, low = 0, 0
    for high, instant in enumerate(ordered):
        while ordered[low] <= instant - 300_000 * NS_PER_MS:
            low += 1
        worst = max(worst, high - low + 1)
    return worst


async def finish_rounds(rig: Rig, count: int, *, step_ms: int = 500) -> None:
    """Advance time in steps (the retry delay, slow failures) until ``count`` round lines are out."""
    for _ in range(1_000):
        await rig.t.settle()
        if len(rig.lines_with("oi-round")) >= count:
            return
        await rig.t.advance(step_ms)
    raise AssertionError(f"expected {count} oi-round lines, have {rig.lines_with('oi-round')}")


async def two_hundred_symbol_rig(tmp_path: Path, failing: int) -> Rig:
    rig = await make_rig(tmp_path, symbols=symbols_of(200), recording=True, count_budget_in_source=True)
    assert rig.config.cadence == 1  # every symbol in every round: n = 200, reserve = 50
    rig.source.fail_symbols = set(symbols_of(200)[:failing])
    return rig


def test_sixty_failures_take_fifty_tickets(tmp_path: Path, states: list[_RoundState]) -> None:
    async def scenario() -> None:
        rig = await two_hundred_symbol_rig(tmp_path, failing=60)
        rig.start()
        await finish_rounds(rig, 1)
        (state,) = states
        assert state.retry_limit == 50 and state.retries_taken == 50
        assert len(retried(rig)) == 50
        assert retried(rig) <= rig.source.fail_symbols
        line = rig.lines_with("oi-round")[0]
        assert "retry_reserve_exhausted=10" in line.split()
        assert field(line, "ok") == 190 and field(line, "failed") == 0
        exhausted = [s for s, o in state.outcomes.items() if o is OpenInterestSymbolOutcome.RETRY_RESERVE_EXHAUSTED]
        assert len(exhausted) == 10 and not set(exhausted) & retried(rig)
        warnings = rig.lines_with("oi-warning symbol-not-retried")
        assert len(warnings) == 10
        assert all(f"boundary={field(line, 'boundary')}" in w and "SourceError" in w for w in warnings)
        assert {w.split("symbol=")[1].split()[0] for w in warnings} == set(exhausted)
        await rig.stop_and_wait()

    run(scenario)


def test_ten_failures_are_all_retried(tmp_path: Path, states: list[_RoundState]) -> None:
    async def scenario() -> None:
        rig = await two_hundred_symbol_rig(tmp_path, failing=10)
        rig.start()
        await finish_rounds(rig, 1)
        assert states[0].retries_taken == 10
        assert retried(rig) == rig.source.fail_symbols
        line = rig.lines_with("oi-round")[0]
        assert field(line, "ok") == 200 and field(line, "retry_reserve_exhausted") == 0
        assert rig.lines_with("oi-warning symbol-not-retried") == []
        await rig.stop_and_wait()

    run(scenario)


@pytest.mark.parametrize("fail_delay_ms", [0, 40])
def test_tickets_are_atomic_under_concurrency(
    tmp_path: Path, states: list[_RoundState], monkeypatch: pytest.MonkeyPatch, fail_delay_ms: int
) -> None:
    """All 200 attempts 2 reach the ticket check in the same scheduler turn, so a non-atomic check races.

    The slot limit is lifted (``MAX_IN_FLIGHT`` = 1000) and the retry delay waits on one shared event that is released
    only when all 200 failed symbols are waiting: every retry then enters its slot in the same turn, with nothing to
    pace it before the check. A check with an ``await`` before the increment would hand out 200 tickets here.
    """
    released = asyncio.Event()
    waiting = [0]
    original_sleep_or_stop = open_interest_task.sleep_or_stop

    async def retry_delay(sleep: Any, ms: int, stop: asyncio.Event) -> bool:
        if ms != open_interest_task.RETRY_DELAY_MS:
            return await original_sleep_or_stop(sleep, ms, stop)
        waiting[0] += 1
        if waiting[0] == 200:
            released.set()
        await released.wait()
        return stop.is_set()

    monkeypatch.setattr(open_interest_task, "MAX_IN_FLIGHT", 1_000)
    monkeypatch.setattr(open_interest_task, "sleep_or_stop", retry_delay)

    async def scenario() -> None:
        rig = await two_hundred_symbol_rig(tmp_path, failing=200)
        rig.source.fail_delay_ms = fail_delay_ms
        rig.start()
        await finish_rounds(rig, 1, step_ms=200)
        assert waiting[0] == 200 and released.is_set()  # every retry was held back and released together
        (state,) = states
        assert state.retries_taken == 50
        assert len(retried(rig)) == 50
        exhausted = [s for s, o in state.outcomes.items() if o is OpenInterestSymbolOutcome.RETRY_RESERVE_EXHAUSTED]
        assert len(exhausted) == 150
        starts = [call.at_ns for call in rig.source.hist_calls] + rig.source.time_calls
        assert len(starts) == 200 + 50 + 1
        assert max_starts_in_any_window(starts) <= 200 + 50 + 1
        await rig.stop_and_wait()

    run(scenario)


def test_try_take_retry_is_synchronous() -> None:
    assert not inspect.iscoroutinefunction(_RoundState.try_take_retry)
    state = _RoundState(symbols_of(200), retry_limit=50)
    assert [state.try_take_retry() for _ in range(50)] == [True] * 50
    assert [state.try_take_retry() for _ in range(5)] == [False] * 5
    assert state.retries_taken == 50  # never above the limit, never given back


def test_pause_refusal_and_absence_take_no_ticket(tmp_path: Path, states: list[_RoundState]) -> None:
    async def scenario() -> None:
        symbols = symbols_of(10)
        rig = await make_rig(tmp_path, symbols=symbols, recording=True, count_budget_in_source=True)
        # round 1: a refusal and an absent row are final; neither is retried nor takes a ticket
        rig.source.symbol_errors = {symbols[0]: OpenInterestRequestRefused("range too old")}
        rig.source.publish_lag_ms = 10**9  # no row is published yet: every symbol is answered without its rows
        rig.start()
        await finish_rounds(rig, 1)
        line = rig.lines_with("oi-round")[0]
        assert field(line, "refused") == 1 and field(line, "ok") == 9
        assert states[0].retries_taken == 0 and retried(rig) == set()
        # round 2: S000 fails its first attempt, S001 answers 429; S000's retry finds the round paused
        rig.source.symbol_errors = {symbols[1]: ExtensionPaused(300_000, rig.t.monotonic_ns() + 300_000 * NS_PER_MS)}
        rig.source.fail_symbols = {symbols[0]}
        await rig.advance_to_next_wake()
        await finish_rounds(rig, 2)
        line = rig.lines_with("oi-round")[1]
        assert field(line, "paused") == 10
        assert states[1].retries_taken == 0
        await rig.stop_and_wait()

    run(scenario)


def test_a_busy_write_takes_a_ticket(tmp_path: Path, states: list[_RoundState]) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=("BTC/USDT:USDT",), recording=True, count_budget_in_source=True)
        assert isinstance(rig.writer, RecordingWriter)
        rig.writer.errors = [
            OpenInterestPageError(OpenInterestWriteBusy("busy"), 0, OpenInterestWriteReport(0, 0, (), (), (), 0, 0))
        ]
        rig.start()
        await finish_rounds(rig, 1)
        assert states[0].retry_limit == 1 and states[0].retries_taken == 1
        assert field(rig.lines_with("oi-round")[0], "ok") == 1
        await rig.stop_and_wait()

    run(scenario)


def test_a_ticket_taken_before_a_deadline_cancel_stays_taken(tmp_path: Path, states: list[_RoundState]) -> None:
    async def scenario() -> None:
        rig = await make_rig(tmp_path, symbols=("BTC/USDT:USDT",), recording=True, count_budget_in_source=True)
        rig.source.fail_symbols = {"BTC/USDT:USDT"}
        rig.source.hang_after_calls = 1  # the retry hangs until the round deadline cancels it
        rig.align_to_next_wake()
        wake = rig.t.wall_ms()
        rig.start()
        await rig.t.settle()
        await rig.t.advance(1_000)  # the retry delay: the retry takes its ticket and hangs
        assert len(rig.source.hist_calls) == 2
        assert states[0].retries_taken == 1
        await rig.t.advance(wake + STEP - 15_000 - rig.t.wall_ms())  # the round deadline
        await rig.until_lines("oi-round", 1)
        assert field(rig.lines_with("oi-round")[0], "deadline") == 1
        assert states[0].retries_taken == 1  # the cancel does not give the ticket back
        await rig.stop_and_wait()

    run(scenario)


def test_a_new_round_starts_with_no_tickets_taken(tmp_path: Path, states: list[_RoundState]) -> None:
    async def scenario() -> None:
        symbols = symbols_of(10)
        rig = await make_rig(tmp_path, symbols=symbols, recording=True, count_budget_in_source=True)
        rig.source.fail_symbols = set(symbols)  # every first request of every range fails: 10 tickets per round
        rig.start()
        await finish_rounds(rig, 1)
        await rig.advance_to_next_wake()
        await finish_rounds(rig, 2)
        assert [(s.retry_limit, s.retries_taken) for s in states] == [(10, 10), (10, 10)]
        assert all(field(line, "ok") == 10 for line in rig.lines_with("oi-round"))
        await rig.stop_and_wait()

    run(scenario)
