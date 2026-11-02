import logging

import pytest

from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.collector.open_interest_gate import OpenInterestBanned, OpenInterestGate, OpenInterestPaused
from tests.collector.fetch_fakes import FakeClock
from tests.typing_helpers import untyped

S = 1000  # ms per second


def make() -> tuple[FakeClock, OpenInterestGate]:
    clock = FakeClock()
    return clock, OpenInterestGate(clock)


def test_the_stop_signals_are_source_errors() -> None:
    assert issubclass(OpenInterestPaused, SourceError)
    assert issubclass(OpenInterestBanned, SourceError)


def test_an_untouched_gate_lets_requests_through() -> None:
    _, gate = make()
    gate.check()


@pytest.mark.parametrize(
    ("retry_after_ms", "expected_ms"),
    [(0, 300 * S), (10 * S, 300 * S), (300 * S, 300 * S), (900 * S, 900 * S)],
)
def test_first_429_pauses_for_the_larger_of_retry_after_and_300_seconds(retry_after_ms: int, expected_ms: int) -> None:
    clock, gate = make()
    assert gate.record_rate_limited(retry_after_ms) == expected_ms
    with pytest.raises(OpenInterestPaused) as raised:
        gate.check()
    assert raised.value.remaining_ms == expected_ms
    clock.advance(expected_ms - 1)
    with pytest.raises(OpenInterestPaused) as raised:
        gate.check()
    assert raised.value.remaining_ms == 1
    clock.advance(1)
    gate.check()  # the pause ends at its exact end


def test_consecutive_429s_double_up_to_one_hour() -> None:
    clock, gate = make()
    applied = []
    for _ in range(6):
        applied.append(gate.record_rate_limited(0))
        clock.advance(applied[-1])
        gate.check()
    assert applied == [300 * S, 600 * S, 1200 * S, 2400 * S, 3600 * S, 3600 * S]


def test_a_large_retry_after_is_honoured_beyond_the_cap() -> None:
    _, gate = make()
    assert gate.record_rate_limited(7200 * S) == 7200 * S


def test_a_success_after_the_pause_resets_the_doubling() -> None:
    clock, gate = make()
    clock.advance(gate.record_rate_limited(0))
    clock.advance(gate.record_rate_limited(0))
    gate.record_success()
    assert gate.record_rate_limited(0) == 300 * S


def test_without_a_success_the_next_pause_still_doubles() -> None:
    clock, gate = make()
    clock.advance(gate.record_rate_limited(0))
    assert gate.record_rate_limited(0) == 600 * S


def test_two_429s_of_requests_in_flight_together_double_once() -> None:
    clock, gate = make()
    first = gate.record_rate_limited(0)
    second = gate.record_rate_limited(0)
    assert first == second == 300 * S
    clock.advance(300 * S)
    assert gate.record_rate_limited(0) == 600 * S  # the next distinct pause is the second step, not the third


def test_a_later_429_during_a_pause_never_shortens_it() -> None:
    clock, gate = make()
    gate.record_rate_limited(900 * S)
    clock.advance(100 * S)
    assert gate.record_rate_limited(0) == 800 * S  # the new pause alone (300 s) would be shorter: the end is kept
    with pytest.raises(OpenInterestPaused) as raised:
        gate.check()
    assert raised.value.remaining_ms == 800 * S


def test_a_late_success_during_a_pause_does_not_shorten_it_or_reset_the_doubling() -> None:
    clock, gate = make()
    gate.record_rate_limited(0)
    gate.record_success()  # a request in flight when the 429 arrived completes
    with pytest.raises(OpenInterestPaused):
        gate.check()
    clock.advance(300 * S)
    assert gate.record_rate_limited(0) == 600 * S


def test_a_ban_is_permanent_for_the_process_run() -> None:
    clock, gate = make()
    gate.record_banned()
    with pytest.raises(OpenInterestBanned):
        gate.check()
    clock.advance(2 * 3600 * S)
    with pytest.raises(OpenInterestBanned):
        gate.check()
    gate.record_success()
    with pytest.raises(OpenInterestBanned):
        gate.check()


def test_a_ban_outranks_a_pause() -> None:
    _, gate = make()
    gate.record_rate_limited(0)
    gate.record_banned()
    with pytest.raises(OpenInterestBanned):
        gate.check()


def test_a_ban_logs_one_error(caplog: pytest.LogCaptureFixture) -> None:
    _, gate = make()
    with caplog.at_level(logging.ERROR):
        gate.record_banned()
        gate.record_banned()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "418" in errors[0].getMessage()


@pytest.mark.parametrize("bad", [-1, True, 1.5, "10", None])
def test_retry_after_must_be_a_non_negative_int(bad: object) -> None:
    _, gate = make()
    with pytest.raises((TypeError, ValueError)):
        gate.record_rate_limited(untyped(bad))
