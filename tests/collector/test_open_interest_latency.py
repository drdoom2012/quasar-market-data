"""Open interest latency at the shipped subset (ADR-0003 A-6, spec section 2.6, verification 1; P-10 "R1 latency").

Every test is a simulation on a fake clock: a measurement of the code on a model of the network, not of Binance. It
runs the real ``OpenInterestTask`` with the real ``OpenInterestBudget`` (every start counted in the source) at
``S = 200``, ``b = 400`` (``c = 1``, ``n = 200``, reserve 50, round cost 252, backfill share 148),
``round_offset_ms = 200 000``. A successful round request holds its slot 480 ms (``round_delay_ms``); a failing one
holds it 480 ms plus ``fail_delay_ms``, and the spec's ``f`` is that whole slot time: a fast failure 500 ms
(``f ~ 0.5 s``), a slow one 10 s (``f = 10 s``, CCXT's default timeout). A retry queues behind every first attempt of
the round, so only first-attempt failures delay the last symbol. The writer is the recording fake of the round tests:
its stamp is the fake wall clock at the write. "Readable" is that stamp plus ``READABLE_BOUND_MS``. ``e`` is the period end of the measured row;
R1 asks for it readable by ``e + 600 s``.

Unless a test needs holes, the database holds every slot of the 1-day horizon up to ``e - 5 min``, so the backfill has
nothing to do in the first idle window (an ideal steady state; the recording writer stores nothing in the database).
The late path publishes the last symbol in sorted order between ``e + 260 s`` and ``e + 490 s``: after its own request
in the round of ``e``, before the next round.
"""

import asyncio
import shutil
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.collector.open_interest_writer import OpenInterestWriter
from quasar_market_data.domain.open_interest import OpenInterest, OpenInterestKey
from quasar_market_data.domain.readability import READABLE_BOUND_MS
from quasar_market_data.storage.connection import open_writer
from tests.collector.manual_time import NS_PER_MS, ManualTime
from tests.collector.open_interest_task_fakes import P5, STEP, VENUE, Rig, make_rig, migrated_db, symbols_of, values_for

