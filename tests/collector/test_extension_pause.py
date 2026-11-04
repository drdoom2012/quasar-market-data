import logging
from pathlib import Path

import pytest

from quasar_market_data.collector.extension_pause import ExtensionPause, ExtensionPaused, ExtensionStopped
from quasar_market_data.collector.kline_source import SourceError
from tests.collector.fetch_fakes import FakeClock
from tests.typing_helpers import untyped

S = 1000  # ms per second


def make() -> tuple[FakeClock, ExtensionPause]:
    clock = FakeClock()
    return clock, ExtensionPause(clock)


def test_the_stop_signals_are_source_errors() -> None:
    assert issubclass(ExtensionPaused, SourceError)
    assert issubclass(ExtensionStopped, SourceError)


def test_an_untouched_pause_lets_requests_through() -> None:
    _, pause = make()
    pause.check()


@pytest.mark.parametrize(
    ("retry_after_ms", "expected_ms"),
    [(0, 300 * S), (10 * S, 300 * S), (300 * S, 300 * S), (900 * S, 900 * S)],
)
def test_first_429_pauses_for_the_larger_of_retry_after_and_300_seconds(retry_after_ms: int, expected_ms: int) -> None:
    clock, pause = make()
    assert pause.record_rate_limited(retry_after_ms) == expected_ms
    with pytest.raises(ExtensionPaused) as raised:
        pause.check()
    assert raised.value.remaining_ms == expected_ms
    clock.advance(expected_ms - 1)
    with pytest.raises(ExtensionPaused) as raised:
        pause.check()
    assert raised.value.remaining_ms == 1
    clock.advance(1)
    pause.check()  # the pause ends at its exact end


def test_consecutive_429s_double_up_to_one_hour() -> None:
    clock, pause = make()
    applied = []
    for _ in range(6):
        applied.append(pause.record_rate_limited(0))
        clock.advance(applied[-1])
        pause.check()
    assert applied == [300 * S, 600 * S, 1200 * S, 2400 * S, 3600 * S, 3600 * S]


def test_a_large_retry_after_is_honoured_beyond_the_cap() -> None:
    _, pause = make()
    assert pause.record_rate_limited(7200 * S) == 7200 * S


def test_a_success_after_the_pause_resets_the_doubling() -> None:
    clock, pause = make()
    clock.advance(pause.record_rate_limited(0))
    clock.advance(pause.record_rate_limited(0))
    pause.record_success(pause.generation)
    assert pause.record_rate_limited(0) == 300 * S


def test_without_a_success_the_next_pause_still_doubles() -> None:
    clock, pause = make()
    clock.advance(pause.record_rate_limited(0))
    assert pause.record_rate_limited(0) == 600 * S


def test_two_429s_of_requests_in_flight_together_double_once() -> None:
    clock, pause = make()
    first = pause.record_rate_limited(0)
    second = pause.record_rate_limited(0)
    assert first == second == 300 * S
    clock.advance(300 * S)
    assert pause.record_rate_limited(0) == 600 * S  # the next distinct pause is the second step, not the third


def test_a_later_429_during_a_pause_never_shortens_it() -> None:
    clock, pause = make()
    pause.record_rate_limited(900 * S)
    clock.advance(100 * S)
    assert pause.record_rate_limited(0) == 800 * S  # the new pause alone (300 s) would be shorter: the end is kept
    with pytest.raises(ExtensionPaused) as raised:
        pause.check()
    assert raised.value.remaining_ms == 800 * S


def test_a_late_success_during_a_pause_does_not_shorten_it_or_reset_the_doubling() -> None:
    clock, pause = make()
    started = pause.generation  # the request started before the 429
    pause.record_rate_limited(0)
    pause.record_success(started)  # a request in flight when the 429 arrived completes
    with pytest.raises(ExtensionPaused):
        pause.check()
    clock.advance(300 * S)
    assert pause.record_rate_limited(0) == 600 * S


def test_a_ban_is_permanent_for_the_process_run() -> None:
    clock, pause = make()
    pause.record_banned()
    with pytest.raises(ExtensionStopped):
        pause.check()
    clock.advance(2 * 3600 * S)
    with pytest.raises(ExtensionStopped):
        pause.check()
    pause.record_success(pause.generation)
    with pytest.raises(ExtensionStopped):
        pause.check()


def test_a_ban_outranks_a_pause() -> None:
    _, pause = make()
    pause.record_rate_limited(0)
    pause.record_banned()
    with pytest.raises(ExtensionStopped):
        pause.check()


def test_a_ban_logs_one_error(caplog: pytest.LogCaptureFixture) -> None:
    _, pause = make()
    with caplog.at_level(logging.ERROR):
        pause.record_banned()
        pause.record_banned()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "418" in errors[0].getMessage()


def test_a_request_that_started_before_a_pause_cannot_reset_the_doubling_after_it_ended() -> None:
    clock, pause = make()
    started = pause.generation
    clock.advance(pause.record_rate_limited(0))  # first pause, now over
    pause.record_success(started)  # the slow request from before the pause finishes
    assert pause.record_rate_limited(0) == 600 * S  # still the second step
    clock.advance(600 * S)
    pause.record_success(pause.generation)  # a request that started after the pause ended
    assert pause.record_rate_limited(0) == 300 * S


def test_the_pause_signal_carries_the_end_on_the_monotonic_clock() -> None:
    clock, pause = make()
    now = clock.monotonic_ns()
    pause.record_rate_limited(0)
    with pytest.raises(ExtensionPaused) as raised:
        pause.check()
    assert raised.value.until_monotonic_ns == now + 300 * S * 1_000_000


@pytest.mark.parametrize("bad", [-1, True, 1.5, "10", None])
def test_retry_after_must_be_a_non_negative_int(bad: object) -> None:
    _, pause = make()
    with pytest.raises((TypeError, ValueError)):
        pause.record_rate_limited(untyped(bad))


def test_the_two_facts_of_p9_are_readable() -> None:
    clock, pause = make()
    assert (pause.paused_until_ns, pause.stopped) == (None, False)
    applied = pause.record_rate_limited(0)
    assert pause.paused_until_ns == clock.monotonic_ns() + applied * 1_000_000
    assert pause.stopped is False
    pause.record_banned()
    assert pause.stopped is True


def test_the_pause_imports_no_dataset_module() -> None:
    import ast

    import quasar_market_data.collector.extension_pause as module

    tree = ast.parse(Path(module.__file__ or "").read_text("utf-8"))
    imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    assert not [name for name in imported if "open_interest" in name or "funding" in name]
