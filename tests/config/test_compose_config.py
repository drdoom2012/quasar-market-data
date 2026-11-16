"""The container configuration file stays identical to the main one except the API bind address (S21 spec 5.2)."""

import tomllib
from pathlib import Path

from quasar_market_data.config.settings import ApiSettings, parse_settings
from quasar_market_data.domain.open_interest_cadence import open_interest_cadence

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


def test_shipped_open_interest_list() -> None:
    """ADR-0003 A-6 D-3: both files list the same 200 symbols, so the cadence is c = 1 at the shipped budget."""
    parsed = {}
    for path in (MAIN, COMPOSE):
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        listed = raw["open_interest"]["symbols"]
        assert isinstance(listed, list)
        assert len(listed) == 200
        assert listed == sorted(listed)
        assert len(set(listed)) == 200
        settings = parse_settings(path.read_text(encoding="utf-8"), base_dir=path.parent)
        assert settings.open_interest is not None
        assert set(listed) <= set(settings.venues[0].symbols)
        assert settings.open_interest.symbols == tuple(listed)
        assert open_interest_cadence(len(listed), settings.open_interest.budget_per_5min) == 1
        assert settings.open_interest.budget_per_5min == 400
        parsed[path.name] = listed
    assert parsed["quasar.toml"] == parsed["quasar.compose.toml"]
