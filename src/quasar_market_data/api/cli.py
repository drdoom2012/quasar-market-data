"""``quasar-api``: the read-only API process.

``serve`` runs the HTTP API against an existing database. The database need not exist yet: the API starts and
answers ``503 not_ready`` until the Collector has created and migrated it. The file is never created or migrated here.
"""

import argparse
import sys
from importlib.metadata import version
from pathlib import Path

import uvicorn

from quasar_market_data.api.app import create_app
from quasar_market_data.storage.connection import StorageError, ensure_sqlite_version

PROG = "quasar-api"
DEFAULT_HOST = "127.0.0.1"  # localhost unless asked otherwise (contract section 1, Exposure)
DEFAULT_PORT = 8000
EXIT_FAILED = 1


def _port(text: str) -> int:
    value = int(text)
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError("must be in 1..65535")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG)
    parser.add_argument("--version", action="version", version=f"{PROG} {version('quasar-market-data')}")
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="serve the read-only HTTP API")
    serve.add_argument("--db", required=True, type=Path, help="SQLite database file written by the Collector")
    serve.add_argument("--host", default=DEFAULT_HOST, help=f"address to bind (default {DEFAULT_HOST})")
    serve.add_argument("--port", type=_port, default=DEFAULT_PORT, help=f"port to bind (default {DEFAULT_PORT})")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command != "serve":
        parser.print_usage()
        return 0
    try:
        ensure_sqlite_version()  # once at startup; open_reader re-checks on every request
    except StorageError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_FAILED
    uvicorn.run(create_app(args.db), host=args.host, port=args.port, log_level="info")
    return 0
