from quasar_market_data.collector.async_open_interest_writer import OpenInterestWriteReport
from quasar_market_data.collector.open_interest_measurements import OpenInterestMeasurements, _rank

MS = 1_000_000


def report(durations_ms: list[int], yields: int = 0, wait_ms: int = 0) -> OpenInterestWriteReport:
    return OpenInterestWriteReport(
        0, 0, (), tuple(0 for _ in durations_ms), tuple(d * MS for d in durations_ms), yields, wait_ms * MS
    )


def fields(line: str | None) -> dict[str, str]:
    assert line is not None
    return dict(part.split("=", 1) for part in line.split()[1:])


def test_nearest_rank_percentiles() -> None:
    assert _rank([7], 50) == 7 and _rank([7], 99) == 7
    assert _rank([1, 2], 50) == 1 and _rank([1, 2], 99) == 2
    hundred = list(range(1, 101))
    assert _rank(hundred, 50) == 50 and _rank(hundred, 99) == 99
    hundred_one = list(range(1, 102))
    assert _rank(hundred_one, 50) == 51 and _rank(hundred_one, 99) == 100


def test_nothing_collected_means_no_line_and_reset_empties() -> None:
    m = OpenInterestMeasurements()
    assert m.line(3_600_000) is None
    m.record_busy()
    assert m.line(3_600_000) is not None
    m.reset()
    assert m.line(3_600_000) is None


def test_the_line_has_every_field() -> None:
    m = OpenInterestMeasurements()
    m.record_open_interest_write(report([5, 9, 12], yields=2, wait_ms=40), 250)
    m.record_candle_write(7 * MS)
    m.record_candle_write(11 * MS)
    m.record_busy()
    values = fields(m.line(3_600_000))
    assert values == {
        "window_s": "3600",
        "yields": "2",
        "yield_wait_ms": "40.0",
        "oi_tx": "3",
        "oi_p50_ms": "9.0",
        "oi_p99_ms": "12.0",
        "oi_max_ms": "12.0",
        "oi100_tx": "2",  # the first two transactions of a 250-row page hold 100 rows
        "oi100_p99_ms": "9.0",
        "oi100_max_ms": "9.0",
        "busy": "1",
        "candle_writes": "2",
        "candle_p50_ms": "7.0",
        "candle_p99_ms": "11.0",
        "candle_max_ms": "11.0",
    }


def test_a_small_page_feeds_no_sample_to_the_100_row_series() -> None:
    m = OpenInterestMeasurements()
    m.record_open_interest_write(report([3]), 25)
    values = fields(m.line(3_600_000))
    assert values["oi_tx"] == "1" and values["oi100_tx"] == "0" and values["oi100_p99_ms"] == "na"


def test_a_partial_report_counts_the_transactions_that_committed() -> None:
    m = OpenInterestMeasurements()
    m.record_open_interest_write(report([4, 6]), 500)  # two of five transactions committed before the failure
    values = fields(m.line(3_600_000))
    assert values["oi_tx"] == "2" and values["oi100_tx"] == "2"
