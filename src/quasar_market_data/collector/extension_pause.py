"""``ExtensionPause``: the stop shared by extension datasets after Binance answers 429 or 418 (ADR-0004 P-9).

One in-process object, consulted before every extension-dataset request and set by whoever receives the response.
Open interest and funding share one instance per process (amendment A-3 to ADR-0003, ADR-0004 P-9), each with its
own limiter: a ``429`` pauses all requests for ``max(Retry-After, min(300 s * 2^(n-1), 1 h))`` where
``n`` counts consecutive pauses (a success after the pause resets it); a ``Retry-After`` above one hour is
honoured, because Binance's answer is the authority. A ``418`` stops every extension dataset for the rest of the
process run.
The two facts P-9 names are readable: ``paused_until_ns`` (monotonic) and ``stopped``. Candle collection neither
reads nor is paused by it (import contract ``candle collection does not use extension datasets``).

While paused or stopped ``check`` raises at once instead of waiting: a wait of up to an hour would hold a slot, and
the caller decides whether to skip, wait or stop. Both signals are ``SourceError``, so a handler that only knows
``SourceError`` still fails closed.

Two requests in flight together can both receive a 429; that is one pause, so the doubling advances once. A
success of a request that was in flight when the 429 arrived neither shortens the pause nor resets the doubling.
"""

import logging
from typing import Final

from quasar_market_data.collector.clock import Clock
from quasar_market_data.collector.kline_source import SourceError

_NS_PER_MS = 1_000_000
_BASE_PAUSE_MS: Final = 300_000
_MAX_DOUBLED_PAUSE_MS: Final = 3_600_000
_MAX_DOUBLINGS: Final = 10  # 300 s * 2^10 is far above the cap; bounds the integer


class ExtensionPaused(SourceError):
    """A 429 paused all extension dataset requests; ``remaining_ms`` is the time left (rounded up)."""

    def __init__(self, remaining_ms: int, until_monotonic_ns: int) -> None:
        self.remaining_ms = remaining_ms
        self.until_monotonic_ns = until_monotonic_ns  # the end of the pause on the gate's monotonic clock
        super().__init__(f"extension dataset requests are paused after a 429 for another {remaining_ms} ms")


class ExtensionStopped(SourceError):
    """A 418 stopped extension dataset requests until the process restarts."""

    def __init__(self) -> None:
        super().__init__("extension dataset requests are stopped for this run after a 418 (IP ban)")


class ExtensionPause:
    def __init__(self, clock: Clock, logger: logging.Logger | None = None) -> None:
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._banned = False
        self._pause_until_ns: int | None = None
        self._consecutive = 0
        self._generation = 0

    @property
    def paused_until_ns(self) -> int | None:
        """End of the latest pause on the monotonic clock, or ``None`` before the first 429 (it may lie in the past)."""
        return self._pause_until_ns

    @property
    def stopped(self) -> bool:
        """``True`` after a 418, for the rest of the process run."""
        return self._banned

    @property
    def generation(self) -> int:
        """Counts pauses and the ban. A request notes it when it starts and passes it to ``record_success``."""
        return self._generation

    def _remaining_ns(self) -> int:
        if self._pause_until_ns is None:
            return 0
        return max(0, self._pause_until_ns - self._clock.monotonic_ns())

    def check(self) -> None:
        if self._banned:
            raise ExtensionStopped
        if self._remaining_ns() > 0:
            raise self.paused()

    def paused(self) -> ExtensionPaused:
        """The signal for the pause in force now (call it only while a pause is running)."""
        assert self._pause_until_ns is not None
        return ExtensionPaused(-(-self._remaining_ns() // _NS_PER_MS), self._pause_until_ns)

    def record_rate_limited(self, retry_after_ms: int) -> int:
        """Record a 429 and return the pause in force from now, in ms."""
        if type(retry_after_ms) is not int:
            raise TypeError(f"retry_after_ms must be int, got {type(retry_after_ms).__name__}")
        if retry_after_ms < 0:
            raise ValueError(f"retry_after_ms must be >= 0, got {retry_after_ms}")
        now = self._clock.monotonic_ns()
        if self._remaining_ns() == 0:
            self._consecutive += 1  # a new pause; a 429 inside a running one is the same pause
            self._generation += 1
        doubled = min(_BASE_PAUSE_MS * 2 ** min(self._consecutive - 1, _MAX_DOUBLINGS), _MAX_DOUBLED_PAUSE_MS)
        until_ns = now + max(retry_after_ms, doubled) * _NS_PER_MS
        if self._pause_until_ns is None or until_ns > self._pause_until_ns:
            self._pause_until_ns = until_ns
        return -(-(self._pause_until_ns - now) // _NS_PER_MS)

    def record_success(self, started_generation: int) -> None:
        """Reset the doubling for a success of a request that started after the last pause began.

        A request that was in flight when a 429 arrived (it started in an earlier generation) neither shortens the
        pause nor resets the doubling, also when it finishes after the pause has ended.
        """
        if started_generation == self._generation and self._remaining_ns() == 0:
            self._consecutive = 0

    def record_banned(self) -> None:
        if not self._banned:
            self._banned = True
            self._generation += 1
            self._logger.error("extension datasets stopped for this run: Binance answered 418 (IP banned)")
