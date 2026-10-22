import subprocess
import sys
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path

import pytest

from quasar_market_data.api.cli import main as api_main
from quasar_market_data.collector.cli import main as collector_main

Main = Callable[[list[str] | None], int]
CASES = [
    (collector_main, "quasar-collector", "quasar_market_data.collector.cli", "quasar_market_data.api"),
    (api_main, "quasar-api", "quasar_market_data.api.cli", "quasar_market_data.collector"),
]


@pytest.mark.parametrize(("main", "name", "_module", "_other"), CASES)
def test_version_flag_prints_name_and_version(
    main: Main, name: str, _module: str, _other: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"{name} {version('quasar-market-data')}"


@pytest.mark.parametrize(("main", "name", "_module", "_other"), CASES)
def test_no_arguments_prints_usage_and_returns_zero(
    main: Main, name: str, _module: str, _other: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()


@pytest.mark.parametrize(("_main", "name", "_module", "_other"), CASES)
def test_installed_console_script_runs(_main: Main, name: str, _module: str, _other: str) -> None:
    result = subprocess.run(
        [str(Path(sys.executable).parent / name), "--version"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert result.stdout.startswith(name)


@pytest.mark.parametrize(("_main", "_name", "module", "other"), CASES)
def test_process_does_not_import_the_other_process(_main: Main, _name: str, module: str, other: str) -> None:
    code = "import importlib, sys; importlib.import_module(sys.argv[1]); sys.exit(int(sys.argv[2] in sys.modules))"
    result = subprocess.run([sys.executable, "-c", code, module, other], check=False)
    assert result.returncode == 0
