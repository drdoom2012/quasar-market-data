"""Supported candle timeframes with UTC epoch alignment (integer arithmetic only)."""

from dataclasses import dataclass
from typing import Final

from quasar_market_data.domain.timestamps import ensure_timestamp_ms

_MINUTE_MS: Final = 60_000
_HOUR_MS: Final = 60 * _MINUTE_MS
_DAY_MS: Final = 24 * _HOUR_MS

# Every interval divides one UTC day, so epoch alignment equals UTC-day alignment.
_MILLISECONDS_BY_LABEL: Final[dict[str, int]] = {
    "1m": _MINUTE_MS,
    "3m": 3 * _MINUTE_MS,
    "5m": 5 * _MINUTE_MS,
    "15m": 15 * _MINUTE_MS,
    "30m": 30 * _MINUTE_MS,
    "1h": _HOUR_MS,
    "2h": 2 * _HOUR_MS,
    "4h": 4 * _HOUR_MS,
    "6h": 6 * _HOUR_MS,
    "8h": 8 * _HOUR_MS,
    "12h": 12 * _HOUR_MS,
    "1d": _DAY_MS,
}


@dataclass(frozen=True)
class Timeframe:
    label: str

    def __post_init__(self) -> None:
        if type(self.label) is not str:
            raise TypeError(f"timeframe label must be str, got {type(self.label).__name__}")
        if self.label not in _MILLISECONDS_BY_LABEL:
            raise ValueError(f"unsupported timeframe {self.label!r}; expected one of {sorted(_MILLISECONDS_BY_LABEL)}")

    @property
    def milliseconds(self) -> int:
        return _MILLISECONDS_BY_LABEL[self.label]

    def align_down(self, ts_ms: int) -> int:
        """Largest epoch-aligned open time that is <= ``ts_ms``."""
        ts = ensure_timestamp_ms(ts_ms)
        return ts - ts % self.milliseconds

    def is_aligned(self, ts_ms: int) -> bool:
        ts = ensure_timestamp_ms(ts_ms)
        return ts % self.milliseconds == 0

    def end_exclusive_ms(self, open_time_ms: int) -> int:
        """Exclusive end of the candle opening at ``open_time_ms`` (Binance ``closeTime`` + 1)."""
        open_time = ensure_timestamp_ms(open_time_ms, "open_time_ms")
        return ensure_timestamp_ms(open_time + self.milliseconds, "end_exclusive_ms")