SYMBOLS = symbols_of(200)
LAST = SYMBOLS[-1]  # the last symbol in sorted order: its request is the round's last
OFFSET_MS = 200_000
ROUND_DELAY_MS = 480  # a successful round request: the measured 4.2 symbols/s at 2 slots
FAST_FAIL_MS = 20  # with the 480 ms of every round request: f ~ 0.5 s of slot time (A-6 ruling 10)
SLOW_FAIL_MS = 9_520  # with the 480 ms of every round request: f = 10 s of slot time, CCXT's default timeout (D-6)
LATE_PUBLICATION_S = 300  # inside [e + 260 s, e + 490 s]
START_WALL = ManualTime().wall_ms()
E = ((START_WALL - OFFSET_MS) // STEP + 1) * STEP  # the first round's boundary (the rig aligns to its wake)


def run(scenario: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(scenario())


@pytest.fixture(scope="module")
def steady_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Every slot from more than a day before ``E`` up to ``E - 5 min``, stamped well before the first round."""
    at = START_WALL - 10 * 86_400_000
    db = migrated_db(tmp_path_factory.mktemp("steady"), at)
    conn = open_writer(db)
    writer = OpenInterestWriter(conn, wall_ms=lambda: at + 1)
    slots = range(E - 86_400_000 - 12 * STEP, E, STEP)
    try:
        for symbol in SYMBOLS:
            rows = [OpenInterest(OpenInterestKey(VENUE, symbol, P5, slot), *values_for(slot)) for slot in slots]
            for start in range(0, len(rows), 100):
                writer.write_transaction(rows[start : start + 100])
    finally:
        writer.close()
    return db


async def shipped_rig(tmp_path: Path, steady: Path | None) -> Rig:
    """The rig at S = 200, b = 400 on a copy of ``steady`` (or an empty database, all holes, 7-day horizon)."""
    t = ManualTime(idle_settle=True)  # settle still waits for the database reads in worker threads
    if steady is None:
        db = migrated_db(tmp_path, START_WALL - 10 * 86_400_000)
    else:
        db = tmp_path / "market.db"
        shutil.copy(steady, db)
    rig = await make_rig(
        tmp_path,
        symbols=SYMBOLS,
        budget_per_5min=400,
        round_offset_ms=OFFSET_MS,
        count_budget_in_source=True,
        db=db,
        t=t,
        recording=True,
        backfill_days=1 if steady is not None else 7,
    )
    assert rig.config.cadence == 1 and rig.config.round_cost == 252
    rig.source.round_delay_ms = ROUND_DELAY_MS
    rig.align_to_next_wake()
    assert rig.t.wall_ms() == E + OFFSET_MS
    return rig


async def until_rounds(rig: Rig, count: int) -> None:
    """Advance the fake clock in 1 s steps until ``count`` rounds have ended."""
    for _ in range(2_000):
        await rig.t.settle()
        if len(rig.lines_with("oi-round")) >= count:
            return
        await rig.t.advance(1_000)
    raise AssertionError(f"round {count} did not end: {rig.lines_with('oi-round')}")


async def finish(rig: Rig) -> None:
    await rig.stop_and_wait()


def readable_s(rig: Rig, symbol: str, slot: int = E) -> float | None:
    """Readable-by of the stored row, as seconds after ``slot``; ``None`` when the row is not stored."""
    stamp = rig.writer.stored.get((symbol, slot))
    return None if stamp is None else (stamp + READABLE_BOUND_MS - slot) / 1_000


def latest_readable_s(rig: Rig, slot: int = E) -> float:
    values = [readable_s(rig, symbol, slot) for symbol in SYMBOLS]
    return max(value for value in values if value is not None)


def publish_last_late(rig: Rig) -> None:
    rig.source.late[(LAST, E)] = E + LATE_PUBLICATION_S * 1_000  # server time


def report(name: str, value: float | None) -> None:
    print(f"latency {name}: readable by e + {value} s")


# ---- ideal runs ---------------------------------------------------------------------------------------------------


def test_ideal_early_readable_by_250_s(tmp_path: Path, steady_db: Path) -> None:
    """Ideal, published before the request (p <= 48 s): readable by e + 250 s.

    Zero margin: on the model both slots finish together, at exactly e + 250.0 s (the documents say so).

    Simulation on a fake clock: a measurement of the code on a model of the network, not of Binance.
    """

    async def scenario() -> None:
        rig = await shipped_rig(tmp_path, steady_db)
        rig.start()
        await until_rounds(rig, 1)
        value = latest_readable_s(rig)
        report("ideal early", value)
        assert all(readable_s(rig, symbol) is not None for symbol in SYMBOLS)
        assert value <= 250
        await finish(rig)

    run(scenario)


def test_ideal_late_readable_by_550_s(tmp_path: Path, steady_db: Path) -> None:
    """Ideal, published after the request: readable by e + 550 s.

    Zero margin: on the model the late row is readable at exactly e + 550.0 s (the documents say so).

    Simulation on a fake clock: a measurement of the code on a model of the network, not of Binance.
    """

    async def scenario() -> None:
        rig = await shipped_rig(tmp_path, steady_db)
        publish_last_late(rig)
        rig.start()
        await until_rounds(rig, 1)
        assert readable_s(rig, LAST) is None  # not yet published at its request
        await until_rounds(rig, 2)
        value = readable_s(rig, LAST)
        report("ideal late", value)
        assert value is not None and value <= 550
        await finish(rig)

    run(scenario)


# ---- request failures -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("failures", [60, 200])
def test_fast_failures_any_count_readable_by_265_and_565_s(tmp_path: Path, steady_db: Path, failures: int) -> None:
    """F fast failures (f ~ 0.5 s), reserve exhausted or not: every row a round stores, retries included, is readable
    by e + 265 s on the early path (e + 565 s on the late path). The late-published last symbol, whose request is
    delayed by first-attempt failures only, is readable by e + 552 s (p' <= 50 s): retries do not delay it.

    The first F symbols in sorted order fail their first attempt in both rounds. With F = 200 the last symbol fails
    too and gets no ticket in either round, so its late row is not stored by the next round (the exhausted lines).

    Simulation on a fake clock: a measurement of the code on a model of the network, not of Binance.
    """

    async def scenario() -> None:
        rig = await shipped_rig(tmp_path, steady_db)
        rig.source.fail_symbols = set(SYMBOLS[:failures])
        rig.source.fail_delay_ms = FAST_FAIL_MS
        publish_last_late(rig)
        rig.start()
        await until_rounds(rig, 1)
        early = latest_readable_s(rig)
        report(f"F = {failures} fast, early (latest stored row)", early)
        stored = sum(1 for symbol in SYMBOLS if readable_s(rig, symbol) is not None)
        late_unpublished = 0 if LAST in rig.source.fail_symbols else 1  # the last symbol's row is published late
        # every first-attempt success but the late-published last symbol, and the 50 retries
        assert stored == len(SYMBOLS) - failures - late_unpublished + min(failures, 50)
        assert early <= 265
        await until_rounds(rig, 2)
        late = readable_s(rig, LAST)
        report(f"F = {failures} fast, late (last symbol)", late)
        if failures < len(SYMBOLS):
            assert late is not None and late <= 552
        else:
            assert late is None
            assert any(LAST in line for line in rig.lines_with("oi-warning symbol-not-retried"))
        await finish(rig)

    run(scenario)


async def slow_failures_on_the_late_path(tmp_path: Path, steady: Path, failures: int) -> float | None:
    """A healthy round of e, then F slow first-attempt failures in the round that serves the late path."""
    rig = await shipped_rig(tmp_path, steady)
    publish_last_late(rig)
    rig.start()
    await until_rounds(rig, 1)
    rig.source.fail_symbols = set(SYMBOLS[:failures])
    rig.source.fail_delay_ms = SLOW_FAIL_MS
    await until_rounds(rig, 2)
    value = readable_s(rig, LAST)
    report(f"F = {failures} slow, late", value)
    await finish(rig)
    return value


def test_five_slow_failures_keep_the_late_path_by_600_s(tmp_path: Path, steady_db: Path) -> None:
    """F = 5 slow failures (10 s) in the round that serves the late path: met (p' ~ 71.8 s estimated), by e + 600 s.

    Simulation on a fake clock: a measurement of the code on a model of the network, not of Binance.
    """

    async def scenario() -> None:
        value = await slow_failures_on_the_late_path(tmp_path, steady_db, 5)
        assert value is not None and value <= 600

    run(scenario)


def test_ten_slow_failures_keep_the_late_path_by_600_s(tmp_path: Path, steady_db: Path) -> None:
    """F = 10 slow failures (10 s): the threshold, met (p' ~ 95.6 s estimated, e + 597.6 s), readable by e + 600 s.

    Simulation on a fake clock: a measurement of the code on a model of the network, not of Binance.
    """

    async def scenario() -> None:
        value = await slow_failures_on_the_late_path(tmp_path, steady_db, 10)
        assert value is not None and value <= 600

    run(scenario)


def test_eleven_slow_failures_miss_the_late_path(tmp_path: Path, steady_db: Path) -> None:
    """F = 11 slow failures (10 s), one above the threshold: missed (p' ~ 100.4 s estimated), readable after e + 600 s.

    Simulation on a fake clock: a measurement of the code on a model of the network, not of Binance.
    """

    async def scenario() -> None:
        value = await slow_failures_on_the_late_path(tmp_path, steady_db, 11)
        assert value is not None and 600 < value <= 660

    run(scenario)


@pytest.mark.parametrize(("failures", "deadline_first"), [(49, False), (50, True)])
def test_slow_failures_before_the_last_symbol_reach_the_deadline_from_50(
    tmp_path: Path, steady_db: Path, failures: int, deadline_first: bool
) -> None:
    """The edge of the deadline line, f = 10 s: with 49 slow first-attempt failures the last symbol's first attempt
    still ends inside the round deadline (285 s after the wake, p ~ 281.2 s estimated); with 50 it does not
    (p ~ 286 s) and the symbol ends ``deadline``.

    Simulation on a fake clock: a measurement of the code on a model of the network, not of Binance.
    """

    async def scenario() -> None:
        rig = await shipped_rig(tmp_path, steady_db)
        rig.source.fail_symbols = set(SYMBOLS[:failures])
        rig.source.fail_delay_ms = SLOW_FAIL_MS
        rig.start()
        await until_rounds(rig, 1)
        value = readable_s(rig, LAST)
        report(f"F = {failures} slow, early, round of e", value)
        unfinished = " ".join(rig.lines_with("oi-warning round-deadline"))  # retries may reach the deadline either way
        if deadline_first:
            assert value is None and f"{LAST}:" in unfinished
        else:
            assert value is not None and value <= 200 + 285 + 2 and f"{LAST}:" not in unfinished
        await finish(rig)

    run(scenario)


def test_sixty_slow_failures_reach_the_deadline_and_the_next_round_serves_early_rows(
    tmp_path: Path, steady_db: Path
) -> None:
    """F = 60 slow failures (10 s): the round reaches its deadline, unfinished symbols end ``deadline``, and a healthy
    next round stores their early-published rows within R1 (e + 500 s + p').

    Simulation on a fake clock: a measurement of the code on a model of the network, not of Binance.
    """

    async def scenario() -> None:
        rig = await shipped_rig(tmp_path, steady_db)
        rig.source.fail_symbols = set(SYMBOLS[:60])
        rig.source.fail_delay_ms = SLOW_FAIL_MS
        rig.start()
        await until_rounds(rig, 1)
        warning = rig.lines_with("oi-warning round-deadline")
        assert len(warning) == 1 and f"{LAST}:" in warning[0]
        assert readable_s(rig, LAST) is None
        rig.source.fail_symbols = set()  # the next round is healthy
        await until_rounds(rig, 2)
        value = readable_s(rig, LAST)
        report("F = 60 slow, early row of an unfinished symbol", value)
        assert value is not None and value <= 600
        await finish(rig)

    run(scenario)


# ---- retry reserve exhaustion -----------------------------------------------------------------------------------

EXHAUSTING = {*SYMBOLS[:50], LAST}  # 51 fast failures: the first 50 retries take every ticket, the last gets none


def test_reserve_exhausted_row_comes_from_the_next_round_by_567_s(tmp_path: Path, steady_db: Path) -> None:
    """Reserve exhausted, the unretried symbol's next request succeeds: its row comes from the next round, by e + 567 s.

    Simulation on a fake clock: a measurement of the code on a model of the network, not of Binance.
    """

    async def scenario() -> None:
        rig = await shipped_rig(tmp_path, steady_db)
        rig.source.fail_symbols = set(EXHAUSTING)
        rig.source.fail_delay_ms = FAST_FAIL_MS
        rig.start()
        await until_rounds(rig, 1)
        assert any(LAST in line for line in rig.lines_with("oi-warning symbol-not-retried"))
        assert readable_s(rig, LAST) is None
        rig.source.fail_symbols = set()
        await until_rounds(rig, 2)
        value = readable_s(rig, LAST)
        report("reserve exhausted, next succeeds", value)
        assert value is not None and value <= 567
        await finish(rig)

    run(scenario)


def test_exhausted_and_next_request_failed_is_absent_before_800_s(tmp_path: Path, steady_db: Path) -> None:
    """Reserve exhausted, and the symbol's next round also fails: e + 800 s + p, missed (fail closed).

    The symbol's requests keep failing (fast) until after the next round and its idle window, so neither the round
    nor the backfill can store the row before the round after.

    Simulation on a fake clock: a measurement of the code on a model of the network, not of Binance.
    """

    async def scenario() -> None:
        rig = await shipped_rig(tmp_path, steady_db)
        rig.source.fail_symbols = set(EXHAUSTING)
        rig.source.fail_delay_ms = FAST_FAIL_MS
        rig.start()
        await until_rounds(rig, 1)
        rig.source.fail_symbols = set()
        rig.source.symbol_errors[LAST] = SourceError("scripted outage of the symbol")
        await until_rounds(rig, 2)
        assert any(LAST in line for line in rig.lines_with("oi-warning symbol-failed"))
        await rig.advance_to_next_wake()
        del rig.source.symbol_errors[LAST]
        assert readable_s(rig, LAST) is None
        await until_rounds(rig, 3)
        value = readable_s(rig, LAST)
        report("exhausted and next failed", value)
        assert value is not None and value > 800
        await finish(rig)

    run(scenario)


# ---- limiter contention and late rounds -------------------------------------------------------------------------


def test_saturated_backfill_after_an_on_pace_round_readable_by_567_s(tmp_path: Path) -> None:
    """Backfill saturated, previous round on schedule and on pace: p' <= 60 s + 5 s, late row readable by e + 567 s.

    Simulation on a fake clock: a measurement of the code on a model of the network, not of Binance.
    """

    async def scenario() -> None:
        rig = await shipped_rig(tmp_path, None)  # an empty database: every slot of 7 days is a hole
        publish_last_late(rig)
        wake_ns = rig.t.monotonic_ns() + STEP * NS_PER_MS  # the wake of the round that serves the late path
        rig.start()
        await until_rounds(rig, 1)
        await until_rounds(rig, 2)
        window = rig.lines_with("oi-catchup")[0]
        assert "pages=0 " not in window and ("end=budget" in window or "end=share" in window)  # saturated
        starts = rig.source.time_calls + [call.at_ns for call in rig.source.hist_calls]
        in_window = sum(1 for start in starts if start < wake_ns < start + STEP * NS_PER_MS)
        assert in_window + 1 + len(SYMBOLS) > 400  # not vacuous: the round's starts met a full budget
        value = readable_s(rig, LAST)
        report("saturated backfill, late", value)
        assert value is not None and value <= 567
        await finish(rig)

    run(scenario)


async def late_round_on_the_late_path(tmp_path: Path, steady: Path, late_s: int) -> float | None:
    """A healthy round of e, then the round that serves the late path woken ``late_s`` late."""
    rig = await shipped_rig(tmp_path, steady)
    publish_last_late(rig)
    rig.start()
    await until_rounds(rig, 1)
    wake = E + STEP + OFFSET_MS
    await rig.t.advance(wake - 1_000 - rig.t.wall_ms())  # the idle window is over, the task sleeps until the wake
    await rig.t.advance(1_000 + late_s * 1_000, late=True)  # the wake-up comes late_s late
    await until_rounds(rig, 2)
    assert f"late_ms={late_s * 1_000}" in rig.lines_with("oi-round")[1]
    value = readable_s(rig, LAST)
    report(f"round {late_s} s late, late path", value)
    await finish(rig)
    return value


def test_storing_round_starting_30_s_late_readable_by_600_s(tmp_path: Path, steady_db: Path) -> None:
    """The round that serves the late path starts 30 s late (the observed host stall): p ~ 78 s, readable by e + 600 s.

    Simulation on a fake clock: a measurement of the code on a model of the network, not of Binance.
    """

    async def scenario() -> None:
        value = await late_round_on_the_late_path(tmp_path, steady_db, 30)
        assert value is not None and value <= 600

    run(scenario)


@pytest.mark.parametrize(("late_s", "met"), [(50, True), (51, False)])
def test_late_start_limit_is_50_s(tmp_path: Path, steady_db: Path, late_s: int, met: bool) -> None:
    """The edge of the late-start line: a round 50 s late still stores the late row by e + 600 s, 51 s late misses.

    Zero margin at 50 s: the late row is readable at exactly e + 600.0 s (p' = 50 s + 48 s).

    Simulation on a fake clock: a measurement of the code on a model of the network, not of Binance.
    """

    async def scenario() -> None:
        value = await late_round_on_the_late_path(tmp_path, steady_db, late_s)
        assert value is not None
        assert (value <= 600) is met

    run(scenario)


def test_untrusted_round_time_defers_rows_to_the_next_round(tmp_path: Path, steady_db: Path) -> None:
    """The round's server-time call fails: the round is ``untrusted`` and stores nothing; the next round stores the
    early-published rows by e + 550 s.

    Simulation on a fake clock: a measurement of the code on a model of the network, not of Binance.
    """

    async def scenario() -> None:
        rig = await shipped_rig(tmp_path, steady_db)
        rig.source.time_error = SourceError("scripted time request failure")
        rig.start()
        await until_rounds(rig, 1)
        assert rig.lines_with("oi-warning round-untrusted-time")
        assert all(readable_s(rig, symbol) is None for symbol in SYMBOLS)
        rig.source.time_error = None
        await until_rounds(rig, 2)
        value = latest_readable_s(rig)
        report("untrusted round, early rows from the next round", value)
        assert all(readable_s(rig, symbol) is not None for symbol in SYMBOLS)
        assert value <= 550
        await finish(rig)

    run(scenario)
