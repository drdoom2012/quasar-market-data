from quasar_market_data.collector.async_funding_rate_writer import FundingRateWriteReport
from quasar_market_data.collector.funding_rate_execution import PageResult
from quasar_market_data.collector.funding_rate_measurements import FundingRateMeasurements, _rank
from quasar_market_data.collector.funding_rate_writer import FundingRateConflict
from quasar_market_data.domain.funding_rate import FundingRate, FundingRateKey, RateType
from quasar_market_data.domain.funding_rate_capacity import RequestClass
from quasar_market_data.domain.funding_rate_scheduler import Plan

MS = 1_000_000
HOURLY = 3_600_000


def result(
    rows: int, durations_ms: list[int], *, request_ms: int = 100, conflicts: int = 0, late: int = 0, off: int = 0
) -> PageResult:
    key = FundingRateKey("binance-usdm", "BTC/USDT:USDT", 1_793_836_800_003, RateType.REGULAR)
    row = FundingRate(key, "0.0001", None)
    return PageResult(
        "BTC/USDT:USDT",
        Plan("BTC/USDT:USDT", "R", RequestClass.R, 1, 2, None),
        rows,
        rows,
        0,
        tuple(FundingRateConflict(row, row, True) for _ in range(conflicts)),
        tuple(key for _ in range(late)),
        off,
        None,
        None,
        tuple(d * MS for d in durations_ms),
        request_ms * MS,
        None,
    )


def fields(line: str | None) -> dict[str, str]:
    assert line is not None
    return dict(part.split("=", 1) for part in line.split()[1:])


def test_nearest_rank_percentiles() -> None:
    assert _rank([7], 50) == 7 and _rank([7], 99) == 7
    assert _rank([1, 2], 50) == 1 and _rank([1, 2], 99) == 2
    hundred = list(range(1, 101))
    assert _rank(hundred, 50) == 50 and _rank(hundred, 99) == 99
    assert _rank(list(range(1, 102)), 99) == 100


def test_nothing_collected_means_no_line_and_reset_empties() -> None:
    m = FundingRateMeasurements()
    assert m.line(HOURLY, 0) is None
    m.record_skipped(1)
    assert m.line(HOURLY, 0) is not None
    m.reset()
    assert m.line(HOURLY, 0) is None


def test_a_peak_alone_makes_a_line() -> None:
    assert FundingRateMeasurements().line(HOURLY, 3) is not None


def test_the_line_has_every_field_and_the_100_row_transactions_are_apart() -> None:
    m = FundingRateMeasurements()
    m.record_page(result(250, [5, 9, 12], request_ms=200, conflicts=2, late=1, off=3))  # 100, 100, 50 rows
    m.record_page(result(100, [20], request_ms=400))
    m.record_candle_write(7 * MS)
    m.record_candle_write(11 * MS)
    m.record_skipped(4)
    f = fields(m.line(HOURLY, 150))
    assert f["window_s"] == "3600" and f["peak_starts_305s"] == "150"
    assert f["requests"] == "2" and f["mean_request_ms"] == "300.0"
    assert (f["off_hour_rows"], f["late_events"], f["skipped"], f["conflicts"]) == ("3", "1", "4", "2")
    assert f["funding_tx"] == "4" and f["funding_max_ms"] == "20.0"
    assert f["funding100_tx"] == "3" and f["funding100_p99_ms"] == "20.0"  # 5 and 9 (pages of 250) and 20
    assert f["candle_writes"] == "2" and f["candle_p99_ms"] == "11.0"


def test_a_failed_page_counts_its_committed_transactions_and_late_events() -> None:
    m = FundingRateMeasurements()
    partial = FundingRateWriteReport(200, 0, (), (0, 0), (30 * MS, 40 * MS), 0, 0, (), None, None)
    m.record_partial(partial, 350, late_events=2)
    f = fields(m.line(HOURLY, 0))
    assert f["funding_tx"] == "2" and f["funding100_tx"] == "2" and f["late_events"] == "2"


def test_a_request_time_alone_is_recorded() -> None:
    m = FundingRateMeasurements()
    m.record_request(50 * MS)
    m.record_request(150 * MS)
    f = fields(m.line(HOURLY, 0))
    assert f["requests"] == "2" and f["mean_request_ms"] == "100.0"
