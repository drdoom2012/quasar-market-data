"""One server-time observation for many symbols (plan 2026-11-01-shared-server-time).

``SharedServerTime`` hands every caller the newest observation that the trust gate still accepts, and refreshes it
(single-flight) when it is older than the refresh age or when the gate rejects it.

Why sharing is safe (ADR-0001 P-4): the bound is ``S + age * (1 - drift) - margin`` and ``S`` is at most the true
server time at the instant of receipt (A1), so every observation, old or new, gives a bound that does not exceed the
true server time. The age of an observation costs only the drift reserve in how conservative the bound is; the age
gate (``max_age_ms``) and the clock-divergence gate still reject an observation that is too old or whose wall clock
moved against the monotonic one. The refresh age must be below ``max_age_ms`` so that an observation handed out is
far from the age gate.

An observation the gate rejects (a large RTT, a divergence) is returned once, so the caller's own gate reports the
reason, and is never served as usable twice: the next caller refreshes. A failed refresh falls back to the previous
observation, which the caller's gate then judges by age; with no previous observation the result is ``None``.

Two further rules keep a slow or dead time endpoint from stalling a round (final review, 2026-11-01):
while one caller is refreshing, the others are served the cached observation if the gate still accepts it instead of
queueing behind the request; and after a failed refresh nobody asks again for ``REFRESH_RETRY_MS``.

The refresh age is short on purpose (``DEFAULT_REFRESH_AFTER_MS``). Sharing saves almost nothing more at 30 s than at
5 s (one refresh per tens of symbols either way), while the age of an observation multiplies the clock-rate error
that the drift reserve of P-4 (assumption A3) must cover: a monotonic clock that is being slewed by the time daemon
can run fast against true time, and ``age * rate error`` is then added to the bound.
"""

import asyncio
from typing import Final, Protocol

from quasar_market_data.collector.clock import Clock
from quasar_market_data.collector.kline_source import KlineSource, SourceError
from quasar_market_data.domain.server_time import (
    NS_PER_MS,
    ServerTimeObservation,
    TimeTrustConfig,
    TrustedTime,
    server_lower_bound,
)

DEFAULT_REFRESH_AFTER_MS: Final = 5_000
REFRESH_RETRY_MS: Final = 1_000

__all__: Final = ["DEFAULT_REFRESH_AFTER_MS", "REFRESH_RETRY_MS", "ServerTimeProvider", "SharedServerTime"]


class ServerTimeProvider(Protocol):
    """Where ``fetch_once`` gets its server-time observation; ``None`` means no observation could be obtained."""

    async def observation(self, source: KlineSource) -> ServerTimeObservation | None: ...


class SharedServerTime:
    def __init__(self, clock: Clock, trust: TimeTrustConfig, refresh_after_ms: int) -> None:
        if type(refresh_after_ms) is not int:
            raise TypeError(f"refresh_after_ms must be int, got {type(refresh_after_ms).__name__}")
        if not 1 <= refresh_after_ms < trust.max_age_ms:
            raise ValueError(f"refresh_after_ms must be in 1..{trust.max_age_ms - 1}, got {refresh_after_ms}")
        self._clock = clock
        self._trust = trust
        self._refresh_after_ns = refresh_after_ms * NS_PER_MS
        self._lock = asyncio.Lock()
        self._cached: ServerTimeObservation | None = None
        self._no_retry_before_ns = 0

    def _trusted(self, *, fresh: bool) -> ServerTimeObservation | None:
        """The cached observation if the gate accepts it and, when ``fresh``, it is within the refresh age."""
        cached = self._cached
        if cached is None:
            return None
        now_ns, now_wall_ms = self._clock.monotonic_ns(), self._clock.wall_ms()
        if fresh and now_ns - cached.receive_monotonic_ns > self._refresh_after_ns:
            return None
        verdict = server_lower_bound(cached, now_ns, now_wall_ms, None, self._trust)
        return cached if isinstance(verdict, TrustedTime) else None

    async def observation(self, source: KlineSource) -> ServerTimeObservation | None:
        usable = self._trusted(fresh=True)
        if usable is not None:
            return usable
        if self._lock.locked():  # someone is refreshing: do not queue behind a request that may hang
            usable = self._trusted(fresh=False)
            if usable is not None:
                return usable
        async with self._lock:
            usable = self._trusted(fresh=True)  # another caller may have refreshed while this one waited
            if usable is not None:
                return usable
            if self._clock.monotonic_ns() < self._no_retry_before_ns:
                return self._cached  # a refresh failed a moment ago; the caller's gate judges what is cached
            try:
                fresh = await source.fetch_server_time()
            except SourceError:
                self._no_retry_before_ns = self._clock.monotonic_ns() + REFRESH_RETRY_MS * NS_PER_MS
                return self._cached
            self._cached = fresh
            return fresh
