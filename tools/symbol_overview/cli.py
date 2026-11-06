"""Command line of the symbol overview tool: ``python -m tools.symbol_overview``."""

import argparse
import sys
from collections.abc import Sequence

from tools.symbol_overview.fetch import http_fetch
from tools.symbol_overview.refresh import Overviewer
from tools.symbol_overview.server import make_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.symbol_overview",
        description="A read-only page that shows which symbols the Quasar API serves and whether they are fine.",
    )
    parser.add_argument(
        "--api-url", default="http://localhost:8000", help="base URL of the Quasar API (default %(default)s)"
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="address of this page (default %(default)s; the container passes 0.0.0.0)",
    )
    parser.add_argument("--port", type=int, default=8765, help="port of this page on --host (default %(default)s)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.host:
        parser.error("--host must not be empty")
    if not 0 <= args.port <= 65535:
        parser.error(f"--port must be between 0 and 65535, got {args.port}")
    try:
        fetch = http_fetch(args.api_url)
    except ValueError as error:
        parser.error(str(error))
    try:
        server = make_server(Overviewer(fetch), args.host, args.port)
    except OSError as error:
        parser.error(f"cannot listen on {args.host}:{args.port}: {error}")
    print(
        f"Symbol overview: http://{args.host}:{server.server_address[1]}/  (API: {args.api_url})",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
