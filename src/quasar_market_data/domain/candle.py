"""Candle identity and value objects.

Construction vs validation
--------------------------
Constructing a ``CandleKey`` or ``Candle`` checks **structure only**: field types, the ``venue`` and
``symbol`` formats, that ``open_time_ms`` is an in-range, timeframe-aligned integer whose candle end does
not overflow, and that numeric fields are decimal text (replaced by their canonical form). Construction
never inspects market content: signs and OHLC relationships (for example ``high < low``) are *not*
checked here. They belong to candle validation (``require_valid()``), which reports data-quality
failures explicitly instead of hiding them behind a constructor.

Identity
--------
A candle is identified by ``(venue, symbol, timeframe, open_time_ms)``.

* ``venue`` names the exchange and market segment (convention ``<exchange>-<segment>``, for example
  ``binance-usdm``) and must match ``[a-z0-9][a-z0-9_-]{0,31}``.
* ``symbol`` is the stored CCXT unified symbol (for example ``BTC/USDT:USDT``). The rule is a broad
  structural one: a ``str`` of 1-64 characters, none of them whitespace or non-printable. Unicode
  letters are allowed. There is deliberately no content rule tied to particular assets.
"""

import re
from dataclasses import dataclass
from typing import Final

from quasar_market_data.domain.decimal_text import canonical_decimal
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import ensure_timestamp_ms

MAX_SYMBOL_LENGTH: Final = 64

_VENUE: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}", re.ASCII)


@dataclass(frozen=True)
class CandleKey:
    venue: str
    symbol: str
    timeframe: Timeframe
    open_time_ms: int

    def __post_init__(self) -> None:
        if type(self.venue) is not str:
            raise TypeError(f"venue must be str, got {type(self.venue).__name__}")
        if _VENUE.fullmatch(self.venue) is None:
            raise ValueError(f"venue {self.venue!r} must match [a-z0-9][a-z0-9_-]{{0,31}}")

        if type(self.symbol) is not str:
            raise TypeError(f"symbol must be str, got {type(self.symbol).__name__}")
        if not 1 <= len(self.symbol) <= MAX_SYMBOL_LENGTH:
            raise ValueError(f"symbol must be 1-{MAX_SYMBOL_LENGTH} characters, got {len(self.symbol)}")
        if any(ch.isspace() or not ch.isprintable() for ch in self.symbol):
            raise ValueError(f"symbol {self.symbol!r} contains whitespace or non-printable characters")

        if type(self.timeframe) is not Timeframe:
            raise TypeError(f"timeframe must be Timeframe, got {type(self.timeframe).__name__}")

        open_time = ensure_timestamp_ms(self.open_time_ms, "open_time_ms")
        if not self.timeframe.is_aligned(open_time):
            raise ValueError(f"open_time_ms {open_time} is not aligned to timeframe {self.timeframe.label}")
        self.timeframe.end_exclusive_ms(open_time)  # raises ValueError if the candle end overflows


@dataclass(frozen=True)
class Candle:
    key: CandleKey
    open: str
    high: str
    low: str
    close: str
    volume: str
    turnover: str | None

    def __post_init__(self) -> None:
        if type(self.key) is not CandleKey:
            raise TypeError(f"key must be CandleKey, got {type(self.key).__name__}")
        for name in ("open", "high", "low", "close", "volume"):
            object.__setattr__(self, name, canonical_decimal(getattr(self, name)))
        if self.turnover is not None:
            object.__setattr__(self, "turnover", canonical_decimal(self.turnover))

    @property
    def end_exclusive_ms(self) -> int:
        return self.key.timeframe.end_exclusive_ms(self.key.open_time_ms)
