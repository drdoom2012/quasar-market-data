import asyncio
from collections.abc import Callable

import pytest

from quasar_market_data.collector import fetch_funding_rate_once as module
from quasar_market_data.collector.async_funding_rate_writer import FundingRatePageError, FundingRateWriteReport
from quasar_market_data.collector.binance_funding_rate import FundingRateParseError
from quasar_market_data.collector.binance_usdm_funding_rate import FundingRateRequestRefused
from quasar_market_data.collector.extension_pause import ExtensionPaused, ExtensionStopped
from quasar_market_data.collector.fetch_funding_rate_once import (
    DEFAULT_HOURS,
    MAX_HOURS,
    MAX_PUBLICATION_DELAY_MS,
    FetchFundingRateOnceConfig,
    FetchFundingRateOnceReport,
    FundingRateSelectionError,
    fetch_funding_rate_once,
)
from quasar_market_data.collector.funding_rate_limiter import RequestClass
from quasar_market_data.collector.funding_rate_writer import FundingRateWriteOutcomeUnknown
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.domain.funding_rate import RateType
from tests.collector.fetch_fakes import SYMBOL, FakeClock
from tests.collector.funding_rate_fakes import HOUR, FakeFundingSource, RecordingFundingSink, settlement_rows

DELAY = 30_000


def config(**changes: object) -> FetchFundingRateOnceConfig:
    return FetchFundingRateOnceConfig(symbol=SYMBOL, **changes)  # type: ignore[arg-type]


def go(
    source: FakeFundingSource,
    sink: RecordingFundingSink,
    clock: FakeClock,
    cfg: FetchFundingRateOnceConfig | None = None,
) -> FetchFundingRateOnceReport:
    return asyncio.run(fetch_funding_rate_once(source, sink, cfg or config(), clock))


def test_requests_one_page_of_class_r_ending_at_the_forward_end_bound_and_writes_the_eligible_rows() -> None:
    clock = FakeClock()
    source, sink = FakeFundingSource(clock), RecordingFundingSink()
    report = go(source, sink, clock)
    assert source.time_calls == [RequestClass.R]
    ((symbol, start, end, request_class),) = source.hist_calls
    assert (symbol, request_class) == (SYMBOL, RequestClass.R)
    assert start == end - DEFAULT_HOURS * HOUR  # exactly `hours` hours wide
    assert 0 <= clock.wall_ms() - DELAY - end <= 1_000  # end = lower_bound - delay, the lower bound is conservative
    assert (report.start_ms, report.end_ms) == (start, end)
    assert report.fetched == len(report.selection.eligible) == 24 or report.fetched == 25
    assert not report.selection.pending and not report.full_page and report.off_hour_rows == 0
    ((rows, ingested_at, merge),) = sink.writes
    assert merge is None  # the command claims no coverage
    assert ingested_at == clock.wall_ms()
    assert all(start <= row.key.funding_time_ms <= end for row in rows)
    assert report.write is not None and report.write.inserted == len(rows)


def test_the_end_bound_row_is_requested_and_stored_and_the_next_settlement_is_not() -> None:
    clock = FakeClock()
    probe = FakeFundingSource(clock)
    go(probe, RecordingFundingSink(), clock)
    end = probe.hist_calls[0][2]
    clock = FakeClock()
    rows = settlement_rows(end - 10 * HOUR, end + 3 * HOUR, offset_ms=0)
    rows = [row for row in rows if row["fundingTime"] != end] + [{**rows[0], "fundingTime": end}]
    rows.sort(key=lambda row: row["fundingTime"])
    source, sink = FakeFundingSource(clock, dataset=rows), RecordingFundingSink()
    report = go(source, sink, clock)
    stored = [row.key.funding_time_ms for row in sink.writes[0][0]]
    assert stored[-1] == end and max(stored) <= end
    assert report.end_ms == end


@pytest.mark.parametrize("hours", [1, 500])
@pytest.mark.parametrize("delay", [0, 595_000])
def test_the_range_is_exactly_hours_wide_and_start_never_exceeds_end(hours: int, delay: int) -> None:
    clock = FakeClock()
    source, sink = FakeFundingSource(clock), RecordingFundingSink()
    report = go(source, sink, clock, config(hours=hours, publication_delay_ms=delay))
    (_, start, end, _) = source.hist_calls[0]
    assert end - start == hours * HOUR and start <= end
    assert 0 <= clock.wall_ms() - delay - end <= 1_000
    assert report.fetched == len([r for r in source.dataset if start <= r["fundingTime"] <= end])


