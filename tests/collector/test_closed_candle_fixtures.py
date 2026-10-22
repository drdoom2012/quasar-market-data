"""Integration verification on the committed real Binance fixtures (structural and temporal checks only).

No network. Not test-driven: the code under test already exists, and a failure here is a finding to
investigate, not something to fix by loosening a rule.
"""

import json
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from typing import Any

from quasar_market_data.collector.binance_klines import parse_klines
from quasar_market_data.domain.closed_candles import DEFAULT_PUBLICATION_DELAY_MS, select_closed_candles
from quasar_market_data.domain.server_time import (
    DEFAULT_TIME_TRUST,
    NS_PER_MS,
    ServerTimeObservation,
    TimeRejection,
    TrustedTime,
    UntrustedTime,
    server_lower_bound,
)
from quasar_market_data.domain.timeframe import Timeframe

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "binance"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def to_observation(entry: dict[str, int]) -> ServerTimeObservation:
    return ServerTimeObservation(
        entry["server_time_ms"], entry["monotonic_before_ns"], entry["monotonic_after_ns"], entry["wall_after_ms"]
    )


def at_receipt(observation: ServerTimeObservation, previous: int | None = None) -> TrustedTime | UntrustedTime:
    return server_lower_bound(
        observation,
        now_monotonic_ns=observation.receive_monotonic_ns,
        now_wall_ms=observation.receive_wall_ms,
        previous_accepted_server_time_ms=previous,
        config=DEFAULT_TIME_TRUST,
    )


def first_observation(meta_name: str) -> ServerTimeObservation:
    return to_observation(load(meta_name)["time_observations"][0])


def test_forming_fixture_splits_closed_from_forming() -> None:
    meta = load("btcusdt_5m_forming.meta.json")
    candles = parse_klines(load("btcusdt_5m_forming_raw.json"), meta["instrument"], Timeframe(meta["interval"]))
    assert len(candles) == 2

    time = at_receipt(first_observation("btcusdt_5m_forming.meta.json"))
    assert time == TrustedTime(1_792_677_035_675)  # server time 1_792_677_035_725 minus the 50 ms margin
    selection = select_closed_candles(candles, DEFAULT_PUBLICATION_DELAY_MS, time)

    # Row 1: open 1_792_676_700_000, end + 15 s = 1_792_677_015_000 <= bound. Row 2: end + 15 s = 1_792_677_315_000.
    assert [c for c in selection.eligible] == [candles[0]]
    assert selection.pending == (candles[1],)
    assert selection.invalid == ()
    assert selection.untrusted_reason is None


def test_closed_fixtures_are_all_eligible() -> None:
    for prefix in ("btcusdt_5m_closed", "smallprice_5m_closed"):
        meta = load(f"{prefix}.meta.json")
        candles = parse_klines(load(f"{prefix}_raw.json"), meta["instrument"], Timeframe(meta["interval"]))
        assert len(candles) == 100, prefix

        time = at_receipt(first_observation(f"{prefix}.meta.json"))
        selection = select_closed_candles(candles, DEFAULT_PUBLICATION_DELAY_MS, time)
        assert len(selection.eligible) == 100, prefix
        assert selection.pending == () and selection.invalid == (), prefix


def test_all_fixture_time_observations_are_trusted() -> None:
    total = 0
    for path in sorted(FIXTURES.glob("*.meta.json")):
        for entry in json.loads(path.read_text()).get("time_observations", []):
            assert isinstance(at_receipt(to_observation(entry)), TrustedTime), (path.name, entry)
            total += 1
    assert total > 0


def unique_observations() -> list[ServerTimeObservation]:
    unique: dict[tuple[int, int, int], ServerTimeObservation] = {}
    for path in sorted(FIXTURES.glob("*.meta.json")):
        for entry in json.loads(path.read_text()).get("time_observations", []):
            unique[(entry["monotonic_before_ns"], entry["monotonic_after_ns"], entry["server_time_ms"])] = (
                to_observation(entry)
            )
    return sorted(unique.values(), key=lambda o: o.receive_monotonic_ns)


def test_lower_bound_never_exceeds_later_observation_ceiling() -> None:
    # An earlier observation's bound, evaluated at a later observation's receipt, may not pass what the
    # later observation itself shows: T(m1_j) <= S_j + rtt_j + 1 ms (the reading was truncated and taken
    # inside the window). Falsifiable on real data. The pairs include a system suspend, so the age and
    # divergence gates are lifted; the drift and margin stay at the ADR values.
    config = replace(DEFAULT_TIME_TRUST, max_age_ms=10**9, max_clock_divergence_ms=10**9)
    observations = unique_observations()
    assert len(observations) > 1

    for earlier, later in pairwise(observations):
        assert earlier.server_time_ms <= later.server_time_ms  # no server-time regression in monotonic order

    smallest_slack: int | None = None
    pairs = 0
    for i, earlier in enumerate(observations):
        for later in observations[i + 1 :]:
            result = server_lower_bound(
                earlier,
                now_monotonic_ns=later.receive_monotonic_ns,
                now_wall_ms=later.receive_wall_ms,
                previous_accepted_server_time_ms=None,
                config=config,
            )
            assert isinstance(result, TrustedTime), (earlier, later, result)
            rtt_ms = (later.receive_monotonic_ns - later.send_monotonic_ns) // NS_PER_MS
            slack = later.server_time_ms + rtt_ms + 1 - result.lower_bound_ms
            assert slack >= 0, (earlier, later, slack)
            smallest_slack = slack if smallest_slack is None else min(smallest_slack, slack)
            pairs += 1
    assert pairs == len(observations) * (len(observations) - 1) // 2
    assert smallest_slack is not None


def test_regressing_server_time_is_rejected_on_real_data() -> None:
    first, second = (to_observation(e) for e in load("btcusdt_5m_forming.meta.json")["time_observations"][:2])
    assert first.server_time_ms == 1_792_677_035_725
    assert second.server_time_ms == 1_792_677_036_344
    assert at_receipt(first, previous=second.server_time_ms) == UntrustedTime(TimeRejection.SERVER_TIME_REGRESSED)
    assert isinstance(at_receipt(second, previous=first.server_time_ms), TrustedTime)
