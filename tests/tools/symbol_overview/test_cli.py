import pytest

from tools.symbol_overview.cli import build_parser, main


def test_defaults() -> None:
    args = build_parser().parse_args([])
    assert (args.api_url, args.port, args.host) == ("http://localhost:8000", 8765, "127.0.0.1")


def test_host_is_passed_to_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[tuple[str, int]] = []

    class FakeServer:
        server_address = ("0.0.0.0", 0)

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    def fake_make_server(source: object, host: str, port: int) -> FakeServer:
        recorded.append((host, port))
        return FakeServer()

    monkeypatch.setattr("tools.symbol_overview.cli.make_server", fake_make_server)
    assert main(["--host", "0.0.0.0", "--port", "0"]) == 0
    assert recorded == [("0.0.0.0", 0)]


def test_unbindable_host_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    # 192.0.2.1 is in the documentation range (TEST-NET-1), so it is not a local address and the bind fails.
    with pytest.raises(SystemExit) as raised:
        main(["--host", "192.0.2.1", "--port", "0"])
    assert raised.value.code == 2
    assert "cannot listen on" in capsys.readouterr().err


def test_empty_host_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--host", ""])
    assert raised.value.code == 2


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
