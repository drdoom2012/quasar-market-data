"""What the funding task measures per reporting window (ADR-0004 P-9 rule 5, P-10; issue #19, soak #22).

``FundingRateMeasurements`` collects over one window: the number and mean time of requests (``d̄`` of P-10, from the
start of a request to its answer; the dispatcher starts a worker only when the limiter has room and a slot is free,
so this is the request itself), the duration of every funding transaction and, apart, of the transactions of exactly
100 rows (the figure the soak reads: p99 <= 50 ms, as ADR-0003 amendment A-1 does for open interest), the rows that
were off the hour, late events, conflicts, skipped series and the duration of every candle write (reported by the
candle path hook). ``line`` renders the window as one ``funding-measurement`` line (p50, p99 and max by nearest rank,
plus the peak request starts per 305 s the limiter saw); ``reset`` starts the next window. Everything runs on the
event loop, no locking.
"""

from collections.abc import Sequence

from quasar_market_data.collector.async_funding_rate_writer import FundingRateWriteReport
from quasar_market_data.collector.funding_rate_execution import PageResult
from quasar_market_data.collector.funding_rate_writer import MAX_ROWS_PER_TRANSACTION

_NS_PER_MS = 1_000_000


def _rank(sorted_values: Sequence[int], percent: int) -> int:
    """Nearest-rank percentile of an ascending non-empty sequence."""
    rank = -(-percent * len(sorted_values) // 100)
    return sorted_values[max(rank, 1) - 1]


def _ms(value_ns: float) -> str:
    return f"{value_ns / _NS_PER_MS:.1f}"


def _series(count_name: str, prefix: str, values: Sequence[int], percentiles: tuple[int, ...]) -> list[str]:
    ordered = sorted(values)
    fields = [f"{count_name}={len(ordered)}"]
    for percent in percentiles:
        fields.append(f"{prefix}_p{percent}_ms={_ms(_rank(ordered, percent)) if ordered else 'na'}")
    fields.append(f"{prefix}_max_ms={_ms(ordered[-1]) if ordered else 'na'}")
    return fields


class FundingRateMeasurements:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._transactions: list[int] = []
        self._full_transactions: list[int] = []
        self._candle_writes: list[int] = []
        self._requests: list[int] = []
        self._off_hour = 0
        self._late = 0
        self._skipped = 0
        self._conflicts = 0

    def record_candle_write(self, duration_ns: int) -> None:
        self._candle_writes.append(duration_ns)

    def _transactions_of(self, durations_ns: Sequence[int], row_count: int) -> None:
        for index, duration in enumerate(durations_ns):
            self._transactions.append(duration)
            if min(MAX_ROWS_PER_TRANSACTION, row_count - MAX_ROWS_PER_TRANSACTION * index) == MAX_ROWS_PER_TRANSACTION:
                self._full_transactions.append(duration)

    def record_page(self, result: PageResult) -> None:
        """A finished page: its transactions, request time, off-hour rows, late events and new conflicts."""
        self._transactions_of(result.durations_ns, result.rows)
        self._requests.append(result.request_ns)
        self._off_hour += result.off_hour_rows
        self._late += len(result.late_events)
        self._conflicts += sum(1 for conflict in result.conflicts if conflict.newly_recorded)

    def record_partial(self, report: FundingRateWriteReport, row_count: int, *, late_events: int) -> None:
        """The committed part of a failed page (``FundingRatePageError.partial``)."""
        self._transactions_of(report.durations_ns, row_count)
        self._late += late_events
        self._conflicts += sum(1 for conflict in report.conflicts if conflict.newly_recorded)

    def record_request(self, duration_ns: int) -> None:
        self._requests.append(duration_ns)

    def record_skipped(self, count: int) -> None:
        self._skipped += count

    def line(self, window_ms: int, peak_starts: int) -> str | None:
        """One ``funding-measurement`` line for the window, or ``None`` when nothing happened in it."""
        if not (
            self._transactions
            or self._candle_writes
            or self._requests
            or self._skipped
            or self._late
            or self._conflicts
            or self._off_hour
            or peak_starts
        ):
            return None
        mean = sum(self._requests) / len(self._requests) if self._requests else 0.0
        fields = [f"funding-measurement window_s={window_ms // 1000}", f"peak_starts_305s={peak_starts}"]
        fields += [f"requests={len(self._requests)}", f"mean_request_ms={_ms(mean)}"]
        fields += [
            f"off_hour_rows={self._off_hour}",
            f"late_events={self._late}",
            f"skipped={self._skipped}",
            f"conflicts={self._conflicts}",
        ]
        fields += _series("funding_tx", "funding", self._transactions, (50, 99))
        fields += _series("funding100_tx", "funding100", self._full_transactions, (99,))
        fields += _series("candle_writes", "candle", self._candle_writes, (50, 99))
        return " ".join(fields)
