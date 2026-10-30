"""The backfill worker (S16): hole filling through ``fetch_once`` pages, recovery from read-backs, back-off and windows."""

import ast
import asyncio
import itertools
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.collector import backfill as backfill_module
from quasar_market_data.collector import hole_reader
from quasar_market_data.collector.backfill import (
    MAX_BACKOFF_CYCLES,
    OVERRUN_LIMIT,
    PAGE_BUDGET_MS,
    RESCAN_CYCLES,
    UNPROVEN_RETRY_CYCLES,
    BackfillConfig,
    Backfiller,
    SliceReport,
    SliceStatus,
)
from quasar_market_data.collector.candle_writer import CandleWriteOutcomeUnknown, WriteReport
from quasar_market_data.collector.hole_reader import HoleReadError
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.domain.backfill import Attribution
from tests.collector.backfill_fakes import ROOM_MS, SYMBOL, TF, Env, Request


def go(
    tmp_path: Path,
    body: Callable[[Env], Awaitable[None]],
    *,
    symbols: tuple[str, ...] = (SYMBOL,),
    days: int = 1,
) -> None:
    async def main() -> None:
        env = await Env(tmp_path, symbols, days).open()
        try:
            await body(env)
        finally:
            await env.close()

    asyncio.run(main())


def hole(env: Env, first: int, last: int, *, edge: int = 2) -> None:
    """Store ``edge`` candles before and one after the hole ``[first, last]`` (slot indices), serve the hole."""
    env.store([env.slot(i) for i in range(first - edge, first)] + [env.slot(last + 1)])
    env.source.series[SYMBOL] = {env.slot(i) for i in range(first, last + 1)}


def only(env: Env) -> SliceReport:
    assert len(env.slices) == 1, env.slices
    return env.slices[0]


