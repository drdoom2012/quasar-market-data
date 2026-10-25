from pathlib import Path
from typing import Any

import pytest
import uvicorn

from quasar_market_data.api import cli
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
