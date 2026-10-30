"""The container configuration file stays identical to the main one except the API bind address (S21 spec 5.2)."""

import tomllib
from pathlib import Path

from quasar_market_data.config.settings import ApiSettings, parse_settings

REPO = Path(__file__).resolve().parents[2]
MAIN = REPO / "config" / "quasar.toml"
COMPOSE = REPO / "config" / "quasar.compose.toml"


def test_compose_file_differs_from_the_main_file_only_in_api_host() -> None:
    main = tomllib.loads(MAIN.read_text(encoding="utf-8"))
    compose = tomllib.loads(COMPOSE.read_text(encoding="utf-8"))
    assert main["api"]["host"] == "127.0.0.1"
    assert compose["api"]["host"] == "0.0.0.0"
    del main["api"]["host"], compose["api"]["host"]
    assert compose == main


def test_compose_file_is_a_valid_settings_file() -> None:
    settings = parse_settings(COMPOSE.read_text(encoding="utf-8"), base_dir=Path("/app/config"))
    assert settings.api == ApiSettings("0.0.0.0", 8000)
    assert settings.database_path == Path("/app/data/quasar.db")
