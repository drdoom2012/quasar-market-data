"""``quasar-collector run`` with backfill (S16): the idle-time lines, the round fields and the warnings."""

import asyncio
import re
import signal
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.collector import cli
from quasar_market_data.collector.backfill import SliceReport, SliceStatus
from quasar_market_data.collector.cli import main
from quasar_market_data.collector.collection_loop import RoundReport
from quasar_market_data.collector.kline_source import KlineSource, SourceError
from quasar_market_data.domain.backfill import Attribution
from tests.collector.backfill_fakes import Env
from tests.collector.test_backfill_run import HybridSource, drive_forever_step
from tests.config_files import write_config

SYMBOL = "BTC/USDT:USDT"
FORBIDDEN = re.compile(r"\b(done|filled|complete|completed|unavailable)\b")


def run_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_setup: object = None,
    stop_after_slice: bool = True,
    **values: Any,
) -> tuple[int, HybridSource, Env]:
    env = Env(tmp_path, symbols=(SYMBOL,))
    env.store([env.slot(0), env.slot(1), env.slot(12)], symbol=SYMBOL)  # a hole of ten slots (2..11)
    source = HybridSource(env.t)
    if callable(source_setup):
        source_setup(source)

    if stop_after_slice:
        real = cli._print_slice

        def print_then_stop(report: SliceReport) -> None:
            real(report)
            signal.raise_signal(signal.SIGTERM)

        monkeypatch.setattr(cli, "_print_slice", print_then_stop)

    async def open_source() -> KlineSource:
        asyncio.ensure_future(drive_forever_step(env.t))  # simulated time moves on its own inside the CLI's loop
        return source

    config = write_config(env.db.parent, database_path=env.db.name, symbols=(SYMBOL,), max_concurrency=1, **values)
    argv = ["run", str(config)]
    code = main(argv, open_source=open_source, clock=env.t, sleep=env.t.sleep)
    return code, source, env


def test_run_backfills_after_the_first_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, source, env = run_cli(tmp_path, monkeypatch, backfill_days=1)
    out = capsys.readouterr().out
    assert code == 0
    (line,) = [line for line in out.splitlines() if line.startswith(f"backfill symbol={SYMBOL} ")]
    assert "status=progress" in line and "attribution=ok" in line
    assert re.search(r"served_all=[1-9]", line) and re.search(r"recovered=[1-9]\d*", line)
    assert {env.slot(i) for i in range(2, 12)} <= set(env.stored(SYMBOL))
    assert source.page_requests >= 1
    assert not FORBIDDEN.search(line)


def test_no_backfill_disables_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def stop_on_second_round(source: HybridSource) -> None:
        source.on_round = lambda n: signal.raise_signal(signal.SIGTERM) if n == 2 else None

    code, source, env = run_cli(
        tmp_path, monkeypatch, source_setup=stop_on_second_round, stop_after_slice=False, backfill_enabled=False
    )
    out = capsys.readouterr().out
    body = "\n".join(line for line in out.splitlines() if not line.startswith("config "))
    assert code == 0 and "backfill" not in body
    assert source.page_requests == 0  # the source never saw a range
    assert not {env.slot(i) for i in range(2, 12)} & set(env.stored(SYMBOL))


def test_a_backoff_is_reported_and_exit_stays_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing(source: HybridSource) -> None:
        source.page_error = SourceError("exchange down")

    code, _, _ = run_cli(tmp_path, monkeypatch, source_setup=failing, backfill_days=1)
    out = capsys.readouterr().out
    assert code == 0
    assert "status=backoff" in out


def round_report(**fields: int) -> RoundReport:
    return RoundReport(None, 1_792_677_036_000, (), (), **fields)


def test_round_lines_show_lateness_overrun_skips_and_clock_step(capsys: pytest.CaptureFixture[str]) -> None:
    cli._print_round(
        round_report(lateness_ms=40_000, idle_overrun_ms=40_000, skipped_rounds=2, clock_step_ms=-3_600_000)
    )
    out = capsys.readouterr().out
    assert "lateness_ms=40000 idle_overrun_ms=40000 skipped_rounds=2 clock_step_ms=-3600000" in out


def test_an_idle_overrun_or_a_skipped_round_is_warned_on_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    cli._print_round(round_report())
    assert capsys.readouterr().err == ""
    cli._print_round(round_report(lateness_ms=5, idle_overrun_ms=5))
    assert "idle_overrun_ms=5" in capsys.readouterr().err
    cli._print_round(round_report(skipped_rounds=1))
    assert "skipped_rounds=1" in capsys.readouterr().err


def test_a_suspended_backfiller_is_reported_on_stdout_and_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    cli._report_suspension(2, 1_234)
    captured = capsys.readouterr()
    assert captured.out == "backfill suspended reason=idle_overrun consecutive=2 until=restart\n"
    assert captured.err == (
        "warning: backfill suspended after 2 consecutive idle overruns (last idle_overrun_ms=1234); restart to resume\n"
    )


def test_unverified_numbers_are_printed_as_the_word_unverified(capsys: pytest.CaptureFixture[str]) -> None:
    cli._print_slice(
        SliceReport("A", 1, 0, 1, 0, 3, 0, 0, 0, 0, 0, None, 10, Attribution.REPORT_MISMATCH, SliceStatus.PROGRESS)
    )
    line = capsys.readouterr().out
    assert "recovered=unverified" in line and "attribution=report_mismatch" in line and "new_in_targets=0" in line
    assert not FORBIDDEN.search(line)


def test_a_clock_step_is_shown_in_the_round_line_and_prints_no_suspension(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli._print_round(round_report(clock_step_ms=3_600_000, skipped_rounds=12))
    captured = capsys.readouterr()
    assert "clock_step_ms=3600000" in captured.out and "suspended" not in captured.out + captured.err
