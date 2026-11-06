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


def test_both_files_carry_the_same_522_symbol_universe_and_the_same_seven_day_backfill() -> None:
    main = parse_settings(MAIN.read_text(encoding="utf-8"), base_dir=MAIN.parent)
    compose = parse_settings(COMPOSE.read_text(encoding="utf-8"), base_dir=COMPOSE.parent)
    assert len(main.venues[0].symbols) == 522
    assert compose.venues[0].symbols == main.venues[0].symbols
    for settings in (main, compose):
        assert settings.collector.backfill.days == 7
        assert settings.open_interest is not None and settings.open_interest.backfill_days == 7
        assert settings.funding_rate is not None and settings.funding_rate.backfill_days == 7


def test_compose_file_is_a_valid_settings_file() -> None:
    settings = parse_settings(COMPOSE.read_text(encoding="utf-8"), base_dir=Path("/app/config"))
    assert settings.api == ApiSettings("0.0.0.0", 8000)
    assert settings.database_path == Path("/app/data/quasar.db")
