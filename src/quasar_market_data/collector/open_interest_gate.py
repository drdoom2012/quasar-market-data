"""Stop signals for open interest requests after Binance answers 429 or 418 (ADR-0003 P-10, point 2).

``OpenInterestGate`` is shared by every open interest request of one process. A ``429`` pauses all of them for
``max(Retry-After, min(300 s * 2^(n-1), 1 h))`` where ``n`` counts consecutive pauses (a success after the
pause resets it); a ``Retry-After`` above one hour is honoured, because Binance's answer is the authority. A
``418`` stops open interest for the rest of the process run. Neither touches candle collection: the candle
source has its own client and never sees this object.

While paused or banned ``check`` raises at once instead of waiting: a wait of up to an hour would hold an
in-flight slot, and the caller (OI-4) decides whether to skip, wait or stop. Both signals are ``SourceError``,
so a handler that only knows ``SourceError`` still fails closed.

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


class OpenInterestPaused(SourceError):
    """A 429 paused all open interest requests; ``remaining_ms`` is the time left (rounded up)."""

    def __init__(self, remaining_ms: int) -> None:
        self.remaining_ms = remaining_ms
        super().__init__(f"open interest requests are paused after a 429 for another {remaining_ms} ms")


class OpenInterestBanned(SourceError):
    """A 418 stopped open interest requests until the process restarts."""

    def __init__(self) -> None:
        super().__init__("open interest requests are stopped for this run after a 418 (IP ban)")


class OpenInterestGate:
    def __init__(self, clock: Clock, logger: logging.Logger | None = None) -> None:
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._banned = False
        self._pause_until_ns: int | None = None
        self._consecutive = 0

    def _remaining_ns(self) -> int:
        if self._pause_until_ns is None:
            return 0
        return max(0, self._pause_until_ns - self._clock.monotonic_ns())

    def check(self) -> None:
        if self._banned:
            raise OpenInterestBanned
        remaining_ns = self._remaining_ns()
        if remaining_ns > 0:
            raise OpenInterestPaused(-(-remaining_ns // _NS_PER_MS))

    def record_rate_limited(self, retry_after_ms: int) -> int:
        """Record a 429 and return the pause in force from now, in ms."""
        if type(retry_after_ms) is not int:
            raise TypeError(f"retry_after_ms must be int, got {type(retry_after_ms).__name__}")
        if retry_after_ms < 0:
            raise ValueError(f"retry_after_ms must be >= 0, got {retry_after_ms}")
        now = self._clock.monotonic_ns()
        if self._remaining_ns() == 0:
            self._consecutive += 1  # a new pause; a 429 inside a running one is the same pause
        doubled = min(_BASE_PAUSE_MS * 2 ** min(self._consecutive - 1, _MAX_DOUBLINGS), _MAX_DOUBLED_PAUSE_MS)
        until_ns = now + max(retry_after_ms, doubled) * _NS_PER_MS
        if self._pause_until_ns is None or until_ns > self._pause_until_ns:
            self._pause_until_ns = until_ns
        return -(-(self._pause_until_ns - now) // _NS_PER_MS)

    def record_success(self) -> None:
        if self._remaining_ns() == 0:
            self._consecutive = 0

    def record_banned(self) -> None:
        if not self._banned:
            self._banned = True
            self._logger.error("open interest stopped for this run: Binance answered 418 (IP banned)")
