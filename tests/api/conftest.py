"""Fixtures for the API tests.

``no_outbound_network`` makes ``socket.socket.connect``, ``socket.socket.connect_ex`` and
``socket.create_connection`` raise while a test in this directory runs. It is a guard for the requests these
tests execute: it does not cover UDP ``sendto``, name resolution, other processes, ``tests/test_api_process.py``
or any code path the tests do not run (see INV-044 for what the register claims).
"""

import socket
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def no_outbound_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the API tests must not open connections")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
