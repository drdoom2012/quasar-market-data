from pathlib import Path
from typing import Any

import pytest
import uvicorn

from quasar_market_data.api import cli
from quasar_market_data.api.rule import MAX_LAG_THRESHOLD_MS, MAX_PUBLICATION_DELAY_MS
from quasar_market_data.config.settings import Instrument
from quasar_market_data.domain.open_interest_coverage import OpenInterestRule
from quasar_market_data.domain.timed_coverage import ExpectationRule
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import UnsupportedSqliteError
from tests.config_files import write_config

RunCalls = list[tuple[Any, dict[str, Any]]]


@pytest.fixture
def run_calls(monkeypatch: pytest.MonkeyPatch) -> RunCalls:
    calls: RunCalls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    return calls


def test_serve_binds_host_and_port_from_the_file(tmp_path: Path, run_calls: RunCalls) -> None:
    cfg = write_config(tmp_path, database_path="m.db", host="10.0.0.5", port=9001)

    assert cli.main(["serve", str(cfg)]) == 0

    assert len(run_calls) == 1
    assert (run_calls[0][1]["host"], run_calls[0][1]["port"]) == ("10.0.0.5", 9001)


def test_serve_passes_database_rule_and_universe_to_the_app(
    tmp_path: Path, run_calls: RunCalls, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = write_config(
        tmp_path,
        database_path="m.db",
        symbols=("ETH/USDT:USDT", "BTC/USDT:USDT"),
        publication_delay_ms=1000,
        lag_threshold_ms=2000,
    )
    seen: list[tuple[Path, Any, Any]] = []

    def recorder(db_path: Path, **kwargs: Any) -> str:
        seen.append((db_path, kwargs["rule"], kwargs["universe"]))
        return "app"

    monkeypatch.setattr(cli, "create_app", recorder)

    assert cli.main(["serve", str(cfg)]) == 0

    universe = (
        Instrument("binance-usdm", "BTC/USDT:USDT", "5m"),
        Instrument("binance-usdm", "ETH/USDT:USDT", "5m"),
    )
    assert len(seen) == 1
    assert (seen[0][0], seen[0][1]) == (tmp_path / "m.db", ExpectationRule(1000, 2000))
    assert tuple(seen[0][2]) == universe
    assert run_calls[0][0] == "app"


def test_serve_does_not_create_the_database(tmp_path: Path, run_calls: RunCalls) -> None:
    cfg = write_config(tmp_path, database_path="m.db")

    assert cli.main(["serve", str(cfg)]) == 0

    assert not (tmp_path / "m.db").exists()
    assert list(tmp_path.iterdir()) == [cfg]


def test_serve_refuses_an_unsupported_sqlite(
    tmp_path: Path, run_calls: RunCalls, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def old() -> None:
        raise UnsupportedSqliteError("SQLite 3.30.0 is older than the required 3.37.0")

    monkeypatch.setattr(cli, "ensure_sqlite_version", old)
    cfg = write_config(tmp_path, database_path="m.db")

    assert cli.main(["serve", str(cfg)]) == 1

    assert run_calls == []
    assert "older than the required" in capsys.readouterr().err


@pytest.mark.parametrize(
    "key, bound", [("publication_delay_ms", MAX_PUBLICATION_DELAY_MS), ("lag_threshold_ms", MAX_LAG_THRESHOLD_MS)]
)
def test_an_out_of_bound_value_exits_2_and_never_binds(
    tmp_path: Path, run_calls: RunCalls, capsys: pytest.CaptureFixture[str], key: str, bound: int
) -> None:
    ok = write_config(tmp_path, "ok.toml", database_path="m.db", **{key: bound})
    assert cli.main(["serve", str(ok)]) == 0  # the bound itself is valid
    run_calls.clear()
    capsys.readouterr()

    bad = write_config(tmp_path, "bad.toml", database_path="m.db", **{key: bound + 1})
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", str(bad)])

    assert exc.value.code == 2
    assert run_calls == []
    assert f"rule.{key}" in capsys.readouterr().err


@pytest.mark.parametrize(
    "switch",
    [["--db", "m.db"], ["--host", "x"], ["--port", "1"], ["--publication-delay-ms", "1"], ["--lag-threshold-ms", "1"]],
)
def test_no_switch_is_accepted_any_more(tmp_path: Path, run_calls: RunCalls, switch: list[str]) -> None:
    cfg = write_config(tmp_path, database_path="m.db")

    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", str(cfg), *switch])

    assert exc.value.code == 2
    assert run_calls == []


def test_an_invalid_file_exits_2_and_never_binds(
    tmp_path: Path, run_calls: RunCalls, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    created: list[Any] = []
    monkeypatch.setattr(cli, "create_app", lambda *args, **kwargs: created.append(kwargs))
    cfg = tmp_path / "quasar.toml"
    cfg.write_text("[database]\npath = 'm.db'\n", encoding="utf-8")  # the other tables are missing

    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", str(cfg)])

    assert exc.value.code == 2
    assert created == []
    assert run_calls == []
    assert "config" in capsys.readouterr().err


def test_a_missing_file_exits_2_and_never_binds(tmp_path: Path, run_calls: RunCalls) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", str(tmp_path / "absent.toml")])

    assert exc.value.code == 2
    assert run_calls == []


def test_serve_logs_the_config_line(tmp_path: Path, run_calls: RunCalls, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = write_config(tmp_path, database_path="m.db")

    assert cli.main(["serve", str(cfg)]) == 0

    err = capsys.readouterr().err
    assert f"config path={cfg.resolve()} sha256=" in err
    assert f"database={tmp_path.resolve() / 'm.db'}" in err


def test_the_database_path_does_not_depend_on_the_working_directory(
    tmp_path: Path, run_calls: RunCalls, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "etc"
    cfg_dir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    cfg = write_config(cfg_dir, database_path="m.db")
    seen: list[Path] = []
    monkeypatch.setattr(cli, "create_app", lambda db_path, **kwargs: seen.append(db_path))

    monkeypatch.chdir(elsewhere)
    assert cli.main(["serve", str(cfg)]) == 0

    assert seen == [cfg_dir.resolve() / "m.db"]


OPEN_INTEREST_TABLE = (
    '\n[open_interest]\nenabled = {enabled}\nperiod = "5m"\npublication_delay_ms = 15000\n'
    "round_offset_ms = 250000\nbudget_per_5min = 400\n"
)


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        (None, None),
        (OPEN_INTEREST_TABLE.format(enabled="false"), None),
        (OPEN_INTEREST_TABLE.format(enabled="true"), OpenInterestRule(1, 550_000, 2000, Timeframe("5m"))),
    ],
)
def test_serve_passes_the_open_interest_rule_of_the_file_to_the_app(
    tmp_path: Path, run_calls: RunCalls, monkeypatch: pytest.MonkeyPatch, table: str | None, expected: object
) -> None:
    cfg = write_config(tmp_path, database_path="m.db", lag_threshold_ms=2000)
    if table is not None:
        cfg.write_text(cfg.read_text(encoding="utf-8") + table, encoding="utf-8")
    seen: list[Any] = []

    def recorder(db_path: Path, **kwargs: Any) -> str:
        seen.append(kwargs["open_interest"])
        return "app"

    monkeypatch.setattr(cli, "create_app", recorder)

    assert cli.main(["serve", str(cfg)]) == 0

    assert seen == [expected]
