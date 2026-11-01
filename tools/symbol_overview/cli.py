"""Command line of the symbol overview tool: ``python -m tools.symbol_overview``."""

import argparse
import sys
from collections.abc import Sequence

from tools.symbol_overview.fetch import http_fetch
from tools.symbol_overview.refresh import Overviewer
from tools.symbol_overview.server import make_server

HOST = "127.0.0.1"  # the tool is for the person at this machine; it is not a service


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.symbol_overview",
        description="A local, read-only page that shows which symbols the Quasar API serves and whether they are fine.",
    )
    parser.add_argument(
        "--api-url", default="http://localhost:8000", help="base URL of the Quasar API (default %(default)s)"
    )
    parser.add_argument("--port", type=int, default=8765, help="port of this page on 127.0.0.1 (default %(default)s)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error(f"--port must be between 0 and 65535, got {args.port}")
    try:
        fetch = http_fetch(args.api_url)
    except ValueError as error:
        parser.error(str(error))
    server = make_server(Overviewer(fetch), HOST, args.port)
    print(
        f"Symbol overview: http://{HOST}:{server.server_address[1]}/  (API: {args.api_url})",
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
