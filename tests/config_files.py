"""Build configuration file text for tests (spec S20 5.1)."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

TEN_SYMBOLS: tuple[str, ...] = tuple(
    f"{base}/USDT:USDT" for base in ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC")
)


def config_text(
    *,
    database_path: str = "market.db",
    symbols: Sequence[str] = ("BTC/USDT:USDT", "ETH/USDT:USDT"),
    publication_delay_ms: int = 15_000,
    lag_threshold_ms: int = 60_000,
    limit: int = 100,
    max_concurrency: int = 4,
    min_request_interval_ms: int = 100,
    backfill_enabled: bool = True,
    backfill_days: int = 30,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> str:
    return (
        "[database]\n"
        f"path = {json.dumps(database_path)}\n"
        "\n"
        "[rule]\n"
        f"publication_delay_ms = {publication_delay_ms}\n"
        f"lag_threshold_ms = {lag_threshold_ms}\n"
        "\n"
        "[[venues]]\n"
        'id = "binance-usdm"\n'
        'timeframes = ["5m"]\n'
        f"symbols = {json.dumps(list(symbols))}\n"
        "\n"
        "[collector]\n"
        f"limit = {limit}\n"
        f"max_concurrency = {max_concurrency}\n"
        f"min_request_interval_ms = {min_request_interval_ms}\n"
        "\n"
        "[collector.backfill]\n"
        f"enabled = {'true' if backfill_enabled else 'false'}\n"
        f"days = {backfill_days}\n"
        "\n"
        "[api]\n"
        f"host = {json.dumps(host)}\n"
        f"port = {port}\n"
    )


def write_config(directory: Path, name: str = "quasar.toml", **values: Any) -> Path:
    path = directory / name
    path.write_text(config_text(**values), encoding="utf-8")
    return path
