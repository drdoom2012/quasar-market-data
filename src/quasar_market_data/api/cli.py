"""``quasar-api``: the read-only API process.

``serve [CONFIG]`` runs the HTTP API against an existing database. Everything it needs comes from the configuration
file (default ``config/quasar.toml``, S20): database path, bind address, expectation rule and universe. A file that
cannot be read or is invalid is a usage error (exit 2) before anything binds. The database need not exist yet: the API
starts and answers ``503 not_ready`` until the Collector has created and migrated it. The file is never created or
migrated here.
"""

import argparse
import sys
from importlib.metadata import version
from pathlib import Path

import uvicorn

from quasar_market_data.api.app import create_app
from quasar_market_data.api.rule import checked_rule
from quasar_market_data.config.settings import DEFAULT_CONFIG_PATH, ConfigError, load_settings
from quasar_market_data.storage.connection import StorageError, ensure_sqlite_version

PROG = "quasar-api"
EXIT_FAILED = 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG)
    parser.add_argument("--version", action="version", version=f"{PROG} {version('quasar-market-data')}")
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="serve the read-only HTTP API")
    serve.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"configuration file (default {DEFAULT_CONFIG_PATH})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command != "serve":
        parser.print_usage()
        return 0
    try:
        loaded = load_settings(args.config)
    except ConfigError as error:
        parser.error(f"config {args.config}: {error}")
    print(loaded.start_line(), file=sys.stderr, flush=True)
    settings = loaded.settings
    try:
        ensure_sqlite_version()  # once at startup; open_reader re-checks on every request
    except StorageError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_FAILED
    uvicorn.run(
        create_app(
            settings.database_path,
            rule=checked_rule(settings.rule.publication_delay_ms, settings.rule.lag_threshold_ms),
            universe=settings.instruments(),
        ),
        host=settings.api.host,
        port=settings.api.port,
        log_level="info",
    )
    return 0
