"""TA-Lib adapter: the only module that imports talib and numpy (design S18 section 5.6).

TA-Lib global state (unstable period, compatibility) is never touched; defaults apply.
"""

from collections.abc import Sequence

import numpy
import talib

from quasar_market_data.domain.indicator_series import Compute
from quasar_market_data.domain.indicators import IndicatorSpec


def compute_function(spec: IndicatorSpec) -> Compute:
    """The float64 compute function of ``spec``; one output sequence per indicator output."""
    if spec.name == "rsi":
        period = spec.param("period")

        def rsi(closes: Sequence[float]) -> Sequence[Sequence[float]]:
            result = talib.RSI(numpy.asarray(closes, dtype=numpy.float64), timeperiod=period)
            return (result.tolist(),)

        return rsi
    raise ValueError(f"no engine function for indicator {spec.name!r}")
