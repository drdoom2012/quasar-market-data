"""A functional check of ``make bench-overview`` at its smallest size (no timing is asserted)."""

import json
import socket

import pytest

from tests.api.bench_overview_refresh import main

# Captured at import, before the autouse guard of conftest.py patches them for each test.
REAL_CONNECT = socket.socket.connect
REAL_CONNECT_EX = socket.socket.connect_ex
REAL_CREATE_CONNECTION = socket.create_connection


def lift_the_network_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    # The benchmark's own client talks to its own server process on 127.0.0.1: lift the guard for this test only.
    monkeypatch.setattr(socket.socket, "connect", REAL_CONNECT)
    monkeypatch.setattr(socket.socket, "connect_ex", REAL_CONNECT_EX)
    monkeypatch.setattr(socket, "create_connection", REAL_CREATE_CONNECTION)


def test_the_proxy_profile_runs_and_prints_the_ac_fields(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    lift_the_network_guard(monkeypatch)

    assert (
        main(
            [
                "--series",
                "3",
                "--profile",
                "smoke",
                "--oi-mode",
                "proxy",
                "--refreshes",
                "2",
                "--sustained-minutes",
                "0",
            ]
        )
        == 0
    )

    result = json.loads(capsys.readouterr().out)
    assert result["parameters"]["series"] == 3
    assert result["parameters"]["oi_mode"] == "proxy"
    assert result["environment"]["open_interest_cadence"] == 1  # 3 symbols with a budget of 400
    for key in ("platform", "machine", "python", "sqlite", "date"):
        assert result["environment"][key]
    assert set(result["evaluate"]) == {"AC1", "AC2", "AC3", "AC6", "AC7"}
    assert result["evaluate"]["AC2"]["pass"] is True
    assert result["evaluate"]["AC3"]["value"]["candle"]["n"] == 6  # 3 symbols x 2 refreshes
    assert result["evaluate"]["AC3"]["value"]["oi"]["n"] == 6
    assert result["sustained"] is None  # --sustained-minutes 0 skips it


def test_the_real_mode_runs_against_the_endpoint(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    lift_the_network_guard(monkeypatch)

    assert (
        main(
            [
                "--series",
                "3",
                "--profile",
                "smoke",
                "--oi-mode",
                "real",
                "--refreshes",
                "2",
                "--sustained-minutes",
                "0",
            ]
        )
        == 0
    )

    result = json.loads(capsys.readouterr().out)
    assert result["parameters"]["oi_mode"] == "real"
    assert result["evaluate"]["AC2"]["pass"] is True  # every call of both phases answered 200
    assert result["evaluate"]["AC3"]["value"]["oi"]["n"] == 6
