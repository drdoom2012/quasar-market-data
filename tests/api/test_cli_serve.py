from pathlib import Path
from typing import Any

import pytest
import uvicorn

from quasar_market_data.api import cli
from quasar_market_data.api.rule import DEFAULT_RULE, MAX_LAG_THRESHOLD_MS, MAX_PUBLICATION_DELAY_MS
from quasar_market_data.domain.timed_coverage import ExpectationRule
from quasar_market_data.storage.connection import UnsupportedSqliteError


@pytest.fixture
def run_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, dict[str, Any]]]:
    calls: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    return calls


def test_serve_defaults_to_localhost_8000(tmp_path: Path, run_calls: list[tuple[Any, dict[str, Any]]]) -> None:
    assert cli.main(["serve", "--db", str(tmp_path / "m.db")]) == 0

    assert len(run_calls) == 1
    assert run_calls[0][1]["host"] == "127.0.0.1"
    assert run_calls[0][1]["port"] == 8000


def test_serve_passes_host_and_port(tmp_path: Path, run_calls: list[tuple[Any, dict[str, Any]]]) -> None:
    cli.main(["serve", "--db", str(tmp_path / "m.db"), "--host", "10.0.0.5", "--port", "9001"])

    assert (run_calls[0][1]["host"], run_calls[0][1]["port"]) == ("10.0.0.5", 9001)


def test_serve_does_not_create_the_database(tmp_path: Path, run_calls: list[tuple[Any, dict[str, Any]]]) -> None:
    path = tmp_path / "missing" / "m.db"

    assert cli.main(["serve", "--db", str(path)]) == 0

    assert not path.exists()
    assert not path.parent.exists()
    assert list(tmp_path.iterdir()) == []


def test_serve_refuses_an_unsupported_sqlite(
    tmp_path: Path,
    run_calls: list[tuple[Any, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def old() -> None:
        raise UnsupportedSqliteError("SQLite 3.30.0 is older than the required 3.37.0")

    monkeypatch.setattr(cli, "ensure_sqlite_version", old)

    assert cli.main(["serve", "--db", str(tmp_path / "m.db")]) == 1

    assert run_calls == []
    assert "older than the required" in capsys.readouterr().err


@pytest.mark.parametrize("port", ["0", "65536", "abc", "-1"])
def test_bad_port_is_a_usage_error(tmp_path: Path, port: str) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--db", str(tmp_path / "m.db"), "--port", port])

    assert exc.value.code == 2


def test_serve_requires_db() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve"])

    assert exc.value.code == 2


def test_serve_passes_the_configured_rule_to_the_app(
    tmp_path: Path, run_calls: list[tuple[Any, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[Path, Any]] = []

    def recorder(db_path: Path, **kwargs: Any) -> str:
        seen.append((db_path, kwargs["rule"]))
        return "app"

    monkeypatch.setattr(cli, "create_app", recorder)

    args = ["serve", "--db", str(tmp_path / "m.db"), "--publication-delay-ms", "1000", "--lag-threshold-ms", "2000"]
    assert cli.main(args) == 0

    assert seen == [(tmp_path / "m.db", ExpectationRule(1000, 2000))]
    assert run_calls[0][0] == "app"


def test_defaults_equal_default_rule(
    tmp_path: Path, run_calls: list[tuple[Any, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(cli, "create_app", lambda db_path, **kwargs: seen.append(kwargs["rule"]))

    assert cli.main(["serve", "--db", str(tmp_path / "m.db")]) == 0

    assert seen == [DEFAULT_RULE]


@pytest.mark.parametrize(
    "option, bound",
    [("--publication-delay-ms", MAX_PUBLICATION_DELAY_MS), ("--lag-threshold-ms", MAX_LAG_THRESHOLD_MS)],
)
def test_an_out_of_bound_value_exits_2_and_never_binds(
    tmp_path: Path,
    run_calls: list[tuple[Any, dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
    option: str,
    bound: int,
) -> None:
    assert cli.main(["serve", "--db", str(tmp_path / "m.db"), option, str(bound)]) == 0  # the bound itself is valid
    run_calls.clear()

    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--db", str(tmp_path / "m.db"), option, str(bound + 1)])

    assert exc.value.code == 2
    assert run_calls == []
    assert option in capsys.readouterr().err


@pytest.mark.parametrize("option", ["--publication-delay-ms", "--lag-threshold-ms"])
@pytest.mark.parametrize("value", ["-1", "1.5", " 5", "1_0"])
def test_a_negative_a_non_integer_and_a_padded_value_are_refused(
    tmp_path: Path, run_calls: list[tuple[Any, dict[str, Any]]], option: str, value: str
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--db", str(tmp_path / "m.db"), f"{option}={value}"])

    assert exc.value.code == 2
    assert run_calls == []


@pytest.mark.parametrize("option", ["--publication-delay-ms", "--lag-threshold-ms"])
@pytest.mark.parametrize("value", [" 5", "1_0"])
def test_a_padded_value_is_refused_as_a_separate_token(
    tmp_path: Path, run_calls: list[tuple[Any, dict[str, Any]]], option: str, value: str
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--db", str(tmp_path / "m.db"), option, value])

    assert exc.value.code == 2
    assert run_calls == []
