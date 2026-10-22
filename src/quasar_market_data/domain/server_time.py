"""Conservative lower bound of the exchange server time, with fail-closed rejection (ADR-0001 P-4).

The domain never reads a clock. The caller supplies every reading as an integer: monotonic readings in
nanoseconds (``*_ns``), wall-clock readings in milliseconds (``*_ms``), drift in parts per million.
``now_monotonic_ns`` and the observation's monotonic values must come from the same monotonic clock of
the same process; an observation must not outlive the process.

``server_lower_bound`` returns a value that does not exceed the true server time at ``now`` (see the
safety argument in ``docs/superpowers/plans/2026-10-22-s5-closed-candle-selection-and-parser.md``), so an
error can only delay confirming a candle, never confirm it early. Time that cannot be trusted yields
``UntrustedTime`` with the reason, never a guess. All arithmetic is integer arithmetic.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from quasar_market_data.domain.timestamps import ensure_timestamp_ms

NS_PER_MS: Final = 1_000_000
_PPM: Final = 1_000_000


def _ensure_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    return value


def _ensure_non_negative(value: object, name: str) -> int:
    number = _ensure_int(value, name)
    if number < 0:
        raise ValueError(f"{name} must be >= 0, got {number}")
    return number


@dataclass(frozen=True)
class ServerTimeObservation:
    """One server-time reading with the monotonic instants around the request and the wall time at receipt.

    ``receive_wall_ms`` is diagnostic: only the clock-divergence gate uses it.
    """

    server_time_ms: int
    send_monotonic_ns: int
    receive_monotonic_ns: int
    receive_wall_ms: int

    def __post_init__(self) -> None:
        ensure_timestamp_ms(self.server_time_ms, "server_time_ms")
        ensure_timestamp_ms(self.receive_wall_ms, "receive_wall_ms")
        send = _ensure_non_negative(self.send_monotonic_ns, "send_monotonic_ns")
        receive = _ensure_non_negative(self.receive_monotonic_ns, "receive_monotonic_ns")
        if receive < send:
            raise ValueError(f"receive_monotonic_ns {receive} is before send_monotonic_ns {send}")


@dataclass(frozen=True)
class TimeTrustConfig:
    max_rtt_ms: int
    max_uncertainty_ms: int
    max_age_ms: int
    max_drift_ppm: int
    precision_margin_ms: int
    max_clock_divergence_ms: int

    def __post_init__(self) -> None:
        for name in (
            "max_rtt_ms",
            "max_uncertainty_ms",
            "max_age_ms",
            "max_drift_ppm",
            "precision_margin_ms",
            "max_clock_divergence_ms",
        ):
            _ensure_non_negative(getattr(self, name), name)
        if self.max_drift_ppm >= _PPM:
            raise ValueError(f"max_drift_ppm must be < {_PPM}, got {self.max_drift_ppm}")


# Initial values of ADR-0001 P-4.
DEFAULT_TIME_TRUST: Final = TimeTrustConfig(
    max_rtt_ms=2000,
    max_uncertainty_ms=2000,
    max_age_ms=60000,
    max_drift_ppm=500,
    precision_margin_ms=50,
    max_clock_divergence_ms=100,
)


class TimeRejection(StrEnum):
    """Why time is untrusted, in the order the checks run."""

    NO_OBSERVATION = "NO_OBSERVATION"
    MONOTONIC_REGRESSION = "MONOTONIC_REGRESSION"
    RTT_TOO_LARGE = "RTT_TOO_LARGE"
    AGE_TOO_LARGE = "AGE_TOO_LARGE"
    UNCERTAINTY_TOO_LARGE = "UNCERTAINTY_TOO_LARGE"
    SERVER_TIME_REGRESSED = "SERVER_TIME_REGRESSED"
    CLOCK_DIVERGENCE = "CLOCK_DIVERGENCE"


@dataclass(frozen=True)
class TrustedTime:
    """A lower bound of the server time, in epoch milliseconds."""

    lower_bound_ms: int

    def __post_init__(self) -> None:
        _ensure_int(self.lower_bound_ms, "lower_bound_ms")


@dataclass(frozen=True)
class UntrustedTime:
    reason: TimeRejection

    def __post_init__(self) -> None:
        if type(self.reason) is not TimeRejection:
            raise TypeError(f"reason must be TimeRejection, got {type(self.reason).__name__}")


def server_lower_bound(
    observation: ServerTimeObservation | None,
    now_monotonic_ns: int,
    now_wall_ms: int,
    previous_accepted_server_time_ms: int | None,
    config: TimeTrustConfig,
) -> TrustedTime | UntrustedTime:
    """Return the server-time lower bound at ``now``, or the reason time cannot be trusted.

    Checks run in ``TimeRejection`` order. The clock-divergence gate only rejects; it never enters the bound.
    """
    _ensure_non_negative(now_monotonic_ns, "now_monotonic_ns")
    ensure_timestamp_ms(now_wall_ms, "now_wall_ms")
    if previous_accepted_server_time_ms is not None:
        ensure_timestamp_ms(previous_accepted_server_time_ms, "previous_accepted_server_time_ms")

    if observation is None:
        return UntrustedTime(TimeRejection.NO_OBSERVATION)

    age_ns = now_monotonic_ns - observation.receive_monotonic_ns
    rtt_ns = observation.receive_monotonic_ns - observation.send_monotonic_ns

    if age_ns < 0:
        return UntrustedTime(TimeRejection.MONOTONIC_REGRESSION)
    if rtt_ns > config.max_rtt_ms * NS_PER_MS:
        return UntrustedTime(TimeRejection.RTT_TOO_LARGE)
    if age_ns > config.max_age_ms * NS_PER_MS:
        return UntrustedTime(TimeRejection.AGE_TOO_LARGE)
    if rtt_ns > config.max_uncertainty_ms * NS_PER_MS:  # uncertainty = rtt
        return UntrustedTime(TimeRejection.UNCERTAINTY_TOO_LARGE)
    if previous_accepted_server_time_ms is not None and observation.server_time_ms < previous_accepted_server_time_ms:
        return UntrustedTime(TimeRejection.SERVER_TIME_REGRESSED)
    wall_elapsed_ns = (now_wall_ms - observation.receive_wall_ms) * NS_PER_MS
    if abs(wall_elapsed_ns - age_ns) > config.max_clock_divergence_ms * NS_PER_MS:
        return UntrustedTime(TimeRejection.CLOCK_DIVERGENCE)

    # Floor division only lowers the bound, which is the conservative direction.
    elapsed_ms = age_ns * (_PPM - config.max_drift_ppm) // (_PPM * NS_PER_MS)
    return TrustedTime(observation.server_time_ms + elapsed_ms - config.precision_margin_ms)