def test_untrusted_time_makes_no_request_and_stores_nothing() -> None:
    clock = FakeClock()
    source, sink = FakeFundingSource(clock, time_error=SourceError("time request failed")), RecordingFundingSink()
    report = go(source, sink, clock)
    assert source.hist_calls == [] and sink.writes == []
    assert report.fetched == 0 and report.write is None
    assert report.selection.untrusted_reason is not None
    assert (report.start_ms, report.end_ms) == (None, None)


@pytest.mark.parametrize("error", [ExtensionPaused(300_000, 1), ExtensionStopped()], ids=["paused", "stopped"])
def test_a_429_or_418_on_the_time_request_is_not_reported_as_untrusted_time(error: SourceError) -> None:
    clock = FakeClock()
    source, sink = FakeFundingSource(clock, time_error=error), RecordingFundingSink()
    with pytest.raises(type(error)):
        go(source, sink, clock)
    assert source.hist_calls == [] and sink.writes == []


def test_an_unusable_symbol_is_refused_before_any_request_including_the_time_request() -> None:
    clock = FakeClock()
    source = FakeFundingSource(clock, market_error=FundingRateRequestRefused("not active"))
    sink = RecordingFundingSink()
    with pytest.raises(FundingRateRequestRefused):
        go(source, sink, clock)
    assert source.time_calls == [] and source.hist_calls == [] and sink.writes == []


def test_an_empty_answer_stores_nothing_and_is_not_an_error() -> None:
    clock = FakeClock()
    source, sink = FakeFundingSource(clock, dataset=[]), RecordingFundingSink()
    report = go(source, sink, clock)
    assert report.fetched == 0 and report.write is None and sink.writes == []
    assert report.selection.untrusted_reason is None and report.start_ms is not None


def test_a_regular_and_a_special_row_of_one_hour_are_stored_in_either_order() -> None:
    for order in (("Regular", "Special"), ("Special", "Regular")):
        clock = FakeClock()
        probe = FakeFundingSource(clock)
        go(probe, RecordingFundingSink(), clock)
        end = probe.hist_calls[0][2]
        clock = FakeClock()
        base = settlement_rows(end - HOUR, end - HOUR, offset_ms=3)[0]
        pair = [{**base, "rateType": order[0]}, {**base, "rateType": order[1], "fundingRate": "0.00020000"}]
        source, sink = FakeFundingSource(clock, dataset=pair), RecordingFundingSink()
        go(source, sink, clock)
        assert [row.key.rate_type for row in sink.writes[0][0]] == [RateType(order[0]), RateType(order[1])]


def test_a_negative_rate_and_an_empty_mark_price_are_stored_as_text_and_null() -> None:
    clock = FakeClock()
    row = settlement_rows(clock.wall_ms() // HOUR * HOUR - 5 * HOUR, clock.wall_ms() // HOUR * HOUR - 5 * HOUR)[0]
    source = FakeFundingSource(clock, dataset=[{**row, "fundingRate": "-0.00010000", "markPrice": ""}])
    sink = RecordingFundingSink()
    go(source, sink, clock)
    ((stored,),) = [rows for rows, _, _ in sink.writes]
    assert (stored.funding_rate, stored.mark_price) == ("-0.0001", None)


def test_an_off_hour_row_is_stored_and_counted() -> None:
    clock = FakeClock()
    hour = clock.wall_ms() // HOUR * HOUR - 5 * HOUR
    row = settlement_rows(hour, hour)[0]
    source = FakeFundingSource(clock, dataset=[{**row, "fundingTime": hour + 1_800_000}])
    sink = RecordingFundingSink()
    report = go(source, sink, clock)
    assert report.off_hour_rows == 1 and len(sink.writes[0][0]) == 1


def test_conflicts_are_in_the_write_report() -> None:
    clock = FakeClock()
    source, sink = FakeFundingSource(clock), RecordingFundingSink(conflicts=1)
    report = go(source, sink, clock)
    assert report.write is not None and len(report.write.conflicts) == 1


def test_a_full_page_is_flagged() -> None:
    clock = FakeClock()
    hour = clock.wall_ms() // HOUR * HOUR - 5 * HOUR
    dataset = [{**settlement_rows(hour, hour)[0], "fundingTime": hour - i * 1_000} for i in range(1_000, 0, -1)]
    source, sink = FakeFundingSource(clock, dataset=dataset), RecordingFundingSink()
    report = go(source, sink, clock, config(hours=500))
    assert report.fetched == 1_000 and report.full_page


def valid_page(clock: FakeClock) -> list[dict[str, object]]:
    """Three rows a request of this clock accepts (the last three settlements before its end bound)."""
    end = clock.wall_ms() - DELAY - 1_000
    hour = end // HOUR * HOUR
    return [dict(row) for row in settlement_rows(hour - 3 * HOUR, hour - HOUR)]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda rows: [rows[0], {**rows[1], "fundingTime": rows[1]["fundingTime"] - 10_000 * HOUR}, rows[2]],
        lambda rows: [rows[0], {**rows[1], "symbol": "ETHUSDT"}, rows[2]],
        lambda rows: [rows[0], {**rows[1], "rateType": "Other"}, rows[2]],
        lambda rows: [rows[0], {**rows[1], "fundingRate": 0.0001}, rows[2]],
        lambda rows: [rows[0], rows[1], dict(rows[1])],
        lambda rows: [rows[2], rows[1], rows[0]],
        lambda rows: {"code": -1130, "msg": "Invalid data sent for parameter 'startTime'."},
    ],
    ids=[
        "outside_range",
        "wrong_symbol",
        "unknown_rate_type",
        "float_rate",
        "repeated_key",
        "descending",
        "error_object",
    ],
)
def test_a_malformed_answer_is_rejected_entirely_and_writes_nothing(
    mutation: Callable[[list[dict[str, object]]], object],
) -> None:
    clock = FakeClock()
    source = FakeFundingSource(clock, response=mutation(valid_page(clock)))
    sink = RecordingFundingSink()
    with pytest.raises(FundingRateParseError):
        go(source, sink, clock)
    assert sink.writes == []