def test_fills_an_interior_hole_with_the_exact_request(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        await env.backfiller().run_idle(env.window())
        assert [(r.start, r.end, r.limit) for r in env.source.requests] == [(env.slot(2), env.slot(11), 10)]
        assert set(range(2, 12)) <= {(o - env.hs) // TF for o in env.stored()}
        r = only(env)
        assert (r.status, r.pages, r.served_all, r.stored) == (SliceStatus.PROGRESS, 1, 1, 10)
        # the three statements: exchange answer, writer report, read-back (13 candles were already stored: not counted)
        assert (r.new_in_targets, r.recovered, r.missing_in_targets, r.attribution) == (10, 10, 0, Attribution.OK)

    go(tmp_path, body)


def test_pages_a_long_head_in_order_with_exact_requests(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        env.store([env.slot(2500)])
        env.source.series[SYMBOL] = {env.slot(i) for i in range(2500)}
        await env.backfiller().run_idle(env.window())
        assert [(r.start, r.end, r.limit) for r in env.source.requests] == [
            (env.slot(0), env.slot(2499), 1000),
            (env.slot(1000), env.slot(2499), 1000),
            (env.slot(2000), env.slot(2499), 500),
        ]
        assert env.source.time_calls == 3
        r = only(env)
        assert (r.pages, r.served_all, r.recovered, r.attribution) == (3, 1, 2500, Attribution.OK)

    go(tmp_path, body, days=9)


def test_a_short_page_ends_the_target_and_the_rest_stays_missing(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        env.source.series[SYMBOL] = {env.slot(i) for i in range(2, 6)}  # the exchange has four of ten
        await env.backfiller().run_idle(env.window())
        r = only(env)
        assert (r.served_some, r.served_all, r.recovered, r.missing_in_targets) == (1, 0, 4, 6)
        assert len(env.source.requests) == 1  # a short page ends the target: no second request

    go(tmp_path, body)


def test_a_partly_recovered_target_reports_exact_numbers_end_to_end(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        env.source.series[SYMBOL] = {env.slot(i) for i in range(2, 8)}
        await env.backfiller().run_idle(env.window())
        r = only(env)
        assert (r.served_some, r.new_in_targets, r.recovered, r.missing_in_targets, r.attribution) == (
            1,
            6,
            6,
            4,
            Attribution.OK,
        )

    go(tmp_path, body)


def test_recovered_comes_from_the_database_not_from_the_write_report(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        env.sink.fake_report = WriteReport(inserted=3, duplicates=0, conflicts=())  # claims 3, writes nothing
        before = env.stored()
        await env.backfiller().run_idle(env.window())
        r = only(env)
        assert (r.stored, r.new_in_targets, r.recovered, r.missing_in_targets) == (3, 0, None, 10)
        assert r.attribution is Attribution.REPORT_MISMATCH
        assert env.stored() == before

    go(tmp_path, body)


def test_served_all_with_invalid_rows_does_not_claim_recovery(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        env.source.row_values = {env.slot(4): {"high": "0.1"}, env.slot(5): {"high": "0.1"}}  # high below the rest
        await env.backfiller().run_idle(env.window())
        r = only(env)
        assert (r.served_all, r.invalid, r.recovered, r.missing_in_targets, r.attribution) == (
            1,
            2,
            8,
            2,
            Attribution.OK,
        )

    go(tmp_path, body)


def test_a_pending_row_is_not_stored_and_its_slot_stays_a_hole(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        last = (env.forming - env.hs) // TF - 1
        hole(
            env, last - 9, last, edge=last - 9
        )  # the hole ends at the newest closed slot; the stored candle after it is at `forming`
        env.source.series[SYMBOL] = {env.slot(i) for i in range(last - 9, last + 1)}
        env.source._server_base -= (
            330_000  # the server is 5.5 minutes behind the wall clock: the newest rows are pending
        )
        await env.backfiller().run_idle(env.window())
        r = only(env)
        assert r.pending >= 1 and r.served_all == 1
        assert r.recovered == 10 - r.pending - r.invalid and r.missing_in_targets == r.pending + r.invalid

    go(tmp_path, body)


def test_stopping_after_the_first_of_three_pages_reports_a_partial_target(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        env.store([env.slot(2500)])
        env.source.series[SYMBOL] = {env.slot(i) for i in range(2500)}

        async def stop_after_first(n: int) -> None:
            env.stop.set()

        env.sink.after_write = stop_after_first
        await env.backfiller().run_idle(env.window())
        r = only(env)
        assert (r.pages, r.recovered, r.missing_in_targets, r.attribution) == (1, 1000, 1500, Attribution.OK)
        assert len(env.source.requests) == 1  # stop ends the walk at a page boundary

    go(tmp_path, body, days=9)


def test_before_and_after_reads_use_the_identical_target_ranges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        seen: list[Any] = []
        real = hole_reader.read_present

        async def spy(*args: Any, **kwargs: Any) -> Any:
            seen.append(kwargs["targets"])
            return await real(*args, **kwargs)

        monkeypatch.setattr("quasar_market_data.collector.backfill.read_present", spy)
        await env.backfiller().run_idle(env.window())
        assert len(seen) == 2 and seen[0] == seen[1] and seen[0]

    go(tmp_path, body)


def test_a_concurrent_collector_write_inside_a_target_makes_recovery_unverified(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        env.source.series[SYMBOL] = {env.slot(i) for i in range(2, 6)}

        async def foreign(request: Request) -> None:
            env.store([env.slot(9)])  # another process writes a slot inside the target that this page does not carry

        env.source.on_klines = foreign
        await env.backfiller().run_idle(env.window())
        r = only(env)
        assert (r.new_in_targets, r.recovered, r.missing_in_targets) == (5, None, 5)
        assert r.attribution is Attribution.FOREIGN_WRITES

    go(tmp_path, body)


def test_a_concurrent_write_of_a_slot_the_slice_also_sent_is_caught(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)

        async def foreign(request: Request) -> None:
            env.store([env.slot(4)])  # the same slot this page is about to write: our write sees a duplicate

        env.source.on_klines = foreign
        await env.backfiller().run_idle(env.window())
        r = only(env)
        assert (r.duplicates, r.recovered, r.attribution) == (1, None, Attribution.REPORT_MISMATCH)

    go(tmp_path, body)


def test_a_late_write_draining_from_a_cancelled_round_is_not_credited_to_backfill(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        env.source.series[SYMBOL] = {env.slot(i) for i in range(2, 6)}

        async def late_drain(request: Request) -> None:
            from quasar_market_data.collector.binance_klines import parse_klines
            from quasar_market_data.domain.validation import require_valid
            from tests.collector.fetch_fakes import kline_row

            candles = [
                require_valid(c) for c in parse_klines([kline_row(env.slot(8), TF)], SYMBOL, env.loop_config.timeframe)
            ]
            await env.writer.write(
                candles, ingested_at_ms=1_792_677_000_000
            )  # a job still on the writer thread, in a target

        env.source.on_klines = late_drain
        await env.backfiller().run_idle(env.window())
        assert only(env).attribution is Attribution.FOREIGN_WRITES

    go(tmp_path, body)


def test_candles_outside_the_targets_arriving_between_the_reads_change_nothing(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        from quasar_market_data.domain.timeframe import Timeframe

        hole(env, 2, 11)

        async def outside(request: Request) -> None:
            env.store([env.slot(14), env.slot(15)])  # after the stored candle that closes the hole
            env.store([env.slot(5)], symbol="B")
            env.store([env.slot(5) + 60_000 * 2], timeframe=Timeframe("1m"))

        env.source.on_klines = outside
        await env.backfiller().run_idle(env.window())
        r = only(env)
        assert (r.new_in_targets, r.recovered, r.missing_in_targets, r.attribution) == (10, 10, 0, Attribution.OK)

    go(tmp_path, body)


def test_an_unknown_write_outcome_makes_recovery_unverified(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        env.store([env.slot(2500)])
        env.source.series[SYMBOL] = {env.slot(i) for i in range(2500)}
        env.sink.raises = {2: CandleWriteOutcomeUnknown("unknown")}
        await env.backfiller().run_idle(env.window())
        r = only(env)
        assert (r.attribution, r.recovered, r.status, r.pages) == (
            Attribution.WRITE_UNKNOWN,
            None,
            SliceStatus.BACKOFF,
            1,
        )
        assert r.new_in_targets == 1000  # the database fact is still reported

    go(tmp_path, body, days=9)


def test_a_failing_before_read_starts_no_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)

        async def broken(*args: Any, **kwargs: Any) -> Any:
            raise HoleReadError("disk gone")

        monkeypatch.setattr("quasar_market_data.collector.backfill.read_present", broken)
        await env.backfiller().run_idle(env.window())
        assert env.source.requests == [] and only(env).status is SliceStatus.BACKOFF

    go(tmp_path, body)


def test_a_failing_after_read_withholds_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        real = hole_reader.read_present
        calls: list[int] = []

        async def second_fails(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            if len(calls) == 2:
                raise HoleReadError("disk gone")
            return await real(*args, **kwargs)

        monkeypatch.setattr("quasar_market_data.collector.backfill.read_present", second_fails)
        await env.backfiller().run_idle(env.window())
        r = only(env)
        assert (r.new_in_targets, r.recovered, r.missing_in_targets) == (None, None, None)
        assert (r.attribution, r.stored, r.status) == (Attribution.READBACK_FAILED, 10, SliceStatus.PROGRESS)

    go(tmp_path, body)


def test_every_backfill_request_carries_a_page_plan(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        env.store([env.slot(2500)], symbol="B")
        await env.backfiller().run_idle(env.window())
        assert env.source.requests
        for r in env.source.requests:
            assert r.start is not None and r.end is not None and r.limit == (r.end - r.start) // TF + 1

    go(tmp_path, body)


def test_backfill_module_never_writes_except_through_fetch_once() -> None:
    tree = ast.parse(Path(backfill_module.__file__ or "").read_text())
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "write" not in calls
    imported = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    imported |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not {"sqlite3", "CandleWriter", "AsyncCandleWriter", "judge_page"} & imported
    assert "fetch_once" in imported


def test_the_slice_line_has_no_done_filled_or_complete() -> None:
    assert {s.value for s in SliceStatus} == {"no_targets", "progress", "backoff", "time_untrusted", "suspended"}
    fields = set(SliceReport.__dataclass_fields__)
    assert not {"done", "filled", "complete", "unavailable"} & fields
    assert not any(word in name for name in fields for word in ("done", "filled", "complete", "unavailable"))


# --- invalid pages, exhaustion, back-off -------------------------------------------------------------------------


def _violations(env: Env) -> dict[str, Callable[[str, int, int, int], list[list[Any]]]]:
    from tests.collector.fetch_fakes import kline_row

    def rows(times: list[int]) -> list[list[Any]]:
        return [kline_row(o, TF) for o in times]

    def good(lo: int, hi: int, limit: int) -> list[int]:
        return [o for o in range(lo, hi + 1, TF)][:limit]

    def bad_close(lo: int, hi: int, limit: int) -> list[list[Any]]:
        out = rows(good(lo, hi, limit))
        out[-1][6] += 1
        return out

    return {
        "row_before_start": lambda s, lo, hi, n: rows([lo - TF, *good(lo, hi, n - 1)]),
        "row_after_end": lambda s, lo, hi, n: rows([*good(lo, hi, n - 1), hi + TF]),
        "misaligned": lambda s, lo, hi, n: rows([*good(lo, hi, n - 1), hi - 7]),
        "bad_close_time": lambda s, lo, hi, n: bad_close(lo, hi, n),
        "descending": lambda s, lo, hi, n: rows(list(reversed(good(lo, hi, n)))),
    }


@pytest.mark.parametrize("case", ["row_before_start", "row_after_end", "misaligned", "bad_close_time", "descending"])
def test_an_invalid_page_changes_no_database_row(tmp_path: Path, case: str) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        before = env.counts()
        env.source.override = _violations(env)[case]
        bf = env.backfiller()
        await bf.run_idle(env.window())
        assert env.counts() == before
        assert only(env).status is SliceStatus.BACKOFF
        env.slices.clear()
        await bf.run_idle(env.window())  # the back-off doubles: cycle 2 is skipped
        assert len(env.source.requests) == 1 and env.slices == []
        await bf.run_idle(env.window())  # cycle 3 asks again
        assert len(env.source.requests) == 2

    go(tmp_path, body)


async def request_cycles(env: Env, bf: Any, n: int) -> list[int]:
    seen: list[int] = []
    for cycle in range(1, n + 1):
        before = len(env.source.requests)
        await bf.run_idle(env.window())
        if len(env.source.requests) > before:
            seen.append(cycle)
    return seen


def test_a_symbol_with_no_older_data_is_asked_once_per_ttl(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        env.store([env.slot(10)])  # nothing older exists at the exchange: the head [0, 10) comes back empty
        bf = env.backfiller()
        assert await request_cycles(env, bf, UNPROVEN_RETRY_CYCLES + 1) == [1, UNPROVEN_RETRY_CYCLES + 1]
        restarted = env.backfiller()  # a restart forgets the exhaustion
        assert await request_cycles(env, restarted, 2) == [1]
        assert [s.served_none for s in env.slices if s.pages] == [1, 1, 1]

    go(tmp_path, body)


def test_exhausted_targets_do_not_report_no_targets(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        env.store([env.slot(10)])
        await request_cycles(env, env.backfiller(), 3)
        assert [s.status for s in env.slices] == [
            SliceStatus.PROGRESS
        ]  # not NO_TARGETS: a hole exists, it is only waiting

    go(tmp_path, body)


def test_no_targets_means_only_that_the_reader_found_no_hole(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        env.store([env.slot(i) for i in range(5)])
        await env.backfiller().run_idle(env.window())
        r = only(env)
        assert (r.status, r.pages, r.stored) == (SliceStatus.NO_TARGETS, 0, 0)
        assert env.source.requests == []

    go(tmp_path, body)


TEN = tuple(f"S{i}" for i in range(10))  # ten symbols, like the shipped config: the old stride never visited 1, 3, 6, 8


def _no_holes(env: Env) -> None:
    for symbol in env.symbols:
        env.store([env.slot(i) for i in range(5)], symbol=symbol)


def test_one_window_visits_every_symbol_once_in_order(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        _no_holes(env)
        await env.backfiller().run_idle(env.window())
        assert [s.symbol for s in env.slices] == list(TEN)

    go(tmp_path, body, symbols=TEN)


def test_the_next_window_resumes_after_the_last_visited_symbol(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        _no_holes(env)

        def stop_after_three(report: SliceReport) -> None:
            env.slices.append(report)
            if len(env.slices) == 3:
                env.stop.set()

        bf = Backfiller(
            BackfillConfig(env.days), env.loop_config, env.db, env.t, env.t.sleep, stop_after_three, lambda c, o: None
        )
        await bf.run_idle(env.window())
        assert [s.symbol for s in env.slices] == ["S0", "S1", "S2"]
        env.stop.clear()
        env.slices.clear()
        await bf.run_idle(env.window())
        assert [s.symbol for s in env.slices] == ["S3", "S4", "S5"]

    go(tmp_path, body, symbols=TEN)


def _many_targets(env: Env) -> None:
    env.store([env.slot(i) for i in range(10, 52, 2)])  # 21 candles: 20 one-slot holes between them, plus a head


def test_a_restart_costs_at_most_k_plus_one_requests_per_symbol(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        _many_targets(env)
        bf = env.backfiller()
        await bf.run_idle(env.window())
        assert len(env.source.requests) == 21  # K interior runs and the head
        await bf.run_idle(env.window())
        await bf.run_idle(env.window())
        assert len(env.source.requests) == 21  # exhausted

    go(tmp_path, body)


def test_requests_stay_within_the_limiter_rate_during_repeated_restarts(tmp_path: Path) -> None:
    from quasar_market_data.collector.rate_limiter import RateLimitedSource, RateLimiter

    async def body(env: Env) -> None:
        _many_targets(env)

        async def spend(ms: int) -> None:
            await env.t.advance(ms)

        limited = RateLimitedSource(env.source, RateLimiter(100, env.t, spend))
        deadlines: list[int] = []
        for _ in range(10):
            window = env.window(source=limited)
            deadlines.append(window.deadline_mono_ns)
            await env.backfiller().run_idle(window)
        times = [r.mono_ns for r in env.source.requests]
        assert len(times) == 210
        assert min(b - a for a, b in itertools.pairwise(times)) >= 100 * 1_000_000
        assert all(t < max(deadlines) for t in times)

    go(tmp_path, body)


def test_a_source_error_backs_off_exponentially_to_the_cap(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        env.source.errors[SYMBOL] = SourceError("unknown symbol 'A'")
        seen = await request_cycles(env, env.backfiller(), 46)
        assert seen == [1, 3, 6, 11, 20, 33, 46]  # waits of 1, 2, 4, 8, 12, 12 cycles
        assert [b - a - 1 for a, b in itertools.pairwise(seen)] == [1, 2, 4, 8, 12, 12]
        assert MAX_BACKOFF_CYCLES == 12

    go(tmp_path, body)


def test_success_resets_the_backoff(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        env.source.errors[SYMBOL] = SourceError("down")
        bf = env.backfiller()
        assert await request_cycles(env, bf, 5) == [1, 3]  # two failures: the next wait would be 4 cycles
        del env.source.errors[SYMBOL]
        await bf.run_idle(env.window())  # cycle 6: ask again, succeeds
        assert env.source.requests and only_last(env).status is SliceStatus.PROGRESS
        env.store([env.slot(20)])  # a new hole appears (13..19)
        env.source.series[SYMBOL] = {env.slot(i) for i in range(13, 20)}
        env.source.errors[SYMBOL] = SourceError("down again")
        before = len(env.source.requests)
        await bf.run_idle(env.window())  # cycle 7: fails, a first failure again: wait 1
        await bf.run_idle(env.window())  # cycle 8 skipped
        await bf.run_idle(env.window())  # cycle 9 asks
        assert len(env.source.requests) == before + 2

    go(tmp_path, body)


def only_last(env: Env) -> SliceReport:
    return env.slices[-1]


# --- untrusted time and wall-clock steps -----------------------------------------------------------------------


def test_untrusted_round_trip_stores_nothing_and_claims_no_progress(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        env.source.rtt_ms = 2_001
        before = env.counts()
        bf = env.backfiller()
        await bf.run_idle(env.window())
        r = only(env)
        assert (r.status, r.stored, r.recovered, r.missing_in_targets) == (SliceStatus.TIME_UNTRUSTED, 0, 0, 10)
        assert env.counts() == before
        await bf.run_idle(env.window())  # not exhausted and not backed off: it asks again
        assert len(env.source.requests) == 2

    go(tmp_path, body)


def test_untrusted_clock_divergence_stores_nothing(tmp_path: Path) -> None:
    class Jumpy:
        """A clock whose wall time alternates by one second between consecutive reads."""

        def __init__(self, inner: Any) -> None:
            self.inner, self.reads = inner, 0

        def monotonic_ns(self) -> int:
            return int(self.inner.monotonic_ns())

        def wall_ms(self) -> int:
            self.reads += 1
            return int(self.inner.wall_ms()) + 1_000 * (self.reads % 2)

    async def body(env: Env) -> None:
        hole(env, 2, 11)
        before = env.counts()
        await env.backfiller(clock=Jumpy(env.t)).run_idle(env.window())
        assert only(env).status is SliceStatus.TIME_UNTRUSTED and env.counts() == before

    go(tmp_path, body)


def test_a_backward_wall_clock_step_does_not_raise_and_fills_only_the_hole(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        env.t._wall_ms -= 3_600_000
        await env.backfiller().run_idle(env.window())
        assert max(env.stored()) <= env.slot(
            12
        )  # the gate uses server time, so the hole is filled and nothing beyond it

    go(tmp_path, body)


def test_a_forward_local_clock_does_not_store_a_forming_candle(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        env.source.series[SYMBOL] = {env.source.server_ms() // TF * TF}  # the server's forming slot
        env.t._wall_ms += 3_600_000  # the local clock believes the forming slot is an hour later: the head covers it
        await env.backfiller().run_idle(env.window())
        r = only(env)
        assert env.stored() == [] and r.pending == 1 and r.recovered == 0

    go(tmp_path, body)


# --- deadline and stop -----------------------------------------------------------------------------------------


def test_a_page_is_not_started_without_room_before_the_deadline(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        await env.backfiller().run_idle(env.window(deadline_in_ms=PAGE_BUDGET_MS - 1))
        assert env.source.requests == []
        await env.backfiller().run_idle(env.window(deadline_in_ms=PAGE_BUDGET_MS))
        assert len(env.source.requests) == 1

    go(tmp_path, body)


def test_a_page_is_not_started_after_a_wall_clock_step_that_would_fake_room(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        env.t._wall_ms -= 3_600_000
        await env.backfiller().run_idle(env.window(deadline_in_ms=1_000))
        assert env.source.requests == []

    go(tmp_path, body)


def test_stop_between_pages_ends_at_a_page_boundary(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        env.store([env.slot(21)])
        env.store([env.slot(31)], symbol="B")
        env.source.series["B"] = {env.slot(i) for i in range(22, 31)}

        async def stop_after_write(n: int) -> None:
            env.stop.set()

        env.sink.after_write = stop_after_write
        await env.backfiller().run_idle(env.window())
        assert {r.symbol for r in env.source.requests} == {SYMBOL}  # the second symbol was never started

    go(tmp_path, body, symbols=(SYMBOL, "B"))


def test_stop_during_a_request_cancels_it_and_stores_nothing(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        parked = asyncio.Event()

        async def park(request: Request) -> None:
            parked.set()
            await asyncio.Event().wait()

        env.source.on_klines = park
        before = env.counts()
        task = asyncio.ensure_future(env.backfiller().run_idle(env.window()))
        await parked.wait()
        env.stop.set()
        await task
        assert env.counts() == before and only(env).status is SliceStatus.PROGRESS  # a stop is not a failure

    go(tmp_path, body)


def test_stop_during_a_write_waits_for_it(tmp_path: Path) -> None:
    from tests.collector.fetch_fakes import ControlledSink

    async def body(env: Env) -> None:
        hole(env, 2, 11)
        blocked = ControlledSink(block=True)
        env.sink.inner = blocked
        task = asyncio.ensure_future(env.backfiller().run_idle(env.window()))
        await blocked.write_started.wait()
        env.stop.set()
        for _ in range(100):
            await asyncio.sleep(0)
        assert not task.done()  # a started write is never cancelled and never abandoned
        blocked.release.set()
        await task
        assert only(env).status is SliceStatus.PROGRESS

    go(tmp_path, body)


# --- overrun and suspension ------------------------------------------------------------------------------------


def extend(env: Env, k: int) -> None:
    """Window ``k >= 2``: a new candle 12 slots after the previous one, and the eleven slots between are served."""
    previous, new = 12 * (k - 1), 12 * k
    env.store([env.slot(new)])
    env.source.series[SYMBOL] = {env.slot(i) for i in range(previous + 1, new)}


def slow_writes(env: Env, delays: list[int]) -> None:
    async def after_write(n: int) -> None:
        delay = delays[min(n - 1, len(delays) - 1)]
        if delay:
            await env.t.advance(delay)

    env.sink.after_write = after_write


def test_two_consecutive_idle_overruns_suspend_the_backfiller_for_the_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        env.store([env.slot(12)])
        slow_writes(env, [31_000])  # each window ends 1 s after its due time (room 30 s)
        holes_calls: list[int] = []
        real = hole_reader.read_holes

        async def counting(*args: Any, **kwargs: Any) -> Any:
            holes_calls.append(1)
            return await real(*args, **kwargs)

        monkeypatch.setattr("quasar_market_data.collector.backfill.read_holes", counting)
        bf = env.backfiller()
        await bf.run_idle(env.window(30_000))
        assert env.suspended == []
        extend(env, 2)
        await bf.run_idle(env.window(30_000))
        assert env.suspended == [(OVERRUN_LIMIT, 1_000)]
        frozen = (len(env.source.requests), env.sink.writes, len(holes_calls))
        for k in range(3, 8):
            extend(env, k)
            await bf.run_idle(env.window(30_000))
        assert (len(env.source.requests), env.sink.writes, len(holes_calls)) == frozen  # zero calls, no resume
        assert env.suspended == [(OVERRUN_LIMIT, 1_000)]  # reported once
        fresh = env.backfiller()  # a new process runs again
        await fresh.run_idle(env.window(30_000))
        assert len(env.source.requests) > frozen[0]

    go(tmp_path, body)


def test_an_overrun_followed_by_a_clean_window_resets_the_count(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        env.store([env.slot(12)])
        slow_writes(env, [31_000, 0, 31_000])
        bf = env.backfiller()
        for k in (1, 2, 3):
            if k > 1:
                extend(env, k)
            env.sink.writes = 0 if k == 1 else env.sink.writes
            await bf.run_idle(env.window(30_000))
        assert env.suspended == []

    go(tmp_path, body)


def test_a_wall_clock_step_during_the_window_is_not_an_overrun(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        env.store([env.slot(12)])

        async def step(n: int) -> None:
            env.t._wall_ms += 3_600_000
            await env.t.advance(1_000)

        env.sink.after_write = step
        bf = env.backfiller()
        for k in (1, 2, 3):
            if k > 1:
                extend(env, k)
            await bf.run_idle(env.window(30_000))
        assert env.suspended == []

    go(tmp_path, body)


def test_a_window_ending_before_the_due_time_is_not_an_overrun_even_after_the_write_reserve(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        env.store([env.slot(12)])
        slow_writes(env, [29_999])  # ends 1 ms before due, long after deadline + write reserve
        bf = env.backfiller()
        for k in (1, 2, 3):
            if k > 1:
                extend(env, k)
            await bf.run_idle(env.window(30_000))
        assert env.suspended == []

    go(tmp_path, body)


# --- errors, idempotence, the gate ----------------------------------------------------------------------------


def test_a_storage_error_is_raised_and_a_source_error_is_not(tmp_path: Path) -> None:
    from quasar_market_data.collector.candle_writer import CandleWriteError

    async def body(env: Env) -> None:
        hole(env, 2, 11)
        env.source.errors[SYMBOL] = SourceError("down")
        await env.backfiller().run_idle(env.window())  # swallowed into a back-off
        assert only(env).status is SliceStatus.BACKOFF
        del env.source.errors[SYMBOL]
        env.sink.raises = {1: CandleWriteError("disk full")}
        with pytest.raises(CandleWriteError):
            await env.backfiller().run_idle(env.window())

    go(tmp_path, body)


def test_a_second_run_over_the_same_database_adds_nothing(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        await env.backfiller().run_idle(env.window())
        first = env.counts()
        await env.backfiller().run_idle(env.window())
        assert env.counts() == first and len(env.source.requests) == 1
        assert env.slices[-1].status is SliceStatus.NO_TARGETS

    go(tmp_path, body)


def test_run_stopped_after_a_partial_backfill_and_started_again_converges(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        env.store([env.slot(2500)])
        env.source.series[SYMBOL] = {env.slot(i) for i in range(2500)}

        async def stop_after_first(n: int) -> None:
            env.stop.set()

        env.sink.after_write = stop_after_first
        await env.backfiller().run_idle(env.window())
        assert len(env.stored()) == 1001
        env.sink.after_write = None
        env.stop = asyncio.Event()  # a restart
        await env.backfiller().run_idle(env.window())
        assert len(env.stored()) == 2501

    go(tmp_path, body, days=9)


def test_every_stored_page_candle_went_through_the_closed_candle_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import quasar_market_data.collector.fetch_once as fetch_module
    from quasar_market_data.domain.closed_candles import select_closed_candles as real

    async def body(env: Env) -> None:
        hole(env, 2, 11)
        before = set(env.stored())
        eligible: set[int] = set()

        def spy(*args: Any, **kwargs: Any) -> Any:
            selection = real(*args, **kwargs)
            eligible.update(c.key.open_time_ms for c in selection.eligible)
            return selection

        monkeypatch.setattr(fetch_module, "select_closed_candles", spy)
        await env.backfiller().run_idle(env.window())
        assert eligible and set(env.stored()) - before == eligible

    go(tmp_path, body)


def test_conflicting_page_values_are_recorded_as_conflicts_not_overwritten(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)

        async def revise(request: Request) -> None:
            env.store([env.slot(4)], volume="123")  # a different value for a slot this page also serves

        env.source.on_klines = revise
        await env.backfiller().run_idle(env.window())
        assert env.query("SELECT COUNT(*) FROM candle_conflicts")[0][0] == 1
        assert env.query("SELECT volume FROM candles WHERE open_time_ms = ?", (env.slot(4),)) == [("123",)]
        assert only(env).conflicts == 1

    go(tmp_path, body)


def test_holes_are_rescanned_only_every_rescan_cycles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(env: Env) -> None:
        env.store([env.slot(10)])
        calls: list[int] = []
        real = hole_reader.read_holes

        async def counting(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            return await real(*args, **kwargs)

        monkeypatch.setattr("quasar_market_data.collector.backfill.read_holes", counting)
        bf = env.backfiller()
        for _ in range(2 * RESCAN_CYCLES + 1):
            await bf.run_idle(env.window())
        assert len(calls) == 3  # cycles 1, 13 and 25

    go(tmp_path, body)


def test_room_constant_matches_the_documented_budget() -> None:
    assert PAGE_BUDGET_MS == 15_000 and ROOM_MS == 279_000


def test_an_unknown_outcome_after_the_commit_is_write_unknown_not_foreign_writes(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        env.sink.raises_after_commit = {1: CandleWriteOutcomeUnknown("unknown")}
        await env.backfiller().run_idle(env.window())
        r = only(env)
        assert (r.attribution, r.recovered, r.new_in_targets) == (Attribution.WRITE_UNKNOWN, None, 10)

    go(tmp_path, body)


def test_a_fully_served_target_that_recovers_nothing_is_not_re_requested_every_window(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        env.source.row_values = {env.slot(i): {"high": "0.1"} for i in range(2, 12)}  # every row stays invalid
        bf = env.backfiller()
        assert await request_cycles(env, bf, UNPROVEN_RETRY_CYCLES + 1) == [1, UNPROVEN_RETRY_CYCLES + 1]

    go(tmp_path, body)


def test_a_slice_that_judged_no_page_does_not_reset_the_backoff(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        hole(env, 2, 11)
        env.source.errors[SYMBOL] = SourceError("down")
        bf = env.backfiller()
        assert await request_cycles(env, bf, 5) == [1, 3]  # two failures: the next wait is 4 cycles
        del env.source.errors[SYMBOL]

        async def stop_during_request(request: Request) -> None:
            env.stop.set()  # the hanging request is cancelled by the stop: a slice with no judged page
            await asyncio.Event().wait()

        env.source.on_klines = stop_during_request
        await bf.run_idle(env.window())  # cycle 6
        env.source.on_klines = None
        env.stop = asyncio.Event()
        env.source.errors[SYMBOL] = SourceError("down")
        before = len(env.source.requests)
        await bf.run_idle(env.window())  # cycle 7: the third failure
        assert len(env.source.requests) == before + 1
        for _ in range(4):  # cycles 8 to 11: still backing off (4 cycles), not just 1
            await bf.run_idle(env.window())
        assert len(env.source.requests) == before + 1

    go(tmp_path, body)
