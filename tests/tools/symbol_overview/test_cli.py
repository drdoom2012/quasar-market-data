import pytest

from tools.symbol_overview.cli import build_parser, main


def test_defaults() -> None:
    args = build_parser().parse_args([])
    assert (args.api_url, args.port) == ("http://localhost:8000", 8765)


@pytest.mark.parametrize(
    "argv",
    [
        ["--api-url", "ftp://localhost:8000"],
        ["--api-url", "localhost:8000"],
        ["--port", "70000"],
        ["--port", "-1"],
        ["--port", "abc"],
        ["--unknown"],
    ],
)
def test_bad_arguments_are_a_usage_error_and_nothing_starts(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(argv)
    assert raised.value.code == 2