def test_the_valid_page_of_the_helper_is_accepted() -> None:
    clock = FakeClock()
    source, sink = FakeFundingSource(clock, response=valid_page(clock)), RecordingFundingSink()
    report = go(source, sink, clock)
    assert report.fetched == 3 and len(sink.writes[0][0]) == 3


@pytest.mark.parametrize("error", [SourceError("boom"), ExtensionPaused(300_000, 1)])
def test_source_errors_propagate_and_nothing_is_written(error: SourceError) -> None:
    clock = FakeClock()
    source, sink = FakeFundingSource(clock, hist_error=error), RecordingFundingSink()
    with pytest.raises(type(error)):
        go(source, sink, clock)
    assert sink.writes == []


def test_a_page_error_of_the_writer_propagates_unchanged() -> None:
    clock = FakeClock()
    partial = FundingRateWriteReport(100, 0, (), (), (), 0, 0, (), None, None)
    error = FundingRatePageError(FundingRateWriteOutcomeUnknown("commit failed"), 1, partial)
    with pytest.raises(FundingRatePageError) as raised:
        go(FakeFundingSource(clock), RecordingFundingSink(error), clock)
    assert raised.value is error


def test_a_pending_row_fails_closed_before_any_write(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    # Let the end bound run past the lower bound: the parser accepts the row, the selection must still catch it.
    monkeypatch.setattr(module, "forward_end_bound_ms", lambda delay, lower_bound: lower_bound + 6 * HOUR)
    source, sink = FakeFundingSource(clock), RecordingFundingSink()
    with pytest.raises(FundingRateSelectionError):
        go(source, sink, clock)
    assert sink.writes == []


def test_the_command_repeats_idempotently_with_the_same_rows() -> None:
    clock = FakeClock()
    source, sink = FakeFundingSource(clock), RecordingFundingSink()
    first = go(source, sink, clock)
    clock = FakeClock()
    source2, sink2 = FakeFundingSource(clock), RecordingFundingSink()
    second = go(source2, sink2, clock)
    assert [r.key for r in first.selection.eligible] == [r.key for r in second.selection.eligible]


@pytest.mark.parametrize("hours", [0, -1, 501])
def test_hours_outside_1_to_500_are_rejected(hours: int) -> None:
    with pytest.raises(ValueError, match="hours"):
        config(hours=hours)


@pytest.mark.parametrize("hours", [True, 1.0, "24"])
def test_hours_must_be_an_int(hours: object) -> None:
    with pytest.raises(TypeError):
        config(hours=hours)


@pytest.mark.parametrize("delay", [-1, MAX_PUBLICATION_DELAY_MS + 1])
def test_the_publication_delay_is_bounded(delay: int) -> None:
    with pytest.raises(ValueError, match="publication_delay_ms"):
        config(publication_delay_ms=delay)


def test_constants() -> None:
    assert (DEFAULT_HOURS, MAX_HOURS, MAX_PUBLICATION_DELAY_MS) == (24, 500, 595_000)
    assert config().symbol == SYMBOL
    with pytest.raises(ValueError):
        FetchFundingRateOnceConfig(symbol="")
    with pytest.raises(TypeError):
        FetchFundingRateOnceConfig(symbol=1)  # type: ignore[arg-type]
