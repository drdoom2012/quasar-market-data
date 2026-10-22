import argparse
from importlib.metadata import version

PROG = "quasar-collector"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=PROG)
    parser.add_argument("--version", action="version", version=f"{PROG} {version('quasar-market-data')}")
    parser.parse_args(argv)
    parser.print_usage()
    return 0
