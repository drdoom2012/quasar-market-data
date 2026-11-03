"""What the Collector measures about SQLite contention between open interest and candle writes (ADR-0003 P-9 rule 5).

``OpenInterestMeasurements`` collects, over one reporting window: how often and how long an open interest transaction
had to yield to a candle write, the duration of every open interest transaction (and, apart, of the transactions of
exactly 100 rows, the figure amendment A-1 reads: p99 <= 50 ms), the number of busy failures, and the duration of every
candle write (measured around ``_WriteWatch.write``). ``line`` renders the window as one ``oi-measurement`` line with
p50, p99 and max by nearest rank; ``reset`` starts the next window. Everything runs on the event loop, no locking.
"""

from collections.abc import Sequence

from quasar_market_data.collector.async_open_interest_writer import OpenInterestWriteReport
from quasar_market_data.collector.open_interest_writer import MAX_ROWS_PER_TRANSACTION

_NS_PER_MS = 1_000_000


def _rank(sorted_values: Sequence[int], percent: int) -> int:
    """Nearest-rank percentile of an ascending non-empty sequence."""
    rank = -(-percent * len(sorted_values) // 100)
    return sorted_values[max(rank, 1) - 1]


def _ms(value_ns: int) -> str:
    return f"{value_ns / _NS_PER_MS:.1f}"


def _series(prefix: str, values: Sequence[int], percentiles: tuple[int, ...]) -> list[str]:
    ordered = sorted(values)
    fields = [f"{prefix}_tx={len(ordered)}" if prefix != "candle" else f"candle_writes={len(ordered)}"]
    for percent in percentiles:
        fields.append(f"{prefix}_p{percent}_ms={_ms(_rank(ordered, percent)) if ordered else 'na'}")
    fields.append(f"{prefix}_max_ms={_ms(ordered[-1]) if ordered else 'na'}")
    return fields


class OpenInterestMeasurements:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._transactions: list[int] = []
        self._full_transactions: list[int] = []
        self._candle_writes: list[int] = []
        self._yields = 0
        self._yield_wait_ns = 0
        self._busy = 0

    def record_candle_write(self, duration_ns: int) -> None:
        self._candle_writes.append(duration_ns)

    def record_open_interest_write(self, report: OpenInterestWriteReport, row_count: int) -> None:
        """Record a finished page, or the ``partial`` report of a failed one; ``row_count`` is the page's rows."""
        for index, duration in enumerate(report.durations_ns):
            self._transactions.append(duration)
            if min(MAX_ROWS_PER_TRANSACTION, row_count - MAX_ROWS_PER_TRANSACTION * index) == MAX_ROWS_PER_TRANSACTION:
                self._full_transactions.append(duration)
        self._yields += report.yields
        self._yield_wait_ns += report.yield_wait_ns

    def record_busy(self) -> None:
        self._busy += 1

    def line(self, window_ms: int) -> str | None:
        """One ``oi-measurement`` line for the window, or ``None`` when nothing happened in it."""
        if not (self._transactions or self._candle_writes or self._yields or self._busy):
            return None
        fields = [f"oi-measurement window_s={window_ms // 1000}", f"yields={self._yields}"]
        fields.append(f"yield_wait_ms={_ms(self._yield_wait_ns)}")
        fields += _series("oi", self._transactions, (50, 99))
        fields += _series("oi100", self._full_transactions, (99,))
        fields.append(f"busy={self._busy}")
        fields += _series("candle", self._candle_writes, (50, 99))
        return " ".join(fields)
