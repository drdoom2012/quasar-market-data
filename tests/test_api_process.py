"""The S10 acceptance demo: a separate API process reads a database that this test (playing the Collector) writes.

Every wait is bounded. The child's stderr goes to a file, never to an unread pipe. This file is outside
``tests/api/``, so the socket guard of that directory does not apply (and this test is not evidence for INV-044).
"""

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from socket import AF_INET, SOCK_STREAM, socket
from typing import Any

import pytest

from quasar_market_data.storage.connection import open_writer
from tests.api.seed import insert_candle, make_database

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="shuts the server down with SIGINT")

TF = 300_000
S = 1792643400000
QUERY = f"venue=binance-usdm&symbol=BTC/USDT:USDT&timeframe=5m&start={S}"
START_TIMEOUT_S = 15
MAX_START_ATTEMPTS = 3


class PortTaken(Exception):
    """The child exited because the port was taken in the gap between choosing and binding it."""


@dataclass
class Api:
    proc: "subprocess.Popen[bytes]"
    port: int
    db: Path


def slot(n: int) -> int:
    return S + n * TF


def free_port() -> int:
    with socket(AF_INET, SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def get(port: int) -> tuple[int, dict[str, str], dict[str, Any]]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"http://127.0.0.1:{port}/v1/candles?{QUERY}"
    try:
        with opener.open(url, timeout=2) as response:
            status, headers, body = response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        status, headers, body = error.code, dict(error.headers), error.read()
        error.close()
    return status, {k.lower(): v for k, v in headers.items()}, json.loads(body)


def wait_until_up(proc: "subprocess.Popen[bytes]", port: int, log: Path) -> tuple[int, dict[str, str], dict[str, Any]]:
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            text = log.read_text(errors="replace")
            if "address already in use" in text.lower():
                raise PortTaken
            pytest.fail(f"quasar-api exited with {code} during startup:\n{text[-2000:]}")
        try:
            return get(port)
        except (
            OSError
        ):  # includes URLError and connection errors: not listening yet (the child is alive, so it owns the port)
            time.sleep(0.1)
    pytest.fail(f"quasar-api did not answer within {START_TIMEOUT_S} s:\n{log.read_text(errors='replace')[-2000:]}")


def launch(tmp_path: Path, db: Path, attempt: int) -> tuple["subprocess.Popen[bytes]", int, Path]:
    script = Path(sys.executable).parent / "quasar-api"
    assert script.exists(), f"the console script {script} is not installed in this environment"
    port = free_port()
    log = tmp_path / f"api-{attempt}.log"
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    with log.open("wb") as log_file:
        proc = subprocess.Popen(
            [str(script), "serve", "--db", str(db), "--host", "127.0.0.1", "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            env=env,
        )
    return proc, port, log


def stop(proc: "subprocess.Popen[bytes]") -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture
def api(tmp_path: Path) -> Iterator[tuple[Api, tuple[int, dict[str, str], dict[str, Any]]]]:
    db = tmp_path / "market.db"  # does not exist yet
    started: subprocess.Popen[bytes] | None = None
    try:
        for attempt in range(MAX_START_ATTEMPTS):
            proc, port, log = launch(tmp_path, db, attempt)
            started = proc
            try:
                first = wait_until_up(proc, port, log)
            except PortTaken:
                proc.wait(timeout=5)
                continue
            yield Api(proc, port, db), first
            return
        pytest.fail(f"the chosen port was taken {MAX_START_ATTEMPTS} times in a row")
    finally:
        if started is not None:
            stop(started)


def test_collector_writes_and_the_api_process_reads(
    api: tuple[Api, tuple[int, dict[str, str], dict[str, Any]]], tmp_path: Path
) -> None:
    server, first = api

    # (1) the process started without a database
    status, headers, body = first
    assert status == 503
    assert body["error"]["details"] == {"reason": "database_missing"}
    assert headers["retry-after"] == "5"

    # (2) the Collector (this test) creates the database; the same process now serves it
    make_database(server.db, candles=[slot(0), slot(1), slot(2)])
    status, _headers, body = get(server.port)
    assert status == 200
    assert [c["open_time_ms"] for c in body["candles"]] == [slot(0), slot(1), slot(2)]

    # (3) a committed write while a writer connection is still open is seen by the next request
    writer = open_writer(server.db, busy_timeout_ms=1000)
    try:
        insert_candle(writer, slot(3))
        status, _headers, body = get(server.port)
        assert status == 200
        assert body["series"]["newest_open_time_ms"] == slot(3)
    finally:
        writer.close()

    # (4) replacing the file with a foreign database is noticed on the next request
    for suffix in ("-wal", "-shm"):  # a stale sidecar of the old file must not meet the foreign one
        Path(f"{server.db}{suffix}").unlink(missing_ok=True)
    backup = tmp_path / "backup.db"
    shutil.copy(server.db, backup)
    foreign = tmp_path / "foreign.db"
    raw = sqlite3.connect(foreign)
    raw.execute("PRAGMA application_id = 1")
    raw.execute("CREATE TABLE t (x)")
    raw.commit()
    raw.close()
    os.replace(foreign, server.db)
    status, headers, body = get(server.port)
    assert status == 503
    assert body["error"]["details"] == {"reason": "schema_mismatch"}
    assert "retry-after" not in headers

    # (5) restoring the file: the API recovers without a restart
    os.replace(backup, server.db)
    status, _headers, body = get(server.port)
    assert status == 200
    assert len(body["candles"]) == 4

    # (6) graceful shutdown on SIGINT
    server.proc.send_signal(signal.SIGINT)
    try:
        code = server.proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pytest.fail("quasar-api did not shut down within 10 s of SIGINT")
    assert code == 0
