"""The indicator series of one request from its stored inputs (contract api-v1 section 6.3.3; design S18 5.3).

Each indicator is evaluated on its own read range: the facts read once for the widest range are filtered to
``[read_start_ms(...), end)`` of that indicator before its slot states are derived, so no row before its own read
range and no other indicator of the request can influence it. Stored highs, lows and closes are converted with ``float(text)`` for
the computation only.
"""

from collections.abc import Sequence

from quasar_market_data.api.indicator_engine import compute_function
from quasar_market_data.api.indicator_reader import IndicatorInputs
from quasar_market_data.domain.indicator_series import IndicatorSeries, evaluate, read_start_ms
from quasar_market_data.domain.indicators import IndicatorSpec
from quasar_market_data.domain.timed_coverage import ExpectationRule, SlotState, timed_slot_states
from quasar_market_data.domain.timeframe import Timeframe


def indicator_results(
    inputs: IndicatorInputs, specs: Sequence[IndicatorSpec], *, as_of_ms: int, rule: ExpectationRule
) -> tuple[IndicatorSeries, ...]:
    """One ``IndicatorSeries`` per spec, in order. Raises ``ValueError`` when ``inputs`` do not cover a read range."""
    timeframe_ms = Timeframe(inputs.timeframe).milliseconds
    results: list[IndicatorSeries] = []
    for spec in specs:
        read_start = read_start_ms(inputs.start, spec.warmup_slots, timeframe_ms)
        if read_start < inputs.read_start:
            raise ValueError(f"the inputs start at {inputs.read_start}, after the read range of {spec.id}")

        def in_range(open_time: int, first: int = read_start) -> bool:
            return first <= open_time < inputs.end

        states = timed_slot_states(
            start=read_start,
            end=inputs.end,
            timeframe_ms=timeframe_ms,
            candles={t for t in inputs.prices if in_range(t)},
            conflicts={t for t in inputs.conflicts if in_range(t)},
            unavailable={t for t in inputs.unavailable if in_range(t)},
            newest_open_time_ms=inputs.newest_open_time_ms,
            as_of_ms=as_of_ms,
            rule=rule,
        )
        prices = {
            t: (float(inputs.prices[t][0]), float(inputs.prices[t][1]), float(inputs.prices[t][2]))
            for t, state in states
            if state is SlotState.PRESENT
        }
        results.append(
            evaluate(
                start=inputs.start,
                end=inputs.end,
                timeframe_ms=timeframe_ms,
                warmup_slots=spec.warmup_slots,
                states=states,
                prices=prices,
                compute=compute_function(spec),
                output_count=len(spec.outputs),
            )
        )
    return tuple(results)
