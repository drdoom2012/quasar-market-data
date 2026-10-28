import asyncio
from pathlib import Path

import pytest

from quasar_market_data.collector import cli
from quasar_market_data.collector.clock_lead import ClockLeadMonitor
from quasar_market_data.collector.collection_loop import RoundReport, SymbolOutcome, SymbolResult
from quasar_market_data.collector.kline_source import SourceError
from tests.collector.fetch_fakes import AdvancingSleep, FakeClock, FakeSource
from tests.collector.test_cli_run import raise_on_nth, run_cli
from tests.collector.test_collection_loop import TF_MS, Harness, honouring, run

LOCAL = 1_792_677_000_000
FORMING = LOCAL - LOCAL % TF_MS


def test_every_round_observes_the_forming_slot_not_only_round_one() -> None:
    async def scenario() -> None:
        h = Harness()
        await honouring(h)
        seen: list[tuple[str, int, int]] = []
        h.run_kwargs["observe_forming_slot"] = lambda s, o, w: seen.append((s, o, w))
        h.start()
        await h.until_rounds(1)
        assert len(seen) == 1
        await h.advance_to_next_boundary()
        await h.until_rounds(2)
        assert len(seen) == 2
        h.stop.set()
        await h.task

    run(scenario)


def test_round_report_carries_the_round_maximum_without_a_user_hook() -> None:
    async def scenario() -> None:
        h = Harness()
        await honouring(h)
        h.source.forming_ahead_slots = 3
        h.start()  # no observe_forming_slot: the loop still measures
        await h.until_rounds(1)
        h.source.forming_ahead_slots = 1
        await h.advance_to_next_boundary()
        await h.until_rounds(2)
        assert [r.clock_lead_slots for r in h.rounds] == [3, 1]  # the maximum of that round, not of the run
        h.stop.set()
        await h.task

    run(scenario)


def test_round_report_has_no_lead_when_no_row_came_back() -> None:
    async def scenario() -> None:
        h = Harness()
        h.source.script["A"] = [SourceError("down")] * 6
        h.start()
        for _ in range(12):
            await h.t.advance(10_000)
            if h.rounds:
                break
        await h.until_rounds(1)
        assert h.rounds[0].results[0].outcome is SymbolOutcome.GAVE_UP
        assert h.rounds[0].clock_lead_slots is None
        h.stop.set()
        await h.task

    run(scenario)


def test_a_user_hook_does_not_change_collection_behaviour() -> None:
    async def collect(with_hook: bool) -> tuple[list[object], int]:
        h = Harness()
        await honouring(h)
        h.source.forming_ahead_slots = 3
        if with_hook:
            h.run_kwargs["observe_forming_slot"] = lambda s, o, w: None
        h.start()
        await h.until_rounds(1)
        await h.advance_to_next_boundary()
        await h.until_rounds(2)
        h.stop.set()
        await h.task
        return [r.results for r in h.rounds], h.sink.write_calls

    async def scenario() -> None:
        assert await collect(True) == await collect(False)

    asyncio.run(scenario())


@pytest.mark.parametrize(("lead", "text"), [(3, "3"), (None, "-")])
def test_round_line_prints_clock_lead_slots(capsys: pytest.CaptureFixture[str], lead: int | None, text: str) -> None:
    result = SymbolResult("A", SymbolOutcome.COLLECTED, 1, 2, None, (), None)

    cli._print_round(RoundReport(1, 1, (result,), (), clock_lead_slots=lead))

    assert capsys.readouterr().out.splitlines()[0].endswith(f" clock_step_ms=0 clock_lead_slots={text}")


def make_monitor(warnings: list[tuple[int, bool]]) -> ClockLeadMonitor:
    return ClockLeadMonitor(TF_MS, lambda lead, first_round: warnings.append((lead, first_round)))


def feed(monitor: ClockLeadMonitor, lead: int) -> None:
    monitor.observe("A", FORMING + lead * TF_MS, LOCAL)


def test_warning_on_a_new_run_wide_maximum_above_the_margin() -> None:
    warnings: list[tuple[int, bool]] = []
    monitor = make_monitor(warnings)

    for lead in (2, 3, 3, 4, 1):
        feed(monitor, lead)

    assert warnings == [(3, True), (4, True)]  # nothing for 2 (within the margin), nothing for the repeated 3 or the 1
    assert monitor.max_lead == 4


def test_no_warning_within_the_margin() -> None:
    warnings: list[tuple[int, bool]] = []
    monitor = make_monitor(warnings)

    for lead in (-1, 0, 1, 2):
        feed(monitor, lead)

    assert warnings == [] and monitor.max_lead == 2


def test_warnings_after_round_one_say_so() -> None:
    warnings: list[tuple[int, bool]] = []
    monitor = make_monitor(warnings)
    feed(monitor, 3)

    monitor.first_round_done()
    feed(monitor, 3)
    feed(monitor, 5)

    assert warnings == [(3, True), (5, False)]


def test_a_monitor_without_observations_has_no_maximum() -> None:
    assert make_monitor([]).max_lead is None


def test_the_cli_warning_texts(capsys: pytest.CaptureFixture[str]) -> None:
    cli._warn_clock_lead(3, True)
    cli._warn_clock_lead(4, False)

    head = "warning: the server's forming slot was {} slots ahead of the local clock (assumption A1 allows 2);"
    first = head.format(3) + " first-round requests may have missed up to 1 of the oldest wanted slots"
    later = head.format(4) + " every round line reports clock_lead_slots"
    assert capsys.readouterr().err.splitlines() == [first, later]


def test_the_cli_warns_in_a_later_round_with_the_every_round_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = FakeClock()
    clock.advance(4_000)
    source = FakeSource(clock, [], honour_limit=True, server_ahead_ms=0)
    stop = raise_on_nth(2)
    calls = [0]

    def on_sleep(ms: int) -> None:
        calls[0] += 1
        if calls[0] == 1:  # the wait between round one and round two: the server clock jumps ahead
            source.server_ahead_ms = 3 * TF_MS
        stop()  # the second wait (after round two) delivers SIGTERM

    code = run_cli(tmp_path / "market.db", source, clock, AdvancingSleep(clock, on_sleep=on_sleep))

    captured = capsys.readouterr()
    rounds = [line for line in captured.out.splitlines() if line.startswith("round ")]
    assert code == 0
    assert [line.rsplit(" ", 1)[1] for line in rounds[:2]] == ["clock_lead_slots=0", "clock_lead_slots=3"]
    warnings = [line for line in captured.err.splitlines() if line.startswith("warning: the server's forming slot")]
    assert len(warnings) == 1  # round one was within the margin; the new maximum in round two warns once
    assert warnings[0].endswith("every round line reports clock_lead_slots")  # not the first-round text
