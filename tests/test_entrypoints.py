import subprocess
import sys
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path

import pytest
import uvicorn

from quasar_market_data.api import cli as api_cli
from quasar_market_data.api.cli import main as api_main
from quasar_market_data.collector import cli as collector_cli
from quasar_market_data.collector.cli import main as collector_main
from quasar_market_data.config.settings import Settings
from quasar_market_data.domain.timed_coverage import ExpectationRule
from tests.config_files import write_config

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


def test_the_api_rule_equals_the_collector_delay_from_the_same_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = write_config(tmp_path, database_path="m.db", publication_delay_ms=7_000, lag_threshold_ms=21_000)
    api_rules: list[ExpectationRule] = []
    collector_settings: list[Settings] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)
    monkeypatch.setattr(api_cli, "create_app", lambda db_path, **kwargs: api_rules.append(kwargs["rule"]))

    def spy(settings: Settings, *_args: object) -> int:
        collector_settings.append(settings)
        return 0

    monkeypatch.setattr(collector_cli, "_run_command", spy)

    assert api_main(["serve", str(cfg)]) == 0
    assert collector_main(["run", str(cfg)]) == 0

    assert len(api_rules) == 1
    assert len(collector_settings) == 1
    assert api_rules[0].publication_delay_ms == collector_settings[0].rule.publication_delay_ms == 7_000
    assert api_rules[0].lag_threshold_ms == collector_settings[0].rule.lag_threshold_ms == 21_000
