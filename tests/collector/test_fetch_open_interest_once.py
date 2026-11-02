import asyncio

import pytest

from quasar_market_data.collector.async_open_interest_writer import OpenInterestPageError, OpenInterestWriteReport
from quasar_market_data.collector.binance_open_interest import OpenInterestParseError
from quasar_market_data.collector.fetch_open_interest_once import (
    MAX_PERIODS,
    FetchOpenInterestOnceConfig,
    FetchOpenInterestOnceReport,
    fetch_open_interest_once,
)
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.collector.open_interest_gate import OpenInterestBanned, OpenInterestPaused
from quasar_market_data.collector.open_interest_writer import OpenInterestWriteOutcomeUnknown
from tests.collector.fetch_fakes import SYMBOL, FakeClock
from tests.collector.open_interest_fakes import STEP, FakeOiSource, RecordingSink


def config(**changes: object) -> FetchOpenInterestOnceConfig:
    return FetchOpenInterestOnceConfig(symbol=SYMBOL, **changes)  # type: ignore[arg-type]


def go(
    source: FakeOiSource, sink: RecordingSink, clock: FakeClock, cfg: FetchOpenInterestOnceConfig | None = None
) -> FetchOpenInterestOnceReport:
    return asyncio.run(fetch_open_interest_once(source, sink, cfg or config(), clock))


def test_requests_the_newest_period_ends_up_to_the_aligned_boundary_and_writes_the_eligible_rows() -> None:
    clock = FakeClock()
    source, sink = FakeOiSource(clock), RecordingSink()
    report = go(source, sink, clock)
    (symbol, start, end, as_of) = source.hist_calls[0]
    assert symbol == SYMBOL
    assert end % STEP == 0 and 0 <= as_of - end < STEP
    assert start == end - 2 * STEP  # default 3 periods
    assert (report.start_ms, report.end_ms) == (start, end)
    assert report.fetched == 3
    assert len(report.selection.eligible) == 3 and not report.selection.pending
    assert len(sink.writes) == 1
    rows, ingested_at = sink.writes[0]
    assert [row.key.time_ms for row in rows] == [start, start + STEP, end]
    assert ingested_at == clock.wall_ms()
    assert report.write is not None and report.write.inserted == 3


def test_a_row_inside_the_publication_delay_is_pending_and_not_written() -> None:
    clock = FakeClock()
    clock.advance_wall(-30_000)  # 6 s into the period: the period end is younger than the 15 s delay
    source, sink = FakeOiSource(clock), RecordingSink()
    report = go(source, sink, clock)
    assert [row.key.time_ms for row in report.selection.pending] == [source.hist_calls[0][2]]
    assert len(report.selection.eligible) == 2
    assert [row.key.time_ms for row in sink.writes[0][0]] == [r.key.time_ms for r in report.selection.eligible]


def test_nothing_eligible_writes_nothing() -> None:
    clock = FakeClock()
    clock.advance_wall(-30_000)
    source, sink = FakeOiSource(clock), RecordingSink()
    report = go(source, sink, clock, config(periods=1))
    assert not report.selection.eligible and len(report.selection.pending) == 1
    assert sink.writes == [] and report.write is None


def test_untrusted_time_makes_no_request_and_stores_nothing() -> None:
    clock = FakeClock()
    source, sink = FakeOiSource(clock, time_error=SourceError("time request failed")), RecordingSink()
    report = go(source, sink, clock)
    assert source.hist_calls == [] and sink.writes == []
    assert report.fetched == 0 and report.write is None
    assert report.selection.untrusted_reason is not None
    assert (report.start_ms, report.end_ms) == (None, None)


def test_a_row_for_another_symbol_rejects_the_whole_response_and_writes_nothing() -> None:
    clock = FakeClock()
    source, sink = FakeOiSource(clock, symbol_in_rows="ETHUSDT"), RecordingSink()
    with pytest.raises(OpenInterestParseError):
        go(source, sink, clock)
    assert sink.writes == []


@pytest.mark.parametrize("error", [SourceError("boom"), OpenInterestPaused(300_000, 1)])
def test_source_errors_propagate_and_nothing_is_written(error: SourceError) -> None:
    clock = FakeClock()
    source, sink = FakeOiSource(clock, hist_error=error), RecordingSink()
    with pytest.raises(type(error)):
        go(source, sink, clock)
    assert sink.writes == []


@pytest.mark.parametrize("error", [OpenInterestPaused(300_000, 1), OpenInterestBanned()], ids=["paused", "banned"])
def test_a_429_or_418_on_the_time_request_is_not_reported_as_untrusted_time(error: SourceError) -> None:
    clock = FakeClock()
    source, sink = FakeOiSource(clock, time_error=error), RecordingSink()
    with pytest.raises(type(error)):
        go(source, sink, clock)
    assert source.hist_calls == [] and sink.writes == []


def test_a_page_error_of_the_writer_propagates_unchanged() -> None:
    clock = FakeClock()
    partial = OpenInterestWriteReport(100, 0, (), (), (), 0, 0)
    error = OpenInterestPageError(OpenInterestWriteOutcomeUnknown("commit failed"), 1, partial)
    with pytest.raises(OpenInterestPageError) as raised:
        go(FakeOiSource(clock), RecordingSink(error), clock)
    assert raised.value is error


def test_the_range_is_limited_to_500_period_ends() -> None:
    assert MAX_PERIODS == 500
    clock = FakeClock()
    source, sink = FakeOiSource(clock), RecordingSink()
    go(source, sink, clock, config(periods=500))
    (_, start, end, _) = source.hist_calls[0]
    assert (end - start) // STEP + 1 == 500


@pytest.mark.parametrize("periods", [0, -1, 501])
def test_periods_outside_1_to_500_are_rejected(periods: int) -> None:
    with pytest.raises(ValueError, match="periods"):
        config(periods=periods)


@pytest.mark.parametrize("periods", [True, 1.0, "3"])
def test_periods_must_be_an_int(periods: object) -> None:
    with pytest.raises(TypeError):
        config(periods=periods)


def test_a_negative_publication_delay_is_rejected() -> None:
    with pytest.raises(ValueError):
        config(publication_delay_ms=-1)
