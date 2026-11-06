import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

from quasar_market_data.config.settings import (
    DEFAULT_CONFIG_PATH,
    ApiSettings,
    Backfill,
    CollectorSettings,
    ConfigError,
    Rule,
    load_settings,
)
from tests.config_files import write_config

REPO_ROOT = Path(__file__).resolve().parents[2]


def missing_file(tmp_path: Path) -> Path:
    return tmp_path / "absent.toml"


def a_directory(tmp_path: Path) -> Path:
    path = tmp_path / "dir.toml"
    path.mkdir()
    return path


def non_utf8_bytes(tmp_path: Path) -> Path:
    path = tmp_path / "binary.toml"
    path.write_bytes(b'[database]\npath = "\xff\xfe"\n')
    return path


def test_the_default_path_is_the_committed_file() -> None:
    assert Path("config/quasar.toml") == DEFAULT_CONFIG_PATH


def test_the_committed_file_loads() -> None:
    loaded = load_settings(REPO_ROOT / "config" / "quasar.toml")
    s = loaded.settings
    assert s.database_path == REPO_ROOT / "data" / "quasar.db"
    symbols = s.venues[0].symbols
    assert len(symbols) == 522
    assert len(set(symbols)) == 522
    assert (symbols[0], symbols[-1]) == ("0G/USDT:USDT", "龙虾/USDT:USDT")
    # today's switch values (spec 5.3)
    assert (s.rule, s.collector, s.api) == (
        Rule(15_000, 60_000),
        CollectorSettings(100, 4, 100, Backfill(True, 7)),
        ApiSettings("127.0.0.1", 8000),
    )


def test_the_hash_is_of_the_bytes_read(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    loaded = load_settings(cfg)
    assert loaded.sha256 == hashlib.sha256(cfg.read_bytes()).hexdigest()
    assert loaded.path == cfg.resolve()


def test_the_resolved_database_path_does_not_depend_on_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "conf").mkdir()
    cfg = write_config(tmp_path / "conf", database_path="../data/q.db")
    (tmp_path / "data").mkdir()
    (tmp_path / "elsewhere").mkdir()
    seen = set()
    for cwd in (tmp_path, tmp_path / "conf", tmp_path / "elsewhere"):
        monkeypatch.chdir(cwd)
        seen.add(load_settings(cfg).settings.database_path)
    assert seen == {(tmp_path / "data" / "q.db").resolve()}


def test_a_relative_config_from_two_working_directories_names_the_same_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "conf").mkdir()
    write_config(tmp_path / "conf", database_path="q.db")
    monkeypatch.chdir(tmp_path)
    first = load_settings(Path("conf/quasar.toml")).settings.database_path
    monkeypatch.chdir(tmp_path / "conf")
    second = load_settings(Path("quasar.toml")).settings.database_path
    assert first == second == (tmp_path / "conf" / "q.db").resolve()


def test_a_symlinked_config_resolves_against_the_real_files_directory(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").mkdir()
    real = write_config(tmp_path / "real", database_path="q.db")
    (tmp_path / "link" / "quasar.toml").symlink_to(real)
    loaded = load_settings(tmp_path / "link" / "quasar.toml")
    assert loaded.settings.database_path == (tmp_path / "real").resolve() / "q.db"
    assert loaded.path == real.resolve()


def test_an_absolute_database_path_stays_absolute(tmp_path: Path) -> None:
    (tmp_path / "conf").mkdir()
    (tmp_path / "abs").mkdir()
    target = (tmp_path / "abs" / "q.db").resolve()
    cfg = write_config(tmp_path / "conf", database_path=str(target))
    assert load_settings(cfg).settings.database_path == target


def test_a_missing_database_directory_shows_the_resolved_path(tmp_path: Path) -> None:
    cfg = write_config(tmp_path, database_path="nope/q.db")
    with pytest.raises(ConfigError) as e:
        load_settings(cfg)
    assert e.value.key == "database.path"
    assert str(tmp_path.resolve() / "nope") in e.value.problem


@pytest.mark.parametrize("make", [missing_file, a_directory, non_utf8_bytes])
def test_unreadable_files_are_config_errors(tmp_path: Path, make: Callable[[Path], Path]) -> None:
    path = make(tmp_path)
    with pytest.raises(ConfigError) as e:
        load_settings(path)
    assert e.value.key == "file"
    assert str(path.resolve()) in e.value.problem


def test_a_symlink_loop_is_a_config_error_not_a_traceback(tmp_path: Path) -> None:
    loop = tmp_path / "loop.toml"
    loop.symlink_to(loop)
    with pytest.raises(ConfigError) as e:
        load_settings(loop)
    assert e.value.key == "file"
    assert "cannot read" in e.value.problem and str(loop) in e.value.problem


def test_start_line_names_path_hash_and_database(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    loaded = load_settings(cfg)
    assert loaded.start_line() == (
        f"config path={cfg.resolve()} sha256={hashlib.sha256(cfg.read_bytes()).hexdigest()} "
        f"database={tmp_path.resolve() / 'market.db'}"
    )
